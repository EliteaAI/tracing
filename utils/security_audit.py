"""Security audit recording, backpressure policy and in-process metrics (#6945, C-1009).

``record_security_event`` adds the outbox row to the caller's own session so the audit
row commits atomically with the security mutation it describes. Delivery to a sink is
the relay's job (``security_audit_relay``).

Nothing consumes ``security_audit_admission_allowed`` yet: admission wiring is #6756/#6758.
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pylon.core.tools import log
from sqlalchemy import func, select

from ..models.security_audit_event import SecurityAuditEventV1
from ..models.security_audit_outbox import SecurityAuditEvent, DELIVERY_PENDING
from .security_metrics import SECURITY_METRICS

FAIL_CLOSED = "fail_closed"
BUFFER = "buffer"
FAILURE_POLICIES = frozenset({FAIL_CLOSED, BUFFER})
BACKLOG_CACHE_TTL_S = 5.0


class SecurityAuditUnavailable(RuntimeError):
    """The audit row could not be recorded and the policy is fail_closed."""


# Events lost because the outbox itself could not be written. Never increments under fail_closed.
security_audit_drop_total = SECURITY_METRICS.counter(
    "security_audit_drop_total", "Security audit events lost because the outbox could not be written")


@dataclass(frozen=True)
class SecurityAuditSettings:
    failure_policy: str = BUFFER
    backlog_limit: int = 10000
    relay_enabled: bool = False
    interval_s: float = 5
    batch_size: int = 100
    max_attempts: int = 10
    sink_path: Optional[str] = None

    @classmethod
    def from_config(cls, config: Optional[dict]) -> "SecurityAuditSettings":
        config = config or {}
        relay = config.get("relay") or {}
        sink = relay.get("sink") or {}
        defaults = cls()
        settings = cls(
            failure_policy=config.get("failure_policy", defaults.failure_policy),
            backlog_limit=config.get("backlog_limit", defaults.backlog_limit),
            relay_enabled=bool(relay.get("enabled", defaults.relay_enabled)),
            interval_s=relay.get("interval_s", defaults.interval_s),
            batch_size=relay.get("batch_size", defaults.batch_size),
            max_attempts=relay.get("max_attempts", defaults.max_attempts),
            sink_path=sink.get("path"),
        )
        if settings.failure_policy not in FAILURE_POLICIES:
            raise ValueError(f"security_audit.failure_policy must be one of {sorted(FAILURE_POLICIES)}")
        for name in ("backlog_limit", "interval_s", "batch_size", "max_attempts"):
            if not getattr(settings, name) > 0:
                raise ValueError(f"security_audit {name} must be positive")
        if settings.relay_enabled:
            if sink.get("type", "jsonl") != "jsonl":
                raise ValueError("security_audit.relay.sink.type must be 'jsonl'")
            if not settings.sink_path:
                raise ValueError("security_audit.relay.sink.path is required when the relay is enabled")
        return settings


_settings = SecurityAuditSettings()
_backlog_cache = {"checked_at": None, "exceeded": False}


def configure(settings: SecurityAuditSettings) -> None:
    global _settings
    _settings = settings
    _backlog_cache.update(checked_at=None, exceeded=False)


def get_settings() -> SecurityAuditSettings:
    return _settings


@contextmanager
def default_session_scope():
    """Project-schema session, opened the way the audit-trail writer opens it."""
    from pylon.core.tools import db_support
    from tools import db

    db_support.create_local_session()
    try:
        with db.with_project_schema_session(None) as session:
            yield session
    finally:
        db_support.close_local_session()


def _as_event(event) -> SecurityAuditEventV1:
    if isinstance(event, SecurityAuditEventV1):
        return event
    return SecurityAuditEventV1.model_validate(event)


def record_security_event(session, event) -> SecurityAuditEvent:
    """Add the outbox row to ``session``. Does not commit: the caller's transaction owns it."""
    row = SecurityAuditEvent(**_as_event(event).model_dump())
    session.add(row)
    return row


def record_security_event_autocommit(event, *, session_scope=None) -> Optional[SecurityAuditEventV1]:
    """Record in its own transaction, for callers that have none.

    An invalid event raises (caller bug). If the outbox cannot be written, fail_closed
    raises SecurityAuditUnavailable; buffer counts a drop and returns None.
    """
    event = _as_event(event)
    try:
        with (session_scope or default_session_scope)() as session:
            record_security_event(session, event)
            session.commit()
        return event
    except Exception as exc:
        if _settings.failure_policy == FAIL_CLOSED:
            raise SecurityAuditUnavailable("security audit event could not be recorded") from exc
        security_audit_drop_total.inc()
        log.error("security audit: event %s dropped, outbox write failed: %s", event.event_id, exc)
        return None


def _count_pending() -> int:
    with default_session_scope() as session:
        return session.execute(_pending_count_query()).scalar_one()


def _pending_count_query():
    return select(func.count()).select_from(SecurityAuditEvent).where(
        SecurityAuditEvent.delivery_state == DELIVERY_PENDING)


def audit_backlog_exceeded(*, count_pending=None, clock=time.monotonic) -> bool:
    """True when pending outbox rows exceed backlog_limit. Cached for BACKLOG_CACHE_TTL_S.

    If the count cannot be read the backlog is unknown and reported as exceeded.
    """
    now = clock()
    checked_at = _backlog_cache["checked_at"]
    if checked_at is not None and now - checked_at < BACKLOG_CACHE_TTL_S:
        return _backlog_cache["exceeded"]
    try:
        exceeded = (count_pending or _count_pending)() > _settings.backlog_limit
    except Exception as exc:
        log.warning("security audit: backlog unreadable, treating as exceeded: %s", exc)
        exceeded = True
    _backlog_cache.update(checked_at=now, exceeded=exceeded)
    return exceeded


def security_audit_admission_allowed(**probe) -> bool:
    """False only under fail_closed with the backlog over its limit."""
    return not (_settings.failure_policy == FAIL_CLOSED and audit_backlog_exceeded(**probe))


def security_audit_metrics(*, session_scope=None, now: Optional[datetime] = None) -> dict:
    """Outbox depth, age of the last delivered row, and dropped-event total."""
    now = now or datetime.now(timezone.utc)
    with (session_scope or default_session_scope)() as session:
        depth = session.execute(_pending_count_query()).scalar_one()
        last_delivered = session.execute(select(func.max(SecurityAuditEvent.delivered_at))).scalar_one()
    return {
        "security_audit_outbox_depth": depth or 0,
        "security_audit_delivery_lag_seconds": (
            None if last_delivered is None else (now - last_delivered).total_seconds()),
        "security_audit_drop_total": security_audit_drop_total.total(),
    }


def register_audit_gauges(security_metrics=SECURITY_METRICS) -> None:
    """Export outbox depth and delivery lag as observable gauges (read on each export)."""
    security_metrics.gauge_callback(
        "security_audit_outbox_depth", "Pending security audit outbox rows",
        lambda: security_audit_metrics()["security_audit_outbox_depth"])
    security_metrics.gauge_callback(
        "security_audit_delivery_lag_seconds", "Age of the last delivered security audit row",
        lambda: security_audit_metrics()["security_audit_delivery_lag_seconds"], unit="s")
