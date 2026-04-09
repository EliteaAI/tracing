"""
Custom Socket.IO Tracing via Event Wrapping

This module traces Socket.IO events by wrapping pylon's SIO event handlers.
"""

import time
from typing import Optional, List, Callable
from functools import wraps

from opentelemetry import trace, context as otel_context
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.propagate import extract

from pylon.core.tools import log

from ..core.data_types import TelemetryDataType, TELEMETRY_DATA_TYPE_KEY


class SocketIOTraceWrapper:
    """
    Wraps Socket.IO event handlers to add tracing.

    This works by patching pylon's sio.on() registration method
    to wrap handlers with tracing logic.
    """

    def __init__(
        self,
        tracer: trace.Tracer,
        excluded_events: Optional[List[str]] = None,
        capture_payload: bool = True,
        payload_config: Optional[dict] = None,
        capture_user_context: bool = True,
    ):
        self.tracer = tracer
        self.excluded_events = excluded_events or ['connect', 'disconnect', 'ping', 'pong']
        self.capture_payload = capture_payload
        self.payload_config = payload_config or {}
        self.capture_user_context = capture_user_context
        self._installed = False

    def _should_trace(self, event: str) -> bool:
        """Check if the event should be traced."""
        return event not in self.excluded_events

    def _wrap_existing_handlers(self, server) -> int:
        """
        Wrap handlers that were registered before tracing was installed.

        In python-socketio, handlers are stored in server.handlers dict:
        {namespace: {event: handler_func, ...}, ...}

        Returns:
            Number of handlers wrapped
        """
        wrapped_count = 0

        # Get the handlers dict (it exists in socketio.Server)
        handlers = getattr(server, 'handlers', None)
        if not handlers:
            log.debug("No handlers dict found on server, skipping existing handler wrap")
            return 0

        # Iterate through all namespaces and events
        for namespace, event_handlers in handlers.items():
            if not isinstance(event_handlers, dict):
                continue

            for event, handler in list(event_handlers.items()):
                # Skip excluded events
                if not self._should_trace(event):
                    continue

                # Skip if handler is already wrapped (check for our marker attribute)
                if getattr(handler, '_traced_by_otel', False):
                    continue

                # Wrap the handler
                traced_handler = self.wrap_handler(event, handler)

                # Mark as wrapped to avoid double-wrapping
                traced_handler._traced_by_otel = True

                # Replace in the handlers dict
                event_handlers[event] = traced_handler
                wrapped_count += 1

        return wrapped_count

    def wrap_handler(self, event: str, handler: Callable) -> Callable:
        """Wrap a Socket.IO event handler with tracing."""
        tracer = self.tracer
        should_trace = self._should_trace
        capture_payload = self.capture_payload
        payload_config = self.payload_config
        capture_user_context = self.capture_user_context

        @wraps(handler)
        def traced_handler(*args, **kwargs):
            if not should_trace(event):
                return handler(*args, **kwargs)

            # Build span attributes
            attributes = {
                'messaging.system': 'socketio',
                'messaging.operation': 'receive',
                'messaging.destination': event,
                TELEMETRY_DATA_TYPE_KEY: TelemetryDataType.SOCKET_IO,
            }

            # Add session ID if available (first arg is usually sid)
            sid = None
            if args:
                sid = args[0]
                if isinstance(sid, str):
                    attributes['messaging.session_id'] = sid[:16]  # Truncate for privacy

            # Extract user from SIO session (auth.sio_users[sid])
            if capture_user_context and sid:
                try:
                    from tools import auth
                    auth_data = auth.sio_users.get(sid)
                    if auth_data:
                        auth_id = getattr(auth_data, 'id', None)
                        if auth_id and auth_id != '-':
                            try:
                                attributes['user.id'] = int(auth_id)
                            except (ValueError, TypeError):
                                pass
                except Exception:
                    pass

            # Capture event payload if enabled
            if capture_payload and len(args) > 1:
                try:
                    from ..utils.payload_capture import get_payload_capture
                    capture = get_payload_capture(payload_config)
                    # Event data is typically the second arg after sid
                    event_data = args[1] if len(args) > 1 else None
                    if event_data is not None:
                        payload_attrs = capture.serialize_socketio_event(event_data)
                        attributes.update(payload_attrs)
                except Exception as e:
                    pass  # Don't fail tracing if payload capture fails

            # Capture user context (from baggage, Flask g.auth, or event data)
            if capture_user_context:
                try:
                    from ..utils.user_context import extract_user_context
                    # Try baggage first (propagated), then Flask context, then event kwargs
                    user_attrs = extract_user_context(
                        from_flask=True,
                        from_headers=True,
                        from_baggage=True,  # Check propagated baggage
                        kwargs=kwargs
                    )
                    # Also check if user_id is in event data
                    if len(args) > 1 and isinstance(args[1], dict):
                        event_data = args[1]
                        if 'user_id' in event_data or 'project_id' in event_data:
                            from ..utils.user_context import extract_user_from_kwargs
                            # Only extract user_id from event data, NOT project_id
                            # The project_id in event payload is the target entity's project,
                            # not necessarily the calling user's active project
                            user_attrs_from_event = extract_user_from_kwargs(event_data)
                            user_attrs_from_event.pop('project.id', None)  # Never override project from payload
                            user_attrs.update(user_attrs_from_event)
                    attributes.update(user_attrs)
                except Exception as e:
                    pass  # Don't fail tracing if user context capture fails

            start_time = time.perf_counter()
            span_name = f"SIO {event}"

            with tracer.start_as_current_span(
                span_name,
                kind=SpanKind.SERVER,
                attributes=attributes,
            ) as span:
                try:
                    result = handler(*args, **kwargs)
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    span.set_attribute('messaging.duration_ms', duration_ms)
                    # Re-extract user context after handler (auth decorators may have set g.auth)
                    # NOTE: only set user.id/project.id on the span — email
                    # resolution happens in the audit processor's background thread.
                    if capture_user_context:
                        try:
                            from ..utils.user_context import extract_user_context
                            post_user_attrs = extract_user_context(
                                from_flask=True, from_headers=True, from_baggage=False
                            )
                            for key, value in post_user_attrs.items():
                                if value is not None:
                                    span.set_attribute(key, value)
                        except Exception:
                            pass
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as e:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    span.set_attribute('messaging.duration_ms', duration_ms)
                    span.set_attribute('error.message', str(e)[:200])
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise

        return traced_handler

    def install(self, context):
        """
        Install Socket.IO tracing by wrapping the SIO event registration.

        Args:
            context: Pylon context with sio instance
        """
        if self._installed:
            log.warning("Socket.IO trace wrapper already installed")
            return

        try:
            sio = context.sio
            if not sio:
                log.warning("Socket.IO not available, skipping tracing")
                return

            # Get the underlying socketio.Server instance
            # Pylon wraps it in SocketIO from flask_socketio
            server = getattr(sio, 'server', None) or sio

            # STEP 1: Wrap existing handlers that were registered before this patch
            wrapped_count = self._wrap_existing_handlers(server)
            if wrapped_count > 0:
                log.info(f"Wrapped {wrapped_count} existing Socket.IO handlers with tracing")

            # STEP 2: Patch on() for future handler registrations
            original_on = server.on
            wrapper = self

            def traced_on(event, handler=None, namespace=None):
                """Wrapped on() that adds tracing to handlers."""
                def register_traced(func):
                    # Skip if already traced
                    if getattr(func, '_traced_by_otel', False):
                        return original_on(event, func, namespace)
                    traced_func = wrapper.wrap_handler(event, func)
                    traced_func._traced_by_otel = True
                    return original_on(event, traced_func, namespace)

                if handler is None:
                    # Used as decorator: @sio.on('event')
                    return register_traced
                else:
                    # Used as function: sio.on('event', handler)
                    return register_traced(handler)

            server.on = traced_on

            self._installed = True
            log.info(f"Socket.IO tracing installed (excluding: {self.excluded_events})")

        except Exception as e:
            log.warning(f"Failed to install Socket.IO tracing: {e}")
            import traceback
            traceback.print_exc()


def install_socketio_tracing(
    context,
    tracer: trace.Tracer,
    excluded_events: Optional[List[str]] = None,
    capture_payload: bool = True,
    payload_config: Optional[dict] = None,
    capture_user_context: bool = True,
) -> SocketIOTraceWrapper:
    """
    Install Socket.IO tracing.

    Args:
        context: Pylon context with sio instance
        tracer: OpenTelemetry tracer
        excluded_events: List of event names to exclude from tracing
        capture_payload: Whether to capture event payload data
        payload_config: Configuration for payload capture
        capture_user_context: Whether to capture user identity

    Returns:
        The installed wrapper instance
    """
    wrapper = SocketIOTraceWrapper(
        tracer=tracer,
        excluded_events=excluded_events or ['connect', 'disconnect', 'ping', 'pong'],
        capture_payload=capture_payload,
        payload_config=payload_config,
        capture_user_context=capture_user_context,
    )
    wrapper.install(context)
    return wrapper
