"""
Tracing methods exposed to other plugins.
"""

from typing import Optional, Dict, Any

from pylon.core.tools import web, log


class Method:
    """Methods for other plugins to interact with tracing."""

    @web.method('tracing_is_enabled')
    def is_enabled(self) -> bool:
        """
        Check if tracing is enabled.

        Returns:
            bool: True if tracing is enabled and initialized
        """
        return self.module.enabled

    @web.method('tracing_get_tracer')
    def get_tracer(self):
        """
        Get the OpenTelemetry tracer instance.

        Returns:
            Tracer instance or None if tracing is disabled
        """
        return self.module.get_tracer()

    @web.method('tracing_get_config')
    def get_config(self) -> Dict[str, Any]:
        """
        Get tracing configuration.

        Returns:
            dict: Current tracing configuration
        """
        return self.module.get_config()

    @web.method('tracing_start_span')
    def start_span(
        self,
        name: str,
        attributes: Optional[Dict[str, Any]] = None,
        kind: Optional[str] = None,
    ):
        """
        Start a new span.

        Args:
            name: Span name
            attributes: Optional span attributes
            kind: Span kind (client, server, producer, consumer, internal)

        Returns:
            Span context manager or None if tracing is disabled
        """
        tracer = self.module.get_tracer()
        if tracer is None:
            return None

        from opentelemetry.trace import SpanKind

        kind_map = {
            'client': SpanKind.CLIENT,
            'server': SpanKind.SERVER,
            'producer': SpanKind.PRODUCER,
            'consumer': SpanKind.CONSUMER,
            'internal': SpanKind.INTERNAL,
        }

        span_kind = kind_map.get(kind, SpanKind.INTERNAL) if kind else SpanKind.INTERNAL

        return tracer.start_as_current_span(
            name,
            attributes=attributes,
            kind=span_kind,
        )

    @web.method('tracing_get_current_trace_id')
    def get_current_trace_id(self) -> Optional[str]:
        """
        Get the current trace ID.

        Returns:
            str: Current trace ID or None
        """
        from ..utils.trace_context import get_current_trace_id
        return get_current_trace_id()

    @web.method('tracing_inject_headers')
    def inject_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """
        Inject trace context into outgoing request headers.

        Args:
            headers: Headers dict to modify

        Returns:
            Modified headers dict with trace context
        """
        from ..utils.trace_context import inject_trace_context
        return inject_trace_context(headers)

    @web.method('tracing_set_span_attributes')
    def set_span_attributes(self, attributes: Dict[str, Any]) -> None:
        """
        Set attributes on the current span.

        Args:
            attributes: Dict of attribute key-value pairs
        """
        from ..utils.trace_context import set_span_attributes
        set_span_attributes(attributes)

    @web.method('tracing_get_current_traceparent')
    def get_current_traceparent(self) -> Optional[str]:
        """
        Get the current W3C traceparent header value.

        Returns:
            str: W3C traceparent header value (e.g., "00-{trace_id}-{span_id}-01")
                 or None if no trace is active.
        """
        from ..utils.trace_context import get_current_traceparent
        return get_current_traceparent()

    @web.method('tracing_get_audit_callback')
    def get_audit_callback(self, user_id=None, user_email=None, project_id=None):
        """
        Return an AuditLangChainCallback instance for tool/LLM span creation.

        Used by indexer_worker as a fallback when Langfuse is not configured,
        ensuring tool calls and LLM calls always produce OTEL spans for the
        audit trail.

        Args:
            user_id: User ID to propagate to audit spans
            user_email: User email to propagate to audit spans
            project_id: Project ID to propagate to audit spans

        Returns:
            AuditLangChainCallback instance, or None if audit trail is disabled.
        """
        if not self.module.enabled:
            return None
        audit_config = self.module.config.get("audit_trail", {})
        if not audit_config.get("enabled", False):
            return None
        from ..utils.audit_langchain_callback import AuditLangChainCallback
        return AuditLangChainCallback(
            user_id=user_id,
            user_email=user_email,
            project_id=project_id,
        )
