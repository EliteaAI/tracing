"""
Task Tracing Wrapper

Wraps arbiter task handlers to create SERVER spans for incoming tasks.
This enables services like pylon-indexer to appear in observability backends.
"""

import time
import functools
from typing import Callable, Any, Optional

from pylon.core.tools import log


def create_traced_task_wrapper(
    tracer,
    service_name: str = "pylon-indexer",
    telemetry_data_type: str = "task_execution",
):
    """
    Create a task wrapper factory that wraps task handlers with tracing.

    Args:
        tracer: OpenTelemetry tracer instance
        service_name: Name of the service for span attributes
        telemetry_data_type: Data type for telemetry routing

    Returns:
        A wrapper function that can be used to wrap task handlers
    """
    def wrap_task(task_name: str):
        """
        Decorator to wrap a task handler with tracing.

        Args:
            task_name: Name of the task (used in span name)

        Returns:
            Decorator function
        """
        def decorator(func: Callable) -> Callable:
            @functools.wraps(func)
            def traced_task(*args, **kwargs) -> Any:
                if tracer is None:
                    return func(*args, **kwargs)

                try:
                    from opentelemetry.trace import SpanKind, Status, StatusCode

                    # Build attributes
                    attributes = {
                        'task.name': task_name,
                        'task.service': service_name,
                        'telemetry.data_type': telemetry_data_type,
                        'rpc.system': 'arbiter',
                        'rpc.method': task_name,
                        'rpc.service': service_name,
                    }

                    # Create SERVER span for incoming task
                    start_time = time.perf_counter()
                    with tracer.start_as_current_span(
                        f"Task {task_name}",
                        kind=SpanKind.SERVER,
                        attributes=attributes
                    ) as span:
                        try:
                            result = func(*args, **kwargs)
                            duration_ms = (time.perf_counter() - start_time) * 1000
                            span.set_attribute('task.duration_ms', duration_ms)
                            span.set_status(Status(StatusCode.OK))
                            return result
                        except Exception as e:
                            duration_ms = (time.perf_counter() - start_time) * 1000
                            span.set_attribute('task.duration_ms', duration_ms)
                            span.set_attribute('task.error', str(e)[:500])
                            span.set_status(Status(StatusCode.ERROR, str(e)))
                            span.record_exception(e)
                            raise

                except ImportError:
                    # OpenTelemetry not available, run without tracing
                    return func(*args, **kwargs)
                except Exception as e:
                    # Tracing failed, but don't break the task
                    log.warning(f"Task tracing failed for {task_name}: {e}")
                    return func(*args, **kwargs)

            return traced_task
        return decorator
    return wrap_task


def get_task_wrapper_from_context(context) -> Optional[Callable]:
    """
    Get task wrapper from pylon context if tracing plugin is available.

    Args:
        context: Pylon context object

    Returns:
        Task wrapper function or None if tracing not available
    """
    try:
        from tools import this
        tracing_module = this.for_module("tracing").module

        if not tracing_module.enabled:
            return None

        tracer = tracing_module.get_tracer()
        if tracer is None:
            return None

        service_name = tracing_module.config.get('service', {}).get('name', 'pylon-indexer')
        return create_traced_task_wrapper(tracer, service_name)

    except Exception as e:
        log.debug(f"Tracing not available for task wrapping: {e}")
        return None
