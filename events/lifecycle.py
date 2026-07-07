"""Lifecycle event handlers - log pylon startup events to the audit trail.

Listens to bootstrap_runtime_info events and writes synthetic 'lifecycle' audit
events for pylon_started. This integrates with the existing audit trail rather
than requiring a separate platform_events table.

The entity_name is the stable node_name prefix (pylon-main, pylon-indexer, etc.),
not the volatile full pylon_id (which includes a UUID regenerated on each boot).
"""

from datetime import datetime, timezone

from pylon.core.tools import log, web


# Module-level seen set (persists across event calls)
_seen_pylons = set()


class Event:
    """Lifecycle event handlers for audit trail.

    Note: In pylon's event system, `self` is the Module instance directly.
    """

    @web.event("bootstrap_runtime_info")
    def _on_pylon_started(self, context, event, payload):
        """Log pylon_started to audit trail (first heartbeat only per pylon instance)."""
        _ = context, event

        if not self._audit_enabled:
            return

        if not isinstance(payload, dict):
            return

        pylon_id = payload.get("pylon_id")
        if not pylon_id:
            return

        if pylon_id in _seen_pylons:
            return

        _seen_pylons.add(pylon_id)

        try:
            audit_data = {
                "timestamp": datetime.now(timezone.utc),
                "event_type": "lifecycle",
                "action": "pylon_started",
                "entity_name": pylon_id,
                "is_error": False,
            }

            self._write_audit_event(audit_data)
            log.info("Logged pylon_started lifecycle event for %s", pylon_id)
        except Exception as e:
            log.warning("Failed to log pylon_started lifecycle event: %s", e)

    @web.event("bootstrap_runtime_info_prune")
    def _on_pylon_pruned(self, context, event, payload):
        """Remove pylon from seen set when pruned (allows re-logging on restart)."""
        _ = context, event

        if not isinstance(payload, dict):
            return

        pylon_id = payload.get("pylon_id")
        if pylon_id:
            _seen_pylons.discard(pylon_id)
