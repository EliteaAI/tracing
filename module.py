"""
Tracing Plugin Module

Provides distributed tracing capabilities using OpenTelemetry.
Traces are routed through the OTEL Collector to various backends (Jaeger, Dynatrace, etc.)
based on the `telemetry.data_type` attribute.

Data types:
- api_traces: REST API request/response traces
- db_queries: Database query traces
- rpc_calls: Inter-service RPC call traces
- http_client: Outgoing HTTP request traces
- llm_traces: LLM/AI model traces (handled by Langfuse callback in elitea_sdk)
"""

import os
from pylon.core.tools import module, log

# Data type constants for telemetry routing
from .core.data_types import TelemetryDataType, TELEMETRY_DATA_TYPE_KEY

# Check if api_tools is available (pylon_main/pylon_auth only)
try:
    from tools import api_tools
    _API_TOOLS_IMPORTABLE = True
except ImportError:
    _API_TOOLS_IMPORTABLE = False


class Module(module.ModuleModel):
    """Tracing plugin for distributed request tracing."""

    def __init__(self, context, descriptor):
        self.context = context
        self.descriptor = descriptor
        self.config = self.descriptor.config
        self.tracer = None
        self.tracer_provider = None
        self._enabled = False
        self._flask_middleware = None
        self._socketio_wrapper = None
        self._eventnode_handler = None
        self._event_node = None
        # RPC server tracing wrapper for inter-pylon calls (set lazily when tracing is initialized)
        self._rpc_server_wrapper = None
        # Metrics (MeterProvider for system/infra metrics)
        self.meter_provider = None
        self.meter = None
        self._system_metrics_collector = None
        # Audit trail
        self._audit_enabled = False
        self._audit_mode = None
        self._audit_config = {}
        self._audit_event_node = None
        # Email cache moved to utils.user_context (service-level singleton)

    def init(self):
        """Initialize the tracing plugin."""
        # Re-read config from descriptor (external config may have been merged after __init__)
        self.config = self.descriptor.config
        # Determine if API endpoints should be registered (status/collect/otlp endpoints)
        self._register_api = _API_TOOLS_IMPORTABLE and self.config.get('register_api', False)

        # Check if tracing is enabled via config
        self._enabled = self.config.get('enabled', False)

        # Environment variable override (takes precedence)
        env_enabled = os.environ.get('TRACING_ENABLED', '').lower()
        if env_enabled == 'true':
            self._enabled = True
        elif env_enabled == 'false':
            self._enabled = False

        # Environment variable for OTLP endpoint override
        env_endpoint = os.environ.get('TRACING_OTLP_ENDPOINT')
        if env_endpoint:
            if 'otlp' not in self.config:
                self.config['otlp'] = {}
            self.config['otlp']['endpoint'] = env_endpoint

        if not self._enabled:
            log.info("Tracing plugin is DISABLED (set enabled: true in config or TRACING_ENABLED=true)")
            # Still register API for status endpoint (only if configured)
            if self._register_api:
                self.descriptor.init_api()
            return

        log.info("Tracing plugin is ENABLED - initializing OpenTelemetry...")

        try:
            self._setup_opentelemetry()
            otlp_enabled = self.config.get('otlp', {}).get('enabled', True)
            if otlp_enabled:
                log.info(f"OpenTelemetry initialized - sending traces to {self.config.get('otlp', {}).get('endpoint')}")
            else:
                log.info("OpenTelemetry initialized in local-only mode (OTLP export disabled)")
        except Exception as e:
            log.error(f"Failed to initialize OpenTelemetry: {e}")
            self._enabled = False
            if self._register_api:
                self.descriptor.init_api()
            return

        # Setup metrics (MeterProvider for system/infra metrics)
        # Metrics require OTLP export - skip when otlp is disabled
        otlp_enabled = self.config.get('otlp', {}).get('enabled', True)
        if otlp_enabled and self.config.get('instrumentation', {}).get('system_metrics', True):
            try:
                self._setup_metrics()
                log.info("OpenTelemetry Metrics initialized")
            except Exception as e:
                log.warning(f"Failed to initialize metrics: {e}")
        elif not otlp_enabled:
            log.info("Metrics export skipped (OTLP disabled)")

        # Register components (API only if configured - skip on worker pylons)
        if self._register_api:
            self.descriptor.init_api()
        self.descriptor.init_methods()

        # NOTE: Flask auto-instrumentation (FlaskInstrumentor) is NOT used because
        # it stores non-picklable objects in request.environ, which breaks pylon's
        # RPC serialization. Instead, we use a custom middleware that only uses
        # OpenTelemetry's contextvars-based context (pickle-safe).
        # The middleware is installed in ready() after Flask app is available.

        # Patch RPC registration for SERVER-side tracing (incoming RPC calls)
        # This ensures RPC handlers registered by later plugins will be wrapped
        if self.config.get('instrumentation', {}).get('rpc_server', True):
            self._patch_rpc_registration()

        # Instrument requests library for outgoing HTTP calls (can be done early)
        if self.config.get('instrumentation', {}).get('http_client', True):
            self._instrument_requests()

        # Instrument Python logging to include trace context
        if self.config.get('instrumentation', {}).get('logging', True):
            self._instrument_logging()

        # Instrument Socket.IO early to catch handlers from other plugins
        # This patches sio.on() so future registrations will be traced
        if self.config.get('instrumentation', {}).get('socket_io', True):
            self._instrument_socketio()

        # Initialize audit trail if configured
        audit_config = self.config.get('audit_trail', {})
        if audit_config.get('enabled', False):
            self._init_audit_trail(audit_config)

        log.info("Tracing plugin initialization complete")

    def ready(self):
        """Called after all plugins are initialized - instrument DB, RPC, and Flask here."""
        print("[TRACING] ready() called", flush=True)
        log.info("[TRACING] ready() called")
        if not self._enabled:
            log.info("[TRACING] ready() - tracing not enabled, skipping")
            return

        # Instrument Flask for HTTP request tracing (uses custom pickle-safe middleware)
        http_requests_enabled = self.config.get('instrumentation', {}).get('http_requests', True)
        print(f"[TRACING] ready() - http_requests: {http_requests_enabled}", flush=True)
        if http_requests_enabled:
            print("[TRACING] calling _instrument_flask_middleware()", flush=True)
            self._instrument_flask_middleware()

        # Instrument SQLAlchemy for database query tracing (needs db engine to be ready)
        if self.config.get('instrumentation', {}).get('database', True):
            self._instrument_sqlalchemy()

        # Instrument RPC calls
        if self.config.get('instrumentation', {}).get('rpc_calls', True):
            self._instrument_rpc()

        # Wrap existing RPC handlers with SERVER-side tracing (retroactive)
        # This catches handlers registered by plugins that loaded before tracing
        if self.config.get('instrumentation', {}).get('rpc_server', True):
            self._wrap_existing_rpc_handlers()

        # Register audit SpanProcessor (must be after TracerProvider is set up)
        if self._audit_enabled:
            self._register_audit_processor()

    def _wrap_existing_rpc_handlers(self):
        """Retroactively wrap existing RPC handlers with SERVER-side tracing.

        Since tracing may load after other plugins, handlers from auth_core
        and other plugins may already be registered. This method wraps them.
        """
        try:
            # Initialize server wrapper if not already done
            if self._rpc_server_wrapper is None:
                self._init_rpc_server_tracing()

            if self._rpc_server_wrapper is None:
                log.debug("RPC server wrapper not available, skipping handler wrap")
                return

            # Access the service_node's services dict where handlers are stored
            rpc_manager = self.context.rpc_manager
            service_node = rpc_manager.node.service_node
            services = service_node.services

            wrapped_count = 0
            for name, handler in list(services.items()):
                # Skip if already wrapped (check for marker attribute)
                if hasattr(handler, '_rpc_server_traced'):
                    continue

                # Create traced wrapper
                traced_handler = self._rpc_server_wrapper(name)(handler)
                traced_handler._rpc_server_traced = True

                # Replace in services dict
                with service_node.lock:
                    services[name] = traced_handler
                wrapped_count += 1

            if wrapped_count > 0:
                log.info(f"Wrapped {wrapped_count} existing RPC handlers with SERVER-side tracing")

        except Exception as e:
            log.warning(f"Failed to wrap existing RPC handlers: {e}")

    def _instrument_socketio(self):
        """Install Socket.IO tracing by wrapping event handlers."""
        try:
            from .middleware.socketio_trace import install_socketio_tracing

            # Get excluded events from config (default: connect, disconnect, ping, pong)
            excluded_events = self.config.get('exclude', {}).get('socket_events', [
                'connect', 'disconnect', 'ping', 'pong'
            ])
            payload_config = self.config.get('payload_capture', {})
            capture_payload = payload_config.get('enabled', True)
            user_context_config = self.config.get('user_context', {})
            capture_user_context = user_context_config.get('enabled', True)

            self._socketio_wrapper = install_socketio_tracing(
                context=self.context,
                tracer=self.tracer,
                excluded_events=excluded_events,
                capture_payload=capture_payload,
                payload_config=payload_config,
                capture_user_context=capture_user_context,
            )
            log.info(f"Socket.IO tracing enabled (excluding: {excluded_events})")
        except Exception as e:
            log.warning(f"Failed to install Socket.IO tracing: {e}")
            import traceback
            traceback.print_exc()

    def _instrument_gevent(self):
        """Instrument gevent for proper context propagation across greenlets.

        CRITICAL: This must be called BEFORE setting up the TracerProvider.
        Without this, spans created in different greenlets become separate traces.
        """
        try:
            import gevent
            import contextvars
            from functools import wraps

            # Patch gevent.spawn to copy context to greenlets
            original_spawn = gevent.spawn

            @wraps(original_spawn)
            def patched_spawn(func, *args, **kwargs):
                # Copy current context
                ctx = contextvars.copy_context()

                # Wrap the function to run in copied context
                @wraps(func)
                def context_aware_func(*a, **kw):
                    return ctx.run(func, *a, **kw)

                return original_spawn(context_aware_func, *args, **kwargs)

            gevent.spawn = patched_spawn

            # Also patch spawn_later
            original_spawn_later = gevent.spawn_later

            @wraps(original_spawn_later)
            def patched_spawn_later(seconds, func, *args, **kwargs):
                ctx = contextvars.copy_context()

                @wraps(func)
                def context_aware_func(*a, **kw):
                    return ctx.run(func, *a, **kw)

                return original_spawn_later(seconds, context_aware_func, *args, **kwargs)

            gevent.spawn_later = patched_spawn_later

            log.info("Gevent spawn patched for contextvars propagation across greenlets")
        except ImportError:
            log.debug("Gevent not available, skipping gevent instrumentation")
        except Exception as e:
            log.warning(f"Failed to instrument gevent: {e}")

    def _setup_opentelemetry(self):
        """Configure OpenTelemetry with Jaeger exporter."""
        # CRITICAL: Instrument gevent FIRST for proper context propagation
        self._instrument_gevent()

        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, DEPLOYMENT_ENVIRONMENT

        otlp_config = self.config.get('otlp', {})
        service_config = self.config.get('service', {})

        # Create resource with service info
        resource = Resource.create({
            SERVICE_NAME: service_config.get('name', 'pylon-main'),
            DEPLOYMENT_ENVIRONMENT: service_config.get('environment', 'development'),
        })

        # Create tracer provider with optional sampling
        sampling_config = self.config.get('sampling', {})
        if sampling_config.get('enabled', False):
            from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
            sampler = TraceIdRatioBased(sampling_config.get('rate', 1.0))
            self.tracer_provider = TracerProvider(resource=resource, sampler=sampler)
        else:
            self.tracer_provider = TracerProvider(resource=resource)

        # Configure OTLP exporter only when otlp is enabled
        otlp_enabled = otlp_config.get('enabled', True)
        if otlp_enabled:
            endpoint = otlp_config.get('endpoint', 'http://jaeger:4317')
            insecure = otlp_config.get('insecure', True)

            exporter = OTLPSpanExporter(
                endpoint=endpoint,
                insecure=insecure,
            )

            # Add batch processor for efficient span export
            self.tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
        else:
            log.info("OTLP export disabled - TracerProvider initialized without remote exporter (local-only mode)")

        # Set as global tracer provider
        trace.set_tracer_provider(self.tracer_provider)

        # Get tracer instance
        self.tracer = trace.get_tracer(
            service_config.get('name', 'pylon-main'),
            '1.0.0'
        )

    def _setup_metrics(self):
        """Configure OpenTelemetry Metrics with OTLP exporter for system/infra metrics."""
        from opentelemetry import metrics
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, DEPLOYMENT_ENVIRONMENT

        otlp_config = self.config.get('otlp', {})
        service_config = self.config.get('service', {})
        metrics_config = self.config.get('metrics', {})

        # Create resource with service info (same as traces)
        resource = Resource.create({
            SERVICE_NAME: service_config.get('name', 'pylon-auth'),
            DEPLOYMENT_ENVIRONMENT: service_config.get('environment', 'development'),
        })

        # Configure OTLP metrics exporter
        endpoint = otlp_config.get('endpoint', 'http://otel-collector:4317')
        insecure = otlp_config.get('insecure', True)

        metric_exporter = OTLPMetricExporter(
            endpoint=endpoint,
            insecure=insecure,
        )

        # Export interval (default 15 seconds)
        export_interval = metrics_config.get('export_interval_ms', 15000)

        # Create metric reader with periodic export
        metric_reader = PeriodicExportingMetricReader(
            exporter=metric_exporter,
            export_interval_millis=export_interval,
        )

        # Create and set meter provider
        self.meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[metric_reader],
        )
        metrics.set_meter_provider(self.meter_provider)

        # Get meter instance
        self.meter = metrics.get_meter(
            service_config.get('name', 'pylon-auth'),
            '1.0.0'
        )

        # Start system metrics collection
        self._start_system_metrics()

    def _start_system_metrics(self):
        """Start collecting system/infrastructure metrics."""
        try:
            from .utils.system_metrics import create_system_metrics_collector

            service_config = self.config.get('service', {})
            metrics_config = self.config.get('metrics', {}).get('system', {})

            self._system_metrics_collector = create_system_metrics_collector(
                meter=self.meter,
                service_name=service_config.get('name', 'pylon-auth'),
                config=metrics_config,
            )

            if self._system_metrics_collector:
                self._system_metrics_collector.start()
                log.info("System metrics collection started")
            else:
                log.warning("System metrics collector not available (psutil missing?)")

        except Exception as e:
            log.warning(f"Failed to start system metrics: {e}")

    def _instrument_flask_middleware(self):
        """Install custom Flask tracing middleware (pickle-safe, doesn't use request.environ)."""
        print("[TRACING] _instrument_flask_middleware() - starting", flush=True)
        try:
            from .middleware.flask_trace import install_flask_tracing
            from tools import context
            print("[TRACING] _instrument_flask_middleware() - imports successful", flush=True)

            # Get the Flask app from pylon's context
            app = context.app
            print(f"[TRACING] context.app = {app}", flush=True)
            if not app:
                print("[TRACING] Flask app not available, skipping", flush=True)
                log.warning("Flask app not available, skipping HTTP request tracing")
                return

            exclude_paths = self.config.get('exclude', {}).get('paths', [])
            payload_config = self.config.get('payload_capture', {})
            capture_payload = payload_config.get('enabled', True)
            user_context_config = self.config.get('user_context', {})
            capture_user_context = user_context_config.get('enabled', True)
            capture_email = user_context_config.get('capture_email', False)
            print(f"[TRACING] exclude_paths = {exclude_paths}", flush=True)
            print(f"[TRACING] self.tracer = {self.tracer}", flush=True)

            self._flask_middleware = install_flask_tracing(
                app=app,
                tracer=self.tracer,
                excluded_paths=exclude_paths,
                capture_payload=capture_payload,
                payload_config=payload_config,
                capture_user_context=capture_user_context,
                capture_email=capture_email,
            )
            print("[TRACING] install_flask_tracing() completed", flush=True)
            log.info(f"Flask tracing middleware enabled (excluding: {exclude_paths})")
        except Exception as e:
            log.warning(f"Failed to install Flask tracing middleware: {e}")
            import traceback
            traceback.print_exc()

    def _instrument_flask(self):
        """Auto-instrument Flask requests using FlaskInstrumentor (NOT USED - pickle issues)."""
        try:
            from opentelemetry.instrumentation.flask import FlaskInstrumentor

            exclude_paths = self.config.get('exclude', {}).get('paths', [])
            excluded_urls = ','.join(exclude_paths) if exclude_paths else None

            FlaskInstrumentor().instrument(
                excluded_urls=excluded_urls,
            )
            log.info(f"Flask instrumentation enabled (excluding: {exclude_paths})")
        except Exception as e:
            log.warning(f"Failed to instrument Flask: {e}")

    def _instrument_sqlalchemy(self):
        """Auto-instrument SQLAlchemy for database query tracing."""
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
            from opentelemetry.sdk.trace import SpanProcessor
            from opentelemetry.trace import Span
            from tools import context

            # Get the SQLAlchemy engine from pylon's context
            engine = context.db.engine
            if engine:
                SQLAlchemyInstrumentor().instrument(
                    engine=engine,
                    enable_commenter=True,  # Add trace context as SQL comments
                )
                log.info("SQLAlchemy instrumentation enabled for database tracing")
            else:
                log.warning("SQLAlchemy instrumentation skipped - no engine available")
        except Exception as e:
            log.warning(f"Failed to instrument SQLAlchemy: {e}")

    def _instrument_requests(self):
        """Auto-instrument requests library for outgoing HTTP calls."""
        try:
            from opentelemetry.instrumentation.requests import RequestsInstrumentor

            RequestsInstrumentor().instrument()
            log.info("Requests instrumentation enabled for HTTP client tracing")
        except Exception as e:
            log.warning(f"Failed to instrument requests: {e}")

    def _instrument_rpc(self):
        """Instrument RPC calls for distributed tracing."""
        try:
            from opentelemetry import trace
            from opentelemetry.trace import SpanKind, Status, StatusCode
            import time

            rpc_manager = self.context.rpc_manager
            original_proxy = rpc_manager.call
            tracer = self.tracer

            # Get payload capture config
            payload_config = self.config.get('payload_capture', {})
            capture_payload = payload_config.get('enabled', True)

            # Get user context capture config
            user_context_config = self.config.get('user_context', {})
            capture_user_context = user_context_config.get('enabled', True)

            class TracedRpcProxy:
                """Proxy wrapper that creates spans for RPC calls."""

                def __init__(self, original, timeout_value=None):
                    self._original = original
                    self._tracer = tracer
                    self._timeout = timeout_value
                    self._capture_payload = capture_payload
                    self._payload_config = payload_config
                    self._capture_user_context = capture_user_context

                def __getattr__(self, name):
                    original_method = getattr(self._original, name)

                    def traced_call(*args, **kwargs):
                        # Get current span for context linking
                        current_span = trace.get_current_span()
                        parent_context = trace.get_current_span().get_span_context() if current_span else None

                        # Build attributes with data type for OTEL Collector routing
                        attributes = {
                            'rpc.system': 'pylon',
                            'rpc.method': name,
                            'rpc.service': 'pylon-rpc',
                            TELEMETRY_DATA_TYPE_KEY: TelemetryDataType.RPC_CALLS,
                        }

                        # Add timeout if set
                        if self._timeout:
                            attributes['rpc.timeout'] = self._timeout

                        # Add argument summary (safely)
                        if args:
                            attributes['rpc.args_count'] = len(args)
                        if kwargs:
                            attributes['rpc.kwargs_keys'] = ','.join(kwargs.keys())

                        # Capture actual payload if enabled
                        if self._capture_payload:
                            try:
                                from .utils.payload_capture import get_payload_capture
                                capture = get_payload_capture(self._payload_config)
                                payload_attrs = capture.serialize_args(args, kwargs)
                                attributes.update(payload_attrs)
                            except Exception as e:
                                log.debug(f"Failed to capture RPC payload: {e}")

                        # Capture user context (from Flask g.auth, headers, or kwargs)
                        if self._capture_user_context:
                            try:
                                from .utils.user_context import extract_user_context, PROJECT_ID_ATTR
                                user_attrs = extract_user_context(
                                    from_flask=True,
                                    from_headers=True,
                                    kwargs=kwargs
                                )
                                # Extract project_id from Flask request.view_args if not in user context
                                if PROJECT_ID_ATTR not in user_attrs or user_attrs[PROJECT_ID_ATTR] is None:
                                    try:
                                        from flask import request as flask_request
                                        view_args = flask_request.view_args or {}
                                        pid = view_args.get('project_id')
                                        if pid is not None:
                                            user_attrs[PROJECT_ID_ATTR] = int(pid)
                                    except (ValueError, TypeError, RuntimeError):
                                        pass
                                attributes.update(user_attrs)
                            except Exception as e:
                                log.debug(f"Failed to capture user context in RPC: {e}")

                        # Extract entity context and project_id from RPC args/kwargs
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
                            # Also extract project_id from args/kwargs if not already set
                            if 'project.id' not in attributes:
                                pid = kwargs.get('chat_project_id') or kwargs.get('project_id')
                                if not pid:
                                    for source in sources:
                                        pid = source.get('project_id')
                                        if pid:
                                            break
                                if pid:
                                    try:
                                        attributes['project.id'] = int(pid)
                                    except (TypeError, ValueError):
                                        pass
                        except Exception as e:
                            log.debug(f"Failed to extract entity context in RPC: {e}")

                        # Determine target service from method name
                        if name.startswith('predict'):
                            attributes['rpc.target_service'] = 'pylon-predicts'
                        elif name.startswith('auth_'):
                            attributes['rpc.target_service'] = 'pylon-auth'
                        elif name.startswith('worker_'):
                            attributes['rpc.target_service'] = 'pylon-indexer'
                        else:
                            attributes['rpc.target_service'] = 'pylon-main'

                        # Create span for outgoing RPC call
                        start_time = time.perf_counter()
                        with self._tracer.start_as_current_span(
                            f"RPC {name}",
                            kind=SpanKind.CLIENT,
                            attributes=attributes
                        ) as span:
                            try:
                                result = original_method(*args, **kwargs)
                                duration_ms = (time.perf_counter() - start_time) * 1000
                                span.set_attribute('rpc.duration_ms', duration_ms)
                                span.set_status(Status(StatusCode.OK))
                                return result
                            except Exception as e:
                                duration_ms = (time.perf_counter() - start_time) * 1000
                                span.set_attribute('rpc.duration_ms', duration_ms)
                                span.set_attribute('rpc.error', str(e)[:200])
                                span.set_status(Status(StatusCode.ERROR, str(e)))
                                span.record_exception(e)
                                raise

                    return traced_call

            # Replace the call proxy with traced version
            rpc_manager.call = TracedRpcProxy(original_proxy)

            # Also wrap timeout proxy
            original_timeout = rpc_manager.timeout

            def traced_timeout(timeout_seconds):
                """Return a traced proxy with timeout."""
                timeout_proxy = original_timeout(timeout_seconds)
                return TracedRpcProxy(timeout_proxy, timeout_value=timeout_seconds)

            rpc_manager.timeout = traced_timeout

            log.info("RPC client instrumentation enabled for inter-service tracing")

            # === RPC SERVER-SIDE INSTRUMENTATION ===
            # Wrap method_manager to create SERVER spans for incoming RPC calls
            self._instrument_rpc_server(tracer)

        except Exception as e:
            log.warning(f"Failed to instrument RPC: {e}")

    def _instrument_rpc_server(self, tracer):
        """Instrument RPC server-side to create spans for incoming RPC calls.

        This wraps the method_manager's method dispatch to create SERVER spans
        when this pylon receives RPC calls from other services.
        """
        try:
            from opentelemetry.trace import SpanKind, Status, StatusCode
            from .core.data_types import TelemetryDataType, TELEMETRY_DATA_TYPE_KEY
            import time

            service_name = self.config.get('service', {}).get('name', 'pylon-main')

            # Check if method_manager exists (not all pylons have it)
            method_manager = getattr(self.context, 'method_manager', None)
            if not method_manager:
                log.info("method_manager not available, skipping RPC server instrumentation")
                return

            # Get the original call method
            original_call = method_manager.call

            class TracedMethodProxy:
                """Proxy that wraps method_manager.call to create SERVER spans."""

                def __init__(self, original):
                    self._original = original
                    self._tracer = tracer
                    self._service_name = service_name

                def __getattr__(self, name):
                    original_method = getattr(self._original, name)

                    def traced_method_call(*args, **kwargs):
                        # Build attributes
                        attributes = {
                            'rpc.system': 'pylon',
                            'rpc.method': name,
                            'rpc.service': self._service_name,
                            TELEMETRY_DATA_TYPE_KEY: TelemetryDataType.RPC_CALLS,
                        }

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
                            # Also extract project_id from args/kwargs if not already set
                            if 'project.id' not in attributes:
                                pid = kwargs.get('chat_project_id') or kwargs.get('project_id')
                                if not pid:
                                    for source in sources:
                                        pid = source.get('project_id')
                                        if pid:
                                            break
                                if pid:
                                    try:
                                        attributes['project.id'] = int(pid)
                                    except (TypeError, ValueError):
                                        pass
                        except Exception:
                            pass

                        # Create SERVER span for incoming RPC
                        start_time = time.perf_counter()
                        with self._tracer.start_as_current_span(
                            f"RPC {name}",
                            kind=SpanKind.SERVER,
                            attributes=attributes
                        ) as span:
                            try:
                                result = original_method(*args, **kwargs)
                                duration_ms = (time.perf_counter() - start_time) * 1000
                                span.set_attribute('rpc.duration_ms', duration_ms)
                                span.set_status(Status(StatusCode.OK))
                                return result
                            except Exception as e:
                                duration_ms = (time.perf_counter() - start_time) * 1000
                                span.set_attribute('rpc.duration_ms', duration_ms)
                                span.set_attribute('rpc.error', str(e)[:200])
                                span.set_status(Status(StatusCode.ERROR, str(e)))
                                span.record_exception(e)
                                raise

                    return traced_method_call

            # Replace method_manager.call with traced version
            method_manager.call = TracedMethodProxy(original_call)

            log.info("RPC server instrumentation enabled for incoming RPC calls")
        except Exception as e:
            log.warning(f"Failed to instrument RPC server: {e}")

    def _init_rpc_server_tracing(self):
        """Initialize RPC server tracing wrapper from tracer."""
        try:
            if not self._enabled or self.tracer is None:
                return

            from .utils.rpc_server_trace import create_rpc_server_wrapper

            service_name = self.config.get('service', {}).get('name', 'pylon-auth')
            payload_config = self.config.get('payload_capture', {})
            capture_payload = payload_config.get('enabled', True)
            user_context_config = self.config.get('user_context', {})
            capture_user_context = user_context_config.get('enabled', True)

            self._rpc_server_wrapper = create_rpc_server_wrapper(
                self.tracer,
                service_name=service_name,
                telemetry_data_type="rpc_server",
                capture_payload=capture_payload,
                payload_config=payload_config,
                capture_user_context=capture_user_context,
            )
            log.info("RPC server tracing wrapper initialized")
        except Exception as e:
            log.debug(f"RPC server tracing not available: {e}")

    def _patch_rpc_registration(self):
        """Patch arbiter RpcNode to auto-wrap registered RPC handlers with SERVER-side tracing.

        This enables incoming RPC calls to pylon-auth to create SERVER spans,
        making auth visible in distributed traces alongside pylon-main (CLIENT spans).
        """
        import arbiter
        import functools

        original_register = arbiter.RpcNode.register
        tracing_self = self

        def traced_register(rpc_node_self, handler, name=None, *args, **kwargs):
            """Wrapper around RpcNode.register that adds lazy tracing at execution time."""

            @functools.wraps(handler)
            def lazy_traced_handler(*handler_args, **handler_kwargs):
                """Execute handler with tracing if available (checked at execution time)."""
                # Try to get wrapper at execution time (tracing should be loaded by now)
                if tracing_self._rpc_server_wrapper is None:
                    tracing_self._init_rpc_server_tracing()

                if tracing_self._rpc_server_wrapper is not None:
                    try:
                        # Determine RPC name for the span
                        rpc_name = name if name else getattr(handler, '__name__', 'unknown_rpc')
                        # Apply tracing wrapper at execution time
                        traced_fn = tracing_self._rpc_server_wrapper(rpc_name)(handler)
                        return traced_fn(*handler_args, **handler_kwargs)
                    except Exception as e:
                        log.debug(f"RPC server tracing failed for {name}: {e}")

                # Fallback: execute without tracing
                return handler(*handler_args, **handler_kwargs)

            # Register the lazy wrapper instead of the original handler
            return original_register(rpc_node_self, lazy_traced_handler, name, *args, **kwargs)

        arbiter.RpcNode.register = traced_register
        log.info("RpcNode.register patched for execution-time RPC server tracing")

    def _instrument_logging(self):
        """Instrument Python logging to include trace context (trace_id, span_id).

        This adds trace context to log messages so they can be correlated
        with traces in observability backends like Dynatrace.

        Logs are routed through arbiter/EventNode -> logging_hub -> OTEL.
        """
        try:
            from .utils.trace_logging import instrument_pylon_logging

            # Get service name from config
            service_name = self.config.get('service', {}).get('name', 'pylon-main')

            if instrument_pylon_logging(service_name=service_name):
                log.info(f"Logging instrumentation enabled - logs will include trace context (service={service_name})")
            else:
                log.warning("Logging instrumentation failed")

            # Set up EventNode log routing to logging_hub
            self._setup_eventnode_logging(service_name)

        except Exception as e:
            log.warning(f"Failed to instrument logging: {e}")

    def _setup_eventnode_logging(self, service_name: str):
        """Set up EventNode log routing to logging_hub."""
        try:
            import arbiter
            from .utils.eventnode_handler import EventNodeLogHandler
            import logging as stdlib_logging

            # Get EventNode config
            event_node_config = self.config.get('event_node', {})

            # Check if it's a MockEventNode (disabled)
            if event_node_config.get("type", "MockEventNode") == "MockEventNode":
                log.info("EventNode log routing disabled (no config or MockEventNode)")
                return

            # Create event node
            self._event_node = arbiter.make_event_node(config=event_node_config)
            self._event_node.start()

            # Create handler
            default_labels = {
                'service': service_name,
                'container_name': service_name,
            }

            self._eventnode_handler = EventNodeLogHandler(
                event_node=self._event_node,
                service_name=service_name,
                default_labels=default_labels,
            )

            # Only log INFO and above to EventNode to reduce volume
            self._eventnode_handler.setLevel(stdlib_logging.INFO)

            # Add to root logger
            root_logger = stdlib_logging.getLogger()
            root_logger.addHandler(self._eventnode_handler)

            log.info(f"EventNode log routing enabled -> logging_hub (service={service_name})")

        except Exception as e:
            log.warning(f"Failed to set up EventNode log routing: {e}")

    # ========================
    # Audit Trail
    # ========================

    def _init_audit_trail(self, audit_config):
        """Initialize audit trail subsystem."""
        self._audit_config = audit_config
        self._audit_mode = audit_config.get('mode', 'writer')
        self._audit_enabled = True

        log.info(f"Audit trail enabled (mode={self._audit_mode})")

        if self._audit_mode == 'writer':
            # Writer mode (pylon_main): set up DB table and EventNode subscriber
            try:
                from .models.audit_event import AuditEvent  # noqa: F401
                from tools import db, auth, config as c

                db.get_shared_metadata().create_all(bind=db.engine)
                log.info("Audit trail DB table created/verified")

                # Register permission for audit trail admin API
                auth.register_permissions({
                    "permissions": ["models.admin.audit_trail.view"],
                    "recommended_roles": {
                        c.ADMINISTRATION_MODE: {"admin": True, "editor": False, "viewer": False},
                    }
                })
            except Exception as e:
                log.error(f"Failed to set up audit trail DB: {e}")
                self._audit_enabled = False
                return

            # Set up EventNode for receiving forwarded audit events from pylon_indexer
            try:
                self._setup_audit_event_node(subscriber=True)
            except Exception as e:
                log.warning(f"Audit trail EventNode setup failed (local-only mode): {e}")

        elif self._audit_mode == 'forwarder':
            # Forwarder mode (pylon_indexer): set up EventNode emitter
            try:
                self._setup_audit_event_node(subscriber=False)
            except Exception as e:
                log.warning(f"Audit trail EventNode forwarder setup failed: {e}")
                self._audit_enabled = False

    def _setup_audit_event_node(self, subscriber=False):
        """Set up EventNode for audit trail cross-pylon communication."""
        import arbiter

        # Try to clone config from existing event_node or datasources
        event_node_config = self.config.get('event_node', {})

        if event_node_config.get("type", "MockEventNode") == "MockEventNode":
            # Try to clone from datasources module
            module_manager = self.context.module_manager
            for module_name in ["datasources"]:
                if module_name in module_manager.modules:
                    cfg_module = module_manager.modules[module_name].module
                    clone_config = cfg_module.event_node.clone_config
                    if clone_config is None:
                        continue
                    event_node_config = clone_config.copy()
                    break

        if event_node_config.get("type", "MockEventNode") == "MockEventNode":
            raise ValueError("No usable EventNode config for audit trail")

        # Use a dedicated queue for audit events
        config_copy = event_node_config.copy()
        patch_map = {
            "EventNode": "event_queue",
            "RedisEventNode": "event_queue",
            "SocketIOEventNode": "room",
        }
        clone_type = config_copy.get("type", "")
        if clone_type in patch_map:
            config_copy[patch_map[clone_type]] = "audit_trail"

        self._audit_event_node = arbiter.make_event_node(config=config_copy)
        self._audit_event_node.start()

        if subscriber:
            self._audit_event_node.subscribe("audit_event", self._on_remote_audit_event)
            log.info("Audit trail EventNode subscriber started")
        else:
            log.info("Audit trail EventNode forwarder started")

    def _register_audit_processor(self):
        """Register AuditSpanProcessor on the TracerProvider."""
        if not self.tracer_provider:
            log.warning("Cannot register audit processor: no TracerProvider")
            return

        try:
            from .utils.audit_processor import AuditSpanProcessor

            if self._audit_mode == 'writer':
                write_fn = self._write_audit_event
            elif self._audit_mode == 'forwarder':
                write_fn = self._forward_audit_event
            else:
                log.warning(f"Unknown audit mode: {self._audit_mode}")
                return

            processor = AuditSpanProcessor(write_fn, config=self._audit_config)
            self.tracer_provider.add_span_processor(processor)
            log.info("Audit SpanProcessor registered with TracerProvider")
        except Exception as e:
            log.error(f"Failed to register audit SpanProcessor: {e}")

    @staticmethod
    def _resolve_audit_email(user_id):
        """Resolve user email from user_id using the shared service-level cache."""
        from .utils.user_context import resolve_user_email
        return resolve_user_email(user_id)

    def _write_audit_event(self, data):
        """Write an audit event dict to the database (pylon_main writer mode)."""
        try:
            # Resolve email if user_id present but email missing
            if data.get("user_id") and not data.get("user_email"):
                email = self._resolve_audit_email(data["user_id"])
                if email:
                    data["user_email"] = str(email)

            from pylon.core.tools import db_support
            from tools import db
            from .models.audit_event import AuditEvent

            db_support.create_local_session()
            try:
                with db.with_project_schema_session(None) as db_session:
                    event = AuditEvent(**{
                        k: v for k, v in data.items()
                        if hasattr(AuditEvent, k)
                    })
                    db_session.add(event)
                    db_session.commit()
            finally:
                db_support.close_local_session()
        except Exception as e:
            log.debug(f"Failed to write audit event: {e}")

    def _forward_audit_event(self, data):
        """Forward an audit event dict via EventNode (pylon_indexer forwarder mode)."""
        if not self._audit_event_node:
            return
        try:
            # Convert datetime to ISO string for serialization
            if data.get("timestamp"):
                data["timestamp"] = data["timestamp"].isoformat()
            self._audit_event_node.emit("audit_event", data)
        except Exception as e:
            log.debug(f"Failed to forward audit event: {e}")

    def _on_remote_audit_event(self, _, payload):
        """Handle audit events received from pylon_indexer via EventNode."""
        if isinstance(payload, dict):
            # Convert ISO string back to datetime
            if payload.get("timestamp") and isinstance(payload["timestamp"], str):
                from datetime import datetime
                try:
                    payload["timestamp"] = datetime.fromisoformat(payload["timestamp"])
                except (ValueError, TypeError):
                    payload.pop("timestamp", None)
            self._write_audit_event(payload)

    @property
    def enabled(self) -> bool:
        """Check if tracing is enabled."""
        return self._enabled

    def get_tracer(self):
        """Get the tracer instance. Returns None if tracing is disabled."""
        return self.tracer if self._enabled else None

    def get_config(self) -> dict:
        """Get tracing configuration."""
        return {
            'enabled': self._enabled,
            'otlp_endpoint': self.config.get('otlp', {}).get('endpoint'),
            'service_name': self.config.get('service', {}).get('name'),
            'sampling_enabled': self.config.get('sampling', {}).get('enabled', False),
            'sampling_rate': self.config.get('sampling', {}).get('rate', 1.0),
        }

    def get_task_wrapper(self):
        """
        Get a task wrapper factory for wrapping arbiter task handlers.

        This allows other plugins (like worker_core) to wrap their task
        handlers with tracing spans.

        Returns:
            A wrapper factory function, or None if tracing is disabled.

        Usage:
            wrap_task = tracing_module.get_task_wrapper()
            if wrap_task:
                handler = wrap_task("task_name")(handler)
            task_node.register_task(handler, "task_name")
        """
        if not self._enabled or not self.tracer:
            return None

        try:
            from .utils.task_wrapper import create_traced_task_wrapper
            service_name = self.config.get('service', {}).get('name', 'pylon-indexer')
            return create_traced_task_wrapper(self.tracer, service_name)
        except Exception as e:
            log.warning(f"Failed to create task wrapper: {e}")
            return None

    def wrap_task_handler(self, handler, task_name: str):
        """
        Wrap a task handler with tracing.

        Convenience method that wraps a handler directly.

        Args:
            handler: The task handler function
            task_name: Name of the task (used in span name)

        Returns:
            Wrapped handler with tracing, or original handler if tracing disabled
        """
        wrap_task = self.get_task_wrapper()
        if wrap_task:
            return wrap_task(task_name)(handler)
        return handler

    def deinit(self):
        """Cleanup tracing resources."""
        # Shutdown audit trail EventNode
        if self._audit_event_node:
            try:
                if self._audit_mode == 'writer':
                    self._audit_event_node.unsubscribe("audit_event", self._on_remote_audit_event)
                self._audit_event_node.stop()
            except Exception as e:
                log.warning(f"Error during audit EventNode shutdown: {e}")

        # Shutdown EventNode log handler
        if self._eventnode_handler:
            try:
                import logging as stdlib_logging
                root_logger = stdlib_logging.getLogger()
                root_logger.removeHandler(self._eventnode_handler)
                self._eventnode_handler.close()
            except Exception as e:
                log.warning(f"Error during EventNode handler shutdown: {e}")

        # Shutdown EventNode
        if self._event_node:
            try:
                self._event_node.stop()
            except Exception as e:
                log.warning(f"Error during EventNode shutdown: {e}")

        # Shutdown system metrics collector
        if self._system_metrics_collector:
            try:
                self._system_metrics_collector.stop()
            except Exception as e:
                log.warning(f"Error during system metrics shutdown: {e}")

        # Shutdown meter provider
        if self._enabled and self.meter_provider:
            log.info("Shutting down metrics...")
            try:
                self.meter_provider.force_flush()
                self.meter_provider.shutdown()
            except Exception as e:
                log.warning(f"Error during metrics shutdown: {e}")

        # Shutdown tracer provider
        if self._enabled and self.tracer_provider:
            log.info("Shutting down tracing...")
            try:
                self.tracer_provider.force_flush()
                self.tracer_provider.shutdown()
            except Exception as e:
                log.warning(f"Error during tracing shutdown: {e}")
