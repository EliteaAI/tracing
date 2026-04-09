"""
Custom Flask Tracing via Request Hooks

This module traces HTTP requests using Flask's before_request/after_request hooks
instead of WSGI middleware. This approach works with pylon's app_shim which doesn't
expose the raw WSGI app.

The trace context is managed via OpenTelemetry's contextvars-based context,
which is greenlet-safe and doesn't interfere with pickle serialization.

IMPORTANT: We store only simple, picklable values in Flask's g object.
The actual span is managed by OpenTelemetry's context (contextvars), not g.
"""

import io
import time
from typing import Optional, List
from contextvars import ContextVar

from flask import Flask, request, g
from opentelemetry import trace, context as otel_context
from opentelemetry.trace import SpanKind, Status, StatusCode, Span
from opentelemetry.propagate import extract
from opentelemetry.semconv.trace import SpanAttributes

from pylon.core.tools import log

from ..core.data_types import TelemetryDataType, TELEMETRY_DATA_TYPE_KEY


# ContextVar to store the active span for the current request
# This is pickle-safe because contextvars work with greenlets
_current_request_span: ContextVar[Optional[Span]] = ContextVar('_current_request_span', default=None)
_current_request_token: ContextVar[Optional[object]] = ContextVar('_current_request_token', default=None)


class FlaskTraceHooks:
    """
    Flask request tracing using before_request/after_request hooks.

    Unlike FlaskInstrumentor, this approach:
    - Does NOT store spans/activations in request.environ
    - Uses only OpenTelemetry's contextvars-based context
    - Is safe to use with pylon's RPC (pickle-compatible)
    - Works with pylon's app_shim (doesn't need wsgi_app)
    """

    def __init__(
        self,
        app: Flask,
        tracer: trace.Tracer,
        excluded_paths: Optional[List[str]] = None,
        capture_payload: bool = True,
        payload_config: Optional[dict] = None,
        capture_user_context: bool = True,
        capture_email: bool = False,
    ):
        self.app = app
        self.tracer = tracer
        self.excluded_paths = excluded_paths or []
        self.capture_payload = capture_payload
        self.payload_config = payload_config or {}
        self.capture_user_context = capture_user_context
        self.capture_email = capture_email
        self._installed = False

    def install(self):
        """Install request tracing hooks."""
        if self._installed:
            log.warning("Flask trace hooks already installed")
            return

        # Note: pylon's app_shim only supports before_request, after_request, context_processor
        # It does NOT support teardown_request, so we handle everything in after_request
        self.app.before_request(self._before_request)
        self.app.after_request(self._after_request)

        self._installed = True
        log.info(f"Flask trace hooks installed (excluding: {self.excluded_paths})")

    def _should_trace(self, path: str) -> bool:
        """Check if the request path should be traced."""
        for excluded in self.excluded_paths:
            if path.startswith(excluded) or excluded in path:
                return False
        return True

    def _before_request(self):
        """Start a span before processing the request."""
        path = request.path

        # Skip excluded paths
        if not self._should_trace(path):
            g._tracing_skip = True
            return

        g._tracing_skip = False
        g._tracing_start_time = time.perf_counter()

        # Extract trace context from incoming headers
        carrier = {k.lower(): v for k, v in request.headers}
        ctx = extract(carrier)

        # Build span attributes
        method = request.method
        url = request.url
        host = request.host

        # Get the matched route pattern (e.g., /api/v2/elitea_core/regenerate/<string:mode>/<int:project_id>)
        # This is used for endpoint identification in observability tools like Dynatrace
        route = request.url_rule.rule if request.url_rule else path

        attributes = {
            SpanAttributes.HTTP_METHOD: method,
            SpanAttributes.HTTP_URL: url,
            SpanAttributes.HTTP_TARGET: path,
            SpanAttributes.HTTP_ROUTE: route,  # Route pattern for endpoint grouping
            SpanAttributes.HTTP_HOST: host,
            SpanAttributes.HTTP_SCHEME: request.scheme,
            TELEMETRY_DATA_TYPE_KEY: TelemetryDataType.API_TRACES,
        }

        # Add user agent if present
        user_agent = request.headers.get('User-Agent')
        if user_agent:
            attributes[SpanAttributes.HTTP_USER_AGENT] = user_agent

        # Capture request payload if enabled
        if self.capture_payload:
            try:
                from ..utils.payload_capture import get_payload_capture
                capture = get_payload_capture(self.payload_config)

                # Capture query parameters
                if request.args:
                    query_dict = dict(request.args)
                    payload_attrs = capture.serialize_http_request(query_params=query_dict)
                    attributes.update(payload_attrs)

                # Capture request body (for POST/PUT/PATCH)
                if method in ('POST', 'PUT', 'PATCH'):
                    try:
                        # Read raw body once and restore the stream for downstream handlers
                        # This is critical for pylon exposure forwarding to work correctly
                        raw_body = request.get_data()

                        # Restore the stream so downstream handlers (like exposure) can read it
                        request.environ['wsgi.input'] = io.BytesIO(raw_body)

                        # Try to parse as JSON for tracing attributes
                        body = request.get_json(silent=True)
                        if body is not None:
                            payload_attrs = capture.serialize_http_request(body=body)
                            attributes.update(payload_attrs)
                        elif raw_body:
                            # Fallback to raw data as text
                            body_text = raw_body.decode('utf-8', errors='replace')
                            if body_text and len(body_text) > 0:
                                payload_attrs = capture.serialize_http_request(body=body_text)
                                attributes.update(payload_attrs)

                        # Restore stream again after get_json consumed it
                        request.environ['wsgi.input'] = io.BytesIO(raw_body)
                    except Exception:
                        pass

                # Capture selected headers
                headers_dict = dict(request.headers)
                payload_attrs = capture.serialize_http_request(headers=headers_dict)
                attributes.update(payload_attrs)

            except Exception as e:
                log.debug(f"Failed to capture HTTP payload: {e}")

        # Create span name from method and route
        # Try to use the matched URL rule for a cleaner span name
        rule = request.url_rule.rule if request.url_rule else path
        span_name = f"{method} {rule}"

        # Start span with extracted context as parent
        # We use start_span (not start_as_current_span) to manage lifecycle manually
        span = self.tracer.start_span(
            span_name,
            context=ctx,
            kind=SpanKind.SERVER,
            attributes=attributes,
        )

        # Debug: log span creation
        span_ctx = span.get_span_context()
        log.info(f"[FLASK_TRACE] Created span: {span_name} trace_id={format(span_ctx.trace_id, '032x')} span_id={format(span_ctx.span_id, '016x')}")

        # Attach the span to the current context
        token = otel_context.attach(trace.set_span_in_context(span))

        # Store span and token in contextvars (NOT in request.environ or g as complex objects)
        _current_request_span.set(span)
        _current_request_token.set(token)

        # Store only simple values in g for after_request
        g._tracing_has_span = True

    def _after_request(self, response):
        """Record response status, finalize the span, and clean up context.

        Note: Since pylon's app_shim doesn't support teardown_request,
        we handle both response recording AND span cleanup here.
        """
        if getattr(g, '_tracing_skip', True):
            return response

        if not getattr(g, '_tracing_has_span', False):
            return response

        span = _current_request_span.get()
        token = _current_request_token.get()

        if span is None:
            return response

        try:
            # Set response status
            status_code = response.status_code
            span.set_attribute(SpanAttributes.HTTP_STATUS_CODE, status_code)

            if status_code >= 400:
                span.set_status(Status(StatusCode.ERROR, f"HTTP {status_code}"))
            else:
                span.set_status(Status(StatusCode.OK))

            # Record duration
            start_time = getattr(g, '_tracing_start_time', None)
            if start_time:
                duration_ms = (time.perf_counter() - start_time) * 1000
                span.set_attribute('http.duration_ms', duration_ms)

            # Add response content length if available
            content_length = response.content_length
            if content_length:
                span.set_attribute('http.response_content_length', content_length)

            # Capture user context (g.auth is populated after auth middleware)
            if self.capture_user_context:
                try:
                    from ..utils.user_context import (
                        extract_user_context, set_user_baggage, get_current_user_info,
                        USER_ID_ATTR, USER_TYPE_ATTR, USER_EMAIL_ATTR, PROJECT_ID_ATTR
                    )
                    user_attrs = extract_user_context(from_flask=True, from_headers=True, from_baggage=True)

                    # Optionally fetch email - check baggage first, RPC only if needed
                    user_email = user_attrs.get(USER_EMAIL_ATTR)
                    if self.capture_email and user_attrs.get(USER_ID_ATTR) and not user_email:
                        try:
                            user_info = get_current_user_info()
                            if user_info:
                                user_attrs.update(user_info)
                                user_email = user_info.get(USER_EMAIL_ATTR)
                        except Exception as e:
                            log.debug(f"Failed to fetch user email: {e}")

                    # Set baggage for propagation to downstream services (including email)
                    set_user_baggage(
                        user_id=user_attrs.get(USER_ID_ATTR),
                        user_type=user_attrs.get(USER_TYPE_ATTR),
                        project_id=user_attrs.get(PROJECT_ID_ATTR),
                        user_email=user_email,
                    )

                    # Extract project_id from URL view args if not in user context
                    if PROJECT_ID_ATTR not in user_attrs or user_attrs[PROJECT_ID_ATTR] is None:
                        try:
                            view_args = request.view_args or {}
                            pid = view_args.get('project_id')
                            if pid is not None:
                                user_attrs[PROJECT_ID_ATTR] = int(pid)
                        except (ValueError, TypeError):
                            pass

                    for key, value in user_attrs.items():
                        if value is not None:
                            span.set_attribute(key, value)
                except Exception as e:
                    log.debug(f"Failed to capture user context: {e}")

        except Exception as e:
            log.warning(f"Error recording response in span: {e}")

        finally:
            # End the span (always, even if there was an error recording attributes)
            try:
                span_ctx = span.get_span_context()
                log.info(f"[FLASK_TRACE] Ending span: trace_id={format(span_ctx.trace_id, '032x')} span_id={format(span_ctx.span_id, '016x')} status={response.status_code}")
                span.end()
                log.info(f"[FLASK_TRACE] Span ended successfully")
            except Exception as e:
                log.warning(f"Error ending span: {e}")

            # Detach from context
            if token:
                try:
                    otel_context.detach(token)
                except Exception:
                    pass

            # Clear contextvars
            _current_request_span.set(None)
            _current_request_token.set(None)

        return response



# Keep old class name for backwards compatibility
FlaskTraceMiddleware = FlaskTraceHooks


def install_flask_tracing(
    app: Flask,
    tracer: trace.Tracer,
    excluded_paths: Optional[List[str]] = None,
    capture_payload: bool = True,
    payload_config: Optional[dict] = None,
    capture_user_context: bool = True,
    capture_email: bool = False,
) -> FlaskTraceHooks:
    """
    Install Flask tracing using request hooks.

    Args:
        app: Flask application
        tracer: OpenTelemetry tracer
        excluded_paths: List of path prefixes to exclude from tracing
        capture_payload: Whether to capture request body and query params
        payload_config: Configuration for payload capture
        capture_user_context: Whether to capture user identity (g.auth)
        capture_email: Whether to fetch user email via RPC (adds overhead)

    Returns:
        The installed hooks instance
    """
    hooks = FlaskTraceHooks(
        app=app,
        tracer=tracer,
        excluded_paths=excluded_paths or ['/health', '/static', '/favicon'],
        capture_payload=capture_payload,
        payload_config=payload_config,
        capture_user_context=capture_user_context,
        capture_email=capture_email,
    )
    hooks.install()
    return hooks
