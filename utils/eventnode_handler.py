"""
EventNode Log Handler

Routes Python logs through arbiter/EventNode to logging_hub for centralized
log collection and OTEL export.
"""

import logging
import socket
import time
from typing import Optional, Dict, Any

from pylon.core.tools import log as pylon_log

# Cache hostname to avoid repeated syscalls
_HOSTNAME = socket.gethostname()


class EventNodeLogHandler(logging.Handler):
    """
    Logging handler that routes logs through EventNode to logging_hub.

    This enables centralized log collection from all pylon services through
    the arbiter/Redis infrastructure, which then exports to OTEL Collector.
    """

    def __init__(
        self,
        event_node,
        service_name: str = "pylon-main",
        default_labels: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize EventNode log handler.

        Args:
            event_node: arbiter EventNode instance
            service_name: Name of the service for log attribution
            default_labels: Default labels to attach to all logs
        """
        super().__init__()
        self.event_node = event_node
        self.service_name = service_name
        self.default_labels = default_labels or {}
        self._initialized = event_node is not None

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a log record to EventNode."""
        if not self._initialized or not self.event_node:
            return

        try:
            # Format the message
            try:
                msg = self.format(record) if self.formatter else record.getMessage()
            except Exception:
                msg = record.getMessage()

            # Build labels
            labels = self.default_labels.copy()
            labels.update({
                'service': self.service_name,
                'service_name': self.service_name,
                'hostname': _HOSTNAME,
                'host.name': _HOSTNAME,
                'level': record.levelname,
                'logger': record.name,
            })

            # Add trace context if available on the record
            if hasattr(record, 'trace_id'):
                labels['trace_id'] = record.trace_id
            if hasattr(record, 'span_id'):
                labels['span_id'] = record.span_id

            # Build log data
            log_data = {
                "records": [{
                    "line": msg,
                    "time": record.created,
                    "labels": labels,
                }],
            }

            # Emit to EventNode
            self.event_node.emit("log_data", log_data)

        except Exception:
            # Don't raise errors during logging
            pass

    def close(self) -> None:
        """Close the handler."""
        super().close()


def create_eventnode_handler(
    event_node_config: Dict[str, Any],
    service_name: str = "pylon-main"
) -> Optional[EventNodeLogHandler]:
    """
    Create an EventNode log handler from configuration.

    Args:
        event_node_config: EventNode configuration dict
        service_name: Name of the service for log attribution

    Returns:
        EventNodeLogHandler instance or None if creation fails
    """
    try:
        import arbiter

        # Check if it's a MockEventNode (disabled)
        if event_node_config.get("type", "MockEventNode") == "MockEventNode":
            pylon_log.info("EventNode log routing disabled (MockEventNode)")
            return None

        # Create event node
        event_node = arbiter.make_event_node(config=event_node_config)
        event_node.start()

        # Create handler with default labels
        default_labels = {
            'service': service_name,
            'hostname': event_node_config.get('event_queue', 'unknown'),
        }

        handler = EventNodeLogHandler(
            event_node=event_node,
            service_name=service_name,
            default_labels=default_labels,
        )

        # Store event_node reference for shutdown
        handler._event_node_owned = True

        pylon_log.info(f"EventNode log handler created for {service_name}")
        return handler

    except Exception as e:
        pylon_log.warning(f"Failed to create EventNode log handler: {e}")
        return None
