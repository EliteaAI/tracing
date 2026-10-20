"""Security audit recording, backpressure policy and in-process metrics (#6945, C-1009).

``record_security_event`` adds the outbox row to the caller's own session so the audit
row commits atomically with the security mutation it describes. Delivery to a sink is
the relay's job (``security_audit_relay``).

Nothing consumes ``security_audit_admission_allowed`` yet: admission wiring is #6756/#6758.

The read side (``query_security_events``, ``summarize_security_events``) backs the
administration endpoints (#7120): bounded pages, a sort whitelist, parameterized filters.
"""

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Mapping, Optional, Tuple

from pylon.core.tools import log
from sqlalchemy import func, select, text

from ..models.security_audit_event import MAX_STRING_LENGTH, SecurityAuditEventV1
from ..models.security_audit_outbox import SecurityAuditEvent, DELIVERY_PENDING

FAIL_CLOSED = "fail_closed"
BUFFER = "buffer"
FAILURE_POLICIES = frozenset({FAIL_CLOSED, BUFFER})
BACKLOG_CACHE_TTL_S = 5.0

# Read side limits (#7120)
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
MAX_SINCE_HOURS = 168
MAX_SUMMARY_GROUPS = 500
DEFAULT_SORT = "occurred_at"
# Whitelist of sortable columns: a request value is never used as a column name directly.
SORT_FIELDS = frozenset({"occurred_at", "event_type", "decision", "reason_code", "project_id"})
_STRING_FILTERS = ("agent_binding_ref", "credential_ref", "user_ref", "event_type", "reason_code")


class SecurityAuditUnavailable(RuntimeError):
    """The audit row could not be recorded and the policy is fail_closed."""


class Counter:
    """In-process monotonic counter (prometheus_client is not available in plugins)."""

    def __init__(self):
        self._value = 0
        self._lock = threading.Lock()

    def inc(self, amount: int = 1) -> None:
        with self._lock:
            self._value += amount

    def total(self) -> int:
        return self._value


# Events lost because the outbox itself could not be written. Never increments under fail_closed.
security_audit_drop_total = Counter()


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


# ========================
# Schema upgrade (#7120)
# ========================

def ensure_would_deny_column(engine, schema: str) -> None:
    """Add ``would_deny`` and the ``occurred_at`` index to an outbox created before #7120.

    ``create_all`` never alters an existing table, and the model now names the column in every
    INSERT, so a table without it would refuse every audit write. Idempotent; the same two
    statements are in ``migrations/006_add_would_deny_to_security_audit_events.sql``.
    """
    table = '"%s".security_audit_events' % schema.replace('"', '""')
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS would_deny BOOLEAN"))
        connection.execute(text(
            f"CREATE INDEX IF NOT EXISTS ix_security_audit_events_occurred ON {table} (occurred_at)"))


# ========================
# Read side (#7120)
# ========================

class InvalidQuery(ValueError):
    """A request parameter is malformed. The message is safe to return to the caller."""


@dataclass(frozen=True)
class SecurityEventFilters:
    project_id: Optional[int] = None
    agent_binding_ref: Optional[str] = None
    credential_ref: Optional[str] = None
    user_ref: Optional[str] = None
    event_type: Optional[str] = None
    reason_code: Optional[str] = None
    would_deny: Optional[bool] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None


@dataclass(frozen=True)
class Page:
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    sort_by: str = DEFAULT_SORT
    descending: bool = True


def _int_arg(args: Mapping, name: str, *, minimum: int, maximum: Optional[int] = None,
             default: Optional[int] = None, clamp: bool = False) -> Optional[int]:
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise InvalidQuery(f"{name} must be an integer") from None
    if value < minimum:
        raise InvalidQuery(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        if clamp:
            return maximum
        raise InvalidQuery(f"{name} must be at most {maximum}")
    return value


def _datetime_arg(args: Mapping, name: str) -> Optional[datetime]:
    raw = args.get(name)
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise InvalidQuery(f"{name} must be an ISO 8601 datetime") from None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def parse_would_deny(args: Mapping) -> Optional[bool]:
    raw = args.get("would_deny")
    if raw is None or raw == "":
        return None
    if str(raw).lower() in ("true", "false"):
        return str(raw).lower() == "true"
    raise InvalidQuery("would_deny must be true or false")


def parse_filters(args: Mapping) -> SecurityEventFilters:
    values = {"project_id": _int_arg(args, "project_id", minimum=0),
              "would_deny": parse_would_deny(args),
              "date_from": _datetime_arg(args, "date_from"),
              "date_to": _datetime_arg(args, "date_to")}
    for name in _STRING_FILTERS:
        raw = (args.get(name) or "").strip()
        if len(raw) > MAX_STRING_LENGTH:
            raise InvalidQuery(f"{name} is longer than {MAX_STRING_LENGTH} characters")
        values[name] = raw or None
    return SecurityEventFilters(**values)


def parse_page(args: Mapping) -> Page:
    """``limit`` is capped at MAX_LIMIT, an unknown ``sort_by`` falls back to the default."""
    sort_by = args.get("sort_by")
    return Page(
        limit=_int_arg(args, "limit", minimum=1, maximum=MAX_LIMIT, default=DEFAULT_LIMIT, clamp=True),
        offset=_int_arg(args, "offset", minimum=0, default=0),
        sort_by=sort_by if sort_by in SORT_FIELDS else DEFAULT_SORT,
        descending=str(args.get("sort_order", "desc")).lower() != "asc",
    )


def parse_since_hours(args: Mapping, default: int = 24) -> int:
    return _int_arg(args, "since_hours", minimum=1, maximum=MAX_SINCE_HOURS, default=default)


def _conditions(filters: SecurityEventFilters) -> list:
    row = SecurityAuditEvent
    conditions = []
    for name in ("project_id", "agent_binding_ref", "credential_ref", "user_ref", "event_type",
                 "reason_code", "would_deny"):
        value = getattr(filters, name)
        if value is not None:
            conditions.append(getattr(row, name) == value)
    if filters.date_from is not None:
        conditions.append(row.occurred_at >= filters.date_from)
    if filters.date_to is not None:
        conditions.append(row.occurred_at <= filters.date_to)
    return conditions


def event_to_dict(row) -> dict:
    """The v1 fields of an outbox row, JSON-ready. Outbox delivery state is not exposed."""
    values = {name: getattr(row, name, None) for name in SecurityAuditEventV1.model_fields}
    if values["occurred_at"] is not None:
        values["occurred_at"] = values["occurred_at"].isoformat()
    return values


def query_security_events(filters: SecurityEventFilters, page: Page, *,
                          session_scope=None) -> Tuple[int, List[dict]]:
    """One page of events plus the total matching ``filters``."""
    if page.sort_by not in SORT_FIELDS:
        raise InvalidQuery("sort_by is not sortable")
    row = SecurityAuditEvent
    conditions = _conditions(filters)
    column = getattr(row, page.sort_by)
    ordering = (column.desc(), row.id.desc()) if page.descending else (column.asc(), row.id.asc())
    with (session_scope or default_session_scope)() as session:
        total = session.execute(select(func.count()).select_from(row).where(*conditions)).scalar_one()
        rows = session.execute(
            select(row).where(*conditions).order_by(*ordering).offset(page.offset).limit(page.limit)
        ).scalars().all()
        return total, [event_to_dict(r) for r in rows]


def summarize_security_events(since_hours: int, would_deny: Optional[bool] = None, *,
                              now: Optional[datetime] = None, session_scope=None) -> List[dict]:
    """Event counts per (decision, would_deny, reason_code) over the last ``since_hours``."""
    row = SecurityAuditEvent
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=since_hours)
    conditions = [row.occurred_at >= since]
    if would_deny is not None:
        conditions.append(row.would_deny == would_deny)
    count = func.count().label("count")
    statement = (
        select(row.decision, row.would_deny, row.reason_code, count)
        .where(*conditions)
        .group_by(row.decision, row.would_deny, row.reason_code)
        .order_by(count.desc(), row.reason_code, row.decision, row.would_deny)
        .limit(MAX_SUMMARY_GROUPS)
    )
    with (session_scope or default_session_scope)() as session:
        return [{"decision": d, "would_deny": w, "reason_code": r, "count": n}
                for d, w, r, n in session.execute(statement).all()]
