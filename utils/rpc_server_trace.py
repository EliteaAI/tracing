"""
RPC Server Tracing Wrapper

Wraps incoming RPC handlers to create SERVER spans for distributed tracing.
This enables services like pylon-auth to appear in observability backends
when they receive RPC calls from other pylons.
"""

import time
import functools
from typing import Callable, Any, Optional

from pylon.core.tools import log


def create_rpc_server_wrapper(
    tracer,
    service_name: str = "pylon-auth",
    telemetry_data_type: str = "rpc_server",
    capture_payload: bool = True,
    payload_config: dict = None,
    capture_user_context: bool = True,
):
    """
    Create an RPC server wrapper factory that wraps RPC handlers with tracing.

    Args:
        tracer: OpenTelemetry tracer instance
        service_name: Name of the service for span attributes
        telemetry_data_type: Data type for telemetry routing
        capture_payload: Whether to capture actual request parameters
        payload_config: Configuration for payload capture (max_size, etc.)
        capture_user_context: Whether to capture user identity from context

    Returns:
        A wrapper function that can be used to wrap RPC handlers
    """
    payload_config = payload_config or {}
    def wrap_rpc_handler(rpc_name: str):
        """
        Decorator to wrap an RPC handler with SERVER-side tracing.

        Args:
            rpc_name: Name of the RPC method (used in span name)

        Returns:
            Decorator function
        """
        def decorator(func: Callable) -> Callable:
            @functools.wraps(func)
            def traced_rpc_handler(*args, **kwargs) -> Any:
                if tracer is None:
                    return func(*args, **kwargs)

                try:
                    from opentelemetry.trace import SpanKind, Status, StatusCode

                    # Build attributes
                    attributes = {
                        'rpc.system': 'pylon',
                        'rpc.method': rpc_name,
                        'rpc.service': service_name,
                        'telemetry.data_type': telemetry_data_type,
                        'rpc.role': 'server',
                    }

                    # Determine caller service from method name prefix
                    if rpc_name.startswith('auth_'):
                        # This is an auth handler being called
                        attributes['rpc.handler_type'] = 'auth'

                    # Add argument summary (safely)
                    if args:
                        attributes['rpc.args_count'] = len(args)
                    if kwargs:
                        attributes['rpc.kwargs_keys'] = ','.join(str(k) for k in kwargs.keys())

                    # Capture actual payload if enabled
                    if capture_payload:
                        try:
                            from .payload_capture import get_payload_capture
                            capture = get_payload_capture(payload_config)
                            payload_attrs = capture.serialize_args(args, kwargs)
                            attributes.update(payload_attrs)
                        except Exception as e:
                            log.debug(f"Failed to capture RPC server payload: {e}")

                    # Capture user context (from baggage, Flask g.auth, headers, or kwargs)
                    if capture_user_context:
                        try:
                            from .user_context import extract_user_context
                            user_attrs = extract_user_context(
                                from_flask=True,
                                from_headers=True,
                                from_baggage=True,  # Check propagated baggage
                                kwargs=kwargs
                            )
                            attributes.update(user_attrs)
                        except Exception as e:
                            log.debug(f"Failed to capture user context: {e}")

                    # Extract entity context and project_id from args/kwargs
                    try:
                        sources = [kwargs]
                        if isinstance(kwargs.get('data'), dict):
                            sources.append(kwargs['data'])
                        for arg in args:
                            if isinstance(arg, dict):
                                sources.append(arg)
                        for source in sources:
                            for entity_key, entity_type_name in [
                                ('application_id', 'application'),
                                ('datasource_id', 'datasource'),
                            ]:
                                if entity_key in source and source[entity_key]:
                                    attributes['entity.type'] = entity_type_name
                                    attributes['entity.id'] = str(source[entity_key])
                                    break
                            if 'entity.type' in attributes:
                                break
                        # Capture entity name from any source dict
                        if 'entity.type' in attributes:
                            for source in sources:
                                ename = source.get('entity_name')
                                if ename:
                                    attributes['entity.name'] = str(ename)
                                    break
                        # Also extract project_id from args/kwargs
                        pid = kwargs.get('chat_project_id') or kwargs.get('project_id')
                        if not pid:
                            for source in sources:
                                pid = source.get('project_id')
                                if pid:
                                    break
                        # Only set project.id from payload if not already set from user context
                        if pid and 'project.id' not in attributes:
                            try:
                                attributes['project.id'] = int(pid)
                            except (TypeError, ValueError):
                                pass
                    except Exception:
                        pass

                    # Create SERVER span for incoming RPC call
                    start_time = time.perf_counter()
                    with tracer.start_as_current_span(
                        f"RPC Server {rpc_name}",
                        kind=SpanKind.SERVER,
                        attributes=attributes
                    ) as span:
                        try:
                            result = func(*args, **kwargs)
                            duration_ms = (time.perf_counter() - start_time) * 1000
                            span.set_attribute('rpc.duration_ms', duration_ms)
                            span.set_status(Status(StatusCode.OK))
                            return result
                        except Exception as e:
                            duration_ms = (time.perf_counter() - start_time) * 1000
                            span.set_attribute('rpc.duration_ms', duration_ms)
                            span.set_attribute('rpc.error', str(e)[:500])
                            span.set_status(Status(StatusCode.ERROR, str(e)))
                            span.record_exception(e)
                            raise

                except ImportError:
                    # OpenTelemetry not available, run without tracing
                    return func(*args, **kwargs)
                except Exception as e:
                    # Tracing failed, but don't break the RPC handler
                    log.warning(f"RPC server tracing failed for {rpc_name}: {e}")
                    return func(*args, **kwargs)

            return traced_rpc_handler
        return decorator
    return wrap_rpc_handler
