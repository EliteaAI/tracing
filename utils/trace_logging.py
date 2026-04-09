"""
Trace Context Logging Integration

Injects trace_id and span_id into Python log records for correlation
with distributed traces. Logs are routed through arbiter/EventNode
to logging_hub for OTEL export.
"""

import logging
from typing import Optional

from pylon.core.tools import log as pylon_log


class TraceContextLogFilter(logging.Filter):
    """
    Logging filter that adds trace context to log records.

    Adds trace_id and span_id from the current OpenTelemetry span context
    to each log record, enabling trace-log correlation.
    """

    def __init__(self, service_name: str = "pylon-main"):
        super().__init__()
        self.service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        """Add trace context and service name to log record."""
        # Add service name
        record.service_name = self.service_name

        try:
            from opentelemetry import trace

            # Get current span context
            span = trace.get_current_span()
            span_context = span.get_span_context()

            if span_context.is_valid:
                # Format as 32-char hex string (Dynatrace/W3C format)
                record.trace_id = format(span_context.trace_id, '032x')
                record.span_id = format(span_context.span_id, '016x')
            else:
                record.trace_id = "0" * 32
                record.span_id = "0" * 16
        except Exception:
            record.trace_id = "0" * 32
            record.span_id = "0" * 16

        return True


class TraceContextLogFormatter(logging.Formatter):
    """
    Logging formatter that includes trace context and service name in the message.

    Format: [timestamp] [level] [service=xxx] [trace_id=xxx span_id=yyy] message
    """

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        include_trace_context: bool = True,
        include_service_name: bool = True
    ):
        if fmt is None:
            parts = ["[%(asctime)s]", "[%(levelname)s]"]
            if include_service_name:
                parts.append("[service=%(service_name)s]")
            if include_trace_context:
                parts.append("[trace_id=%(trace_id)s span_id=%(span_id)s]")
            parts.append("%(message)s")
            fmt = " ".join(parts)

        super().__init__(fmt=fmt, datefmt=datefmt)


def setup_trace_logging() -> None:
    """
    Configure Python's root logger to include trace context.

    This function adds a TraceContextLogFilter to all existing handlers
    and sets up a formatter that includes trace_id and span_id.
    """
    root_logger = logging.getLogger()

    # Add filter to all existing handlers
    trace_filter = TraceContextLogFilter()
    for handler in root_logger.handlers:
        handler.addFilter(trace_filter)

    pylon_log.info("Trace context logging enabled - logs will include trace_id and span_id")


def instrument_pylon_logging(service_name: str = "pylon-main") -> bool:
    """
    Instrument pylon's logging to include trace context and service name.

    Args:
        service_name: Name of the service for log attribution

    Returns:
        True if instrumentation successful, False otherwise
    """
    try:
        # Get pylon's logger (usually the root logger or a specific one)
        logger = logging.getLogger()

        # Add trace context filter with service name
        trace_filter = TraceContextLogFilter(service_name=service_name)

        # Add filter to all handlers
        for handler in logger.handlers:
            handler.addFilter(trace_filter)

            # Update formatter if it's a stream handler
            if isinstance(handler, logging.StreamHandler):
                handler.setFormatter(TraceContextLogFormatter())

        # If no handlers, add a default one
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.addFilter(trace_filter)
            handler.setFormatter(TraceContextLogFormatter())
            logger.addHandler(handler)

        pylon_log.info(f"Pylon logging instrumented with trace context (service={service_name})")
        return True

    except Exception as e:
        pylon_log.warning(f"Failed to instrument pylon logging: {e}")
        return False
