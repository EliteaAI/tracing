"""Security audit methods for in-process callers in other plugins (#6756, C-1009).

A plugin cannot import tracing's code, but it must record audit events in its own
database transaction. It reaches these through ``this.for_module("tracing").module``.
"""

from pylon.core.tools import web

MAX_REPORTED_ERRORS = 5


def _require_outbox(module):
    """The outbox exists only where ``_init_security_audit`` found the db tool: fail closed otherwise."""
    if not getattr(module, "_security_audit_available", False):
        raise RuntimeError("security audit outbox not available")


def _validated_event(event):
    """Validate against SecurityAuditEventV1. The error names fields and rules, never values."""
    from pydantic import ValidationError

    from ..models.security_audit_event import SecurityAuditEventV1

    if not isinstance(event, dict):
        raise ValueError("security audit event must be a dict")
    try:
        return SecurityAuditEventV1.model_validate(event)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'event'}: {error['msg']}"
            for error in exc.errors()[:MAX_REPORTED_ERRORS]
        )
        raise ValueError(f"invalid security audit event: {problems}") from None


class Method:
    """Security audit recording for other plugins."""

    @web.method('security_audit_record')
    def security_audit_record(self, session, event: dict) -> dict:
        """Add the outbox row to the caller's ``session``. Does not commit: the caller owns the transaction."""
        _require_outbox(self)
        from ..utils import security_audit

        row = security_audit.record_security_event(session, _validated_event(event))
        return {"event_id": row.event_id}

    @web.method('security_audit_record_autocommit')
    def security_audit_record_autocommit(self, event: dict) -> dict:
        """Record in its own transaction. ``event_id`` is None when the event was buffered/dropped by policy.

        Raises SecurityAuditUnavailable under fail_closed when the outbox cannot be written.
        """
        _require_outbox(self)
        from ..utils import security_audit

        recorded = security_audit.record_security_event_autocommit(_validated_event(event))
        return {"event_id": None if recorded is None else recorded.event_id}

    @web.method('security_audit_admission_allowed_local')
    def security_audit_admission_allowed_local(self) -> bool:
        """In-process admission signal (no Redis RPC): False only under fail_closed with the backlog over its limit."""
        _require_outbox(self)
        from ..utils import security_audit

        return security_audit.security_audit_admission_allowed()
