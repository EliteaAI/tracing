"""
Trace context utilities for extracting and propagating trace context.
"""

import uuid
from contextvars import ContextVar
from typing import Optional, Dict, Any

from flask import request, g, has_request_context
from pylon.core.tools import log

# Context variable for trace ID (works across async boundaries)
_trace_id_var: ContextVar[Optional[str]] = ContextVar('trace_id', default=None)
_span_id_var: ContextVar[Optional[str]] = ContextVar('span_id', default=None)


def generate_trace_id(prefix: str = 'srv') -> str:
    """Generate a new trace ID with optional prefix."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def get_current_trace_id() -> Optional[str]:
    """
    Get current trace ID from OpenTelemetry context, Flask g, or context var.
    Returns None if no trace is active.
    """
    # Try OpenTelemetry first
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span and span.get_span_context().is_valid:
            return format(span.get_span_context().trace_id, '032x')
    except Exception:
        pass

    # Try Flask g object
    if has_request_context():
        trace_id = getattr(g, 'trace_id', None)
        if trace_id:
            return trace_id

    # Try context var
    return _trace_id_var.get()


def get_current_span_id() -> Optional[str]:
    """Get current span ID from OpenTelemetry context."""
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span and span.get_span_context().is_valid:
            return format(span.get_span_context().span_id, '016x')
    except Exception:
        pass

    return _span_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """Set trace ID in context var and Flask g (if available)."""
    _trace_id_var.set(trace_id)
    if has_request_context():
        g.trace_id = trace_id


def extract_trace_context() -> Dict[str, Any]:
    """
    Extract trace context from incoming request headers.
    Supports both W3C Trace Context and custom X-Trace-ID header.
    """
    context = {}

    if not has_request_context():
        return context

    # Try W3C traceparent header first
    traceparent = request.headers.get('traceparent')
    if traceparent:
        try:
            # Format: version-trace_id-span_id-flags
            parts = traceparent.split('-')
            if len(parts) >= 3:
                context['trace_id'] = parts[1]
                context['parent_span_id'] = parts[2]
                context['w3c'] = True
        except Exception as e:
            log.debug(f"Failed to parse traceparent header: {e}")

    # Try custom X-Trace-ID header
    custom_trace_id = request.headers.get('X-Trace-ID')
    if custom_trace_id and 'trace_id' not in context:
        context['trace_id'] = custom_trace_id
        context['w3c'] = False

    # Store in Flask g for later use
    if 'trace_id' in context:
        set_trace_id(context['trace_id'])

    return context


def extract_trace_from_sio_payload(data: dict) -> Optional[str]:
    """
    Extract trace ID from Socket.IO event payload.
    Expects payload to have _trace.trace_id field.
    """
    trace_info = data.get('_trace', {})
    trace_id = trace_info.get('trace_id')

    if trace_id:
        set_trace_id(trace_id)
        return trace_id

    return None


def inject_trace_context(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Inject current trace context into outgoing request headers.
    Adds both W3C traceparent and custom X-Trace-ID.
    """
    trace_id = get_current_trace_id()
    span_id = get_current_span_id()

    if trace_id:
        headers['X-Trace-ID'] = trace_id

        # Add W3C traceparent if we have both IDs
        if span_id:
            # Format: version-trace_id-span_id-flags
            # Using version 00, flags 01 (sampled)
            headers['traceparent'] = f"00-{trace_id}-{span_id}-01"

    return headers


def set_span_attributes(attributes: Dict[str, Any]) -> None:
    """
    Set attributes on the current span.
    Safely handles case when tracing is disabled.
    """
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span and span.is_recording():
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(key, str(value) if not isinstance(value, (int, float, bool)) else value)
    except Exception as e:
        log.debug(f"Failed to set span attributes: {e}")


def add_span_event(name: str, attributes: Dict[str, Any] = None) -> None:
    """
    Add an event to the current span.
    Useful for marking significant points within a span.
    """
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span and span.is_recording():
            span.add_event(name, attributes=attributes or {})
    except Exception as e:
        log.debug(f"Failed to add span event: {e}")


def get_current_traceparent() -> Optional[str]:
    """
    Get the current W3C traceparent header value.

    Returns:
        str: W3C traceparent header value (e.g., "00-{trace_id}-{span_id}-01")
             or None if no trace is active.
    """
    trace_id = get_current_trace_id()
    span_id = get_current_span_id()

    if trace_id and span_id:
        # Format: version-trace_id-span_id-flags
        # Using version 00, flags 01 (sampled)
        return f"00-{trace_id}-{span_id}-01"

    return None
