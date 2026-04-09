"""
Tracing decorators for easy instrumentation.
"""

import functools
from typing import Optional, Dict, Any, Callable

from pylon.core.tools import log


def traced(
    name: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None,
    record_exception: bool = True,
):
    """
    Decorator to trace a synchronous function.

    Args:
        name: Span name (defaults to function name)
        attributes: Additional span attributes
        record_exception: Whether to record exceptions in the span

    Usage:
        @traced("my_operation")
        def my_function():
            ...
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Try to get tracer from tracing plugin
            tracer = _get_tracer()

            if tracer is None:
                # Tracing disabled, just run the function
                return func(*args, **kwargs)

            span_name = name or func.__name__
            span_attributes = attributes or {}

            from opentelemetry.trace import Status, StatusCode

            with tracer.start_as_current_span(span_name, attributes=span_attributes) as span:
                try:
                    result = func(*args, **kwargs)
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    if record_exception:
                        span.record_exception(e)
                    raise

        return wrapper
    return decorator


def traced_async(
    name: Optional[str] = None,
    attributes: Optional[Dict[str, Any]] = None,
    record_exception: bool = True,
):
    """
    Decorator to trace an async function.

    Args:
        name: Span name (defaults to function name)
        attributes: Additional span attributes
        record_exception: Whether to record exceptions in the span

    Usage:
        @traced_async("my_async_operation")
        async def my_async_function():
            ...
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            tracer = _get_tracer()

            if tracer is None:
                return await func(*args, **kwargs)

            span_name = name or func.__name__
            span_attributes = attributes or {}

            from opentelemetry.trace import Status, StatusCode

            with tracer.start_as_current_span(span_name, attributes=span_attributes) as span:
                try:
                    result = await func(*args, **kwargs)
                    span.set_status(Status(StatusCode.OK))
                    return result
                except Exception as e:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    if record_exception:
                        span.record_exception(e)
                    raise

        return wrapper
    return decorator


def _get_tracer():
    """Get tracer from the tracing plugin if available."""
    try:
        from tools import this
        if this.for_module('tracing').module.enabled:
            return this.for_module('tracing').module.get_tracer()
    except Exception:
        pass
    return None
