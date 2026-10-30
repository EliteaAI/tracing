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
from ..models.security_audit_outbox import SecurityAuditEvent, DELIVERY_FAILED, DELIVERY_PENDING
from .security_metrics import SECURITY_METRICS

FAIL_CLOSED = "fail_closed"
BUFFER = "buffer"
FAILURE_POLICIES = frozenset({FAIL_CLOSED, BUFFER})
BACKLOG_CACHE_TTL_S = 5.0
# Rows the relay has not delivered: still pending, or failed after max_attempts (never purged).
UNDELIVERED_STATES = (DELIVERY_PENDING, DELIVERY_FAILED)
# The two outbox gauges are read one after the other in one export; they share one reading.
GAUGE_SNAPSHOT_TTL_S = 1.0

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
    """Undelivered outbox depth, age of the oldest undelivered row, and dropped-event total.

    The age is 0 when nothing is undelivered, so it does not grow on an idle outbox and does grow
    when the relay is dead. One statement; both subqueries filter on ``UNDELIVERED_STATES``, which is
    the predicate of the partial index ``ix_security_audit_events_undelivered`` (``ensure_undelivered_index``).
    """
    now = now or datetime.now(timezone.utc)
    undelivered = SecurityAuditEvent.delivery_state.in_(UNDELIVERED_STATES)
    depth_query = select(func.count()).select_from(SecurityAuditEvent).where(undelivered).scalar_subquery()
    oldest_query = select(func.min(SecurityAuditEvent.occurred_at)).where(undelivered).scalar_subquery()
    with (session_scope or default_session_scope)() as session:
        depth, oldest = session.execute(select(depth_query, oldest_query)).one()
    return {
        "security_audit_outbox_depth": depth or 0,
        "security_audit_delivery_lag_seconds": 0.0 if oldest is None else max((now - oldest).total_seconds(), 0.0),
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


def undelivered_predicate_sql() -> str:
    """``delivery_state IN ('pending', 'failed')``: the gauge filter and the partial index predicate."""
    return "delivery_state IN (%s)" % ", ".join("'%s'" % state for state in UNDELIVERED_STATES)


def ensure_undelivered_index(engine, schema: str) -> None:
    """Partial index for the outbox gauges: ``min(occurred_at)`` and ``count(*)`` over undelivered rows.

    ``ix_security_audit_events_delivery`` is ``(delivery_state, next_attempt_at)`` and cannot answer
    ``min(occurred_at)``; delivered rows are never purged, so without this the gauge query would walk them.
    PostgreSQL only (partial index); idempotent; the same statement is in
    ``migrations/007_add_undelivered_index_to_security_audit_events.sql``.
    """
    if getattr(getattr(engine, "dialect", None), "name", None) != "postgresql":
        return
    table = '"%s".security_audit_events' % schema.replace('"', '""')
    with engine.begin() as connection:
        connection.execute(text(
            f"CREATE INDEX IF NOT EXISTS ix_security_audit_events_undelivered ON {table} (occurred_at) "
            f"WHERE {undelivered_predicate_sql()}"))


SUMMARY_INDEX = "ix_security_audit_events_summary"
SUMMARY_INDEX_LOCK_TIMEOUT = "5s"
_INDEX_VALID = text(
    "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relname = :name AND n.nspname = :schema")


def ensure_summary_index(engine, schema: str) -> Optional[bool]:
    """Covering index of the operator summary (#7294): ``summarize_security_events`` groups the window's rows by
    decision, would_deny and reason_code; with them in the index the window is read from the index alone, not from
    every row of it. PostgreSQL only (``INCLUDE``).

    Built ``CONCURRENTLY`` (autocommit): the outbox is never purged and takes every governed audit write, which a plain
    build would block for its whole duration. Waits at most ``SUMMARY_INDEX_LOCK_TIMEOUT`` for its locks. An index left
    invalid (an interrupted or timed-out build) is reported and left to
    ``migrations/008_add_summary_index_to_security_audit_events.sql`` (drop it, run the file). Returns whether a valid
    index is there (None outside PostgreSQL)."""
    if getattr(getattr(engine, "dialect", None), "name", None) != "postgresql":
        return None
    table = '"%s".security_audit_events' % schema.replace('"', '""')
    params = {"name": SUMMARY_INDEX, "schema": schema}
    with engine.connect() as connection:
        connection = connection.execution_options(isolation_level="AUTOCOMMIT")
        valid = connection.execute(_INDEX_VALID, params).scalar()
        if valid is None:
            connection.execute(text(f"SET lock_timeout = '{SUMMARY_INDEX_LOCK_TIMEOUT}'"))
            try:
                connection.execute(text(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {SUMMARY_INDEX} ON {table} (occurred_at) "
                    "INCLUDE (decision, would_deny, reason_code)"))
            except Exception as exc:  # pylint: disable=W0703  # e.g. the lock timeout: what it left is read below
                log.warning("Security audit summary index build stopped (%s)", type(exc).__name__)
            finally:
                connection.execute(text("RESET lock_timeout"))
            valid = connection.execute(_INDEX_VALID, params).scalar()
    if valid is not True:
        log.warning("Security audit summary index %s is not valid (or another replica is still building it); if it "
                    "stays invalid, drop it and run migration 008_add_summary_index_to_security_audit_events.sql",
                    SUMMARY_INDEX)
    return valid is True


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


def register_audit_gauges(security_metrics=SECURITY_METRICS, clock=time.monotonic) -> None:
    """Export outbox depth and delivery lag as observable gauges (read on each export).

    Both gauges are served from one ``security_audit_metrics()`` reading (one query), refreshed
    after ``GAUGE_SNAPSHOT_TTL_S``.
    """
    lock = threading.Lock()
    cached = {"at": None, "snapshot": None}

    def snapshot() -> dict:
        with lock:
            now = clock()
            if cached["at"] is None or now - cached["at"] >= GAUGE_SNAPSHOT_TTL_S:
                cached["snapshot"] = security_audit_metrics()
                cached["at"] = now
            return cached["snapshot"]

    security_metrics.gauge_callback(
        "security_audit_outbox_depth", "Undelivered (pending or failed) security audit outbox rows",
        lambda: snapshot()["security_audit_outbox_depth"])
    security_metrics.gauge_callback(
        "security_audit_delivery_lag_seconds", "Age of the oldest undelivered security audit row (0 when none)",
        lambda: snapshot()["security_audit_delivery_lag_seconds"], unit="s")
