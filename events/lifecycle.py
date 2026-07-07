"""Lifecycle event handlers - log pylon startup events to the audit trail.

Listens to bootstrap_runtime_info events and writes synthetic 'lifecycle' audit
events for pylon_started. This integrates with the existing audit trail rather
than requiring a separate platform_events table.
"""

from datetime import datetime, timezone

from pylon.core.tools import log, web


# Module-level seen set (persists across event calls)
_seen_pylons = set()

def _extract_node_name(pylon_id: str) -> str:
    """Extract short node name from pylon_id.

    pylon_id format: "pylon-{name}_{uuid}" -> returns "{name}"
    Examples:
        "pylon-main_abc123" -> "main"
        "pylon-indexer_def456" -> "indexer"
    """
    if not pylon_id:
        return "unknown"
    # Remove "pylon-" prefix if present
    name = pylon_id
    if name.startswith("pylon-"):
        name = name[6:]
    # Remove UUID suffix (after underscore)
    if "_" in name:
        name = name.rsplit("_", 1)[0]
    return name or "unknown"


def _format_plugin_versions(runtime_info: list) -> str:
    """Format plugin versions as comma-separated string.

    Returns: "admin:0.68, auth:0.29, bootstrap:0.19, ..."
    """
    if not runtime_info:
        return ""

    parts = []
    for plugin in sorted(runtime_info, key=lambda p: p.get("name", "")):
        name = plugin.get("name", "")
        version = plugin.get("local_version", "")
        if name and version:
            # Strip git hash suffix if present (e.g., "0.68 (abc1234)" -> "0.68")
            if " (" in version:
                version = version.split(" (")[0]
            parts.append(f"{name}:{version}")

    return ", ".join(parts)


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

        try:
            node_name = _extract_node_name(pylon_id)
            runtime_info = payload.get("runtime_info", [])
            plugin_versions = _format_plugin_versions(runtime_info)

            # Action format: pylon_started_<node> -> <plugin:version, ...>
            # Truncate to 512 chars (DB column limit) to prevent silent INSERT failures
            action = f"pylon_started_{node_name} -> {plugin_versions}" if plugin_versions else f"pylon_started_{node_name}"
            if len(action) > 512:
                action = action[:509] + "..."

            audit_data = {
                "timestamp": datetime.now(timezone.utc),
                "event_type": "lifecycle",
                "action": action,
                "entity_name": pylon_id,
                "is_error": False,
            }

            # Use correct write function based on audit mode (writer vs forwarder)
            if self._audit_mode == 'writer':
                self._write_audit_event(audit_data)
            elif self._audit_mode == 'forwarder':
                self._forward_audit_event(audit_data)
            else:
                log.warning("Unknown audit mode: %s", self._audit_mode)
                return

            # Mark as seen only after successful write/forward
            _seen_pylons.add(pylon_id)
            log.info("Logged pylon_started lifecycle event for %s (mode=%s)", pylon_id, self._audit_mode)
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
