"""
Telemetry data type definitions for attribute-based routing.

These data types are used as span/log attributes to enable the OTEL Collector
to route telemetry to different backends based on the `telemetry.data_type` attribute.

Example Collector routing:
    - api_traces  -> Dynatrace + Jaeger
    - db_queries  -> Dynatrace
    - llm_traces  -> Dynatrace
    - logs        -> Elasticsearch + Cloud Logging
    - rpc_calls   -> Jaeger
"""

from typing import Optional
from opentelemetry import trace


class TelemetryDataType:
    """Constants for telemetry data type categorization."""

    # API request/response traces (endpoint_metrics decorator)
    API_TRACES = "api_traces"

    # Database query traces (SQLAlchemy instrumentation)
    DB_QUERIES = "db_queries"

    # LLM/AI model traces (Langfuse callback handler)
    # Note: Langfuse already sets this via OTEL, no changes needed in SDK
    LLM_TRACES = "llm_traces"

    # Application logs (logging_hub)
    LOGS = "logs"

    # RPC calls between services
    RPC_CALLS = "rpc_calls"

    # HTTP client requests (outgoing)
    HTTP_CLIENT = "http_client"

    # Socket.IO events (WebSocket communication)
    SOCKET_IO = "socket_io"

    @classmethod
    def all_types(cls) -> list[str]:
        """Return all available data types."""
        return [
            cls.API_TRACES,
            cls.DB_QUERIES,
            cls.LLM_TRACES,
            cls.LOGS,
            cls.RPC_CALLS,
            cls.HTTP_CLIENT,
            cls.SOCKET_IO,
        ]


# Attribute key used for routing in OTEL Collector
TELEMETRY_DATA_TYPE_KEY = "telemetry.data_type"


def tag_span_with_data_type(
    span: Optional[trace.Span] = None,
    data_type: str = TelemetryDataType.API_TRACES
) -> None:
    """
    Tag a span with a data type attribute for Collector routing.

    Args:
        span: The span to tag. If None, uses the current active span.
        data_type: One of TelemetryDataType constants.

    Example:
        with tracer.start_as_current_span("my_operation") as span:
            tag_span_with_data_type(span, TelemetryDataType.API_TRACES)
    """
    if span is None:
        span = trace.get_current_span()

    if span and span.is_recording():
        span.set_attribute(TELEMETRY_DATA_TYPE_KEY, data_type)


def get_current_data_type() -> Optional[str]:
    """Get the data type from the current active span, if set."""
    span = trace.get_current_span()
    if span and hasattr(span, 'attributes'):
        # Note: Reading attributes from a span is not always possible
        # depending on the span implementation
        return None  # Spans don't expose attributes for reading
    return None
