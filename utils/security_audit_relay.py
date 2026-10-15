"""Relay from the security audit outbox to a sink (#6945, C-1009).

Polls ``security_audit_events`` for pending rows that are due, delivers them as OCSF-shaped
objects, and marks them delivered, or schedules a retry with full-jitter exponential
backoff, or marks them failed after ``max_attempts``. Rows are claimed with
``FOR UPDATE SKIP LOCKED`` so several relays can run side by side.

Delivery is at-least-once: if the process dies between the sink write and the commit, the
rows are delivered again. Consumers deduplicate on ``metadata.uid`` (the event_id).
"""

import json
import os
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from pylon.core.tools import log
from sqlalchemy import select

from ..models.security_audit_event import SecurityAuditEventV1
from ..models.security_audit_outbox import (
    SecurityAuditEvent, DELIVERY_PENDING, DELIVERY_DELIVERED, DELIVERY_FAILED,
)
from .security_audit import SecurityAuditSettings, default_session_scope

BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 300.0
OCSF_VERSION = "1.3.0"
_V1_FIELDS = tuple(SecurityAuditEventV1.model_fields)
# Fields carried in dedicated OCSF attributes rather than under "unmapped".
_OCSF_MAPPED = {"event_id", "event_type", "occurred_at", "ocsf_class", "decision", "outcome",
                "reason_code", "traceparent", "user_ref"}


def backoff_seconds(attempts: int, base: float = BACKOFF_BASE_S, cap: float = BACKOFF_CAP_S,
                    rng: Callable[[], float] = random.random) -> float:
    """Full jitter: uniform(0, min(cap, base * 2**(attempts-1))) after ``attempts`` failures."""
    return rng() * min(cap, base * 2 ** (attempts - 1))


def to_ocsf(row) -> dict:
    """Map an outbox row (or any object with the v1 attributes) to an OCSF-shaped dict."""
    values = {name: getattr(row, name, None) for name in _V1_FIELDS}
    ocsf_class = values["ocsf_class"]
    event = {
        "class_uid": ocsf_class,
        "category_uid": ocsf_class // 1000,
        "time": int(values["occurred_at"].timestamp() * 1000),
        "activity_name": values["event_type"],
        "status": values["outcome"] or values["decision"],
        "status_code": values["reason_code"],
        "metadata": {
            "uid": values["event_id"],
            "version": OCSF_VERSION,
            "product": {"name": "elitea", "vendor_name": "Elitea"},
            "correlation_uid": values["traceparent"],
        },
        "actor": {"user": {"uid": values["user_ref"]}},
        "unmapped": {k: v for k, v in values.items() if v is not None and k not in _OCSF_MAPPED},
    }
    return event


class JsonLinesFileSink:
    """Appends one JSON object per line. Each batch is written with a single write call."""

    def __init__(self, path: str):
        self.path = path

    def deliver(self, events: List[dict]) -> None:
        payload = "".join(
            json.dumps(e, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n" for e in events)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())


@dataclass
class RelayResult:
    delivered: int = 0
    retried: int = 0
    failed: int = 0


def due_events_query(now: datetime, batch_size: int):
    return (
        select(SecurityAuditEvent)
        .where(SecurityAuditEvent.delivery_state == DELIVERY_PENDING,
               SecurityAuditEvent.next_attempt_at <= now)
        .order_by(SecurityAuditEvent.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )


def relay_once(session, sink, settings: SecurityAuditSettings, *,
               now: Optional[datetime] = None, rng: Callable[[], float] = random.random) -> RelayResult:
    """Claim one batch, deliver it, update row state. The caller commits."""
    now = now or datetime.now(timezone.utc)
    rows = session.execute(due_events_query(now, settings.batch_size)).scalars().all()
    result = RelayResult()
    if not rows:
        return result
    try:
        sink.deliver([to_ocsf(row) for row in rows])
    except Exception as exc:
        log.warning("security audit relay: delivery of %d event(s) failed: %s", len(rows), exc)
        for row in rows:
            row.delivery_attempts += 1
            if row.delivery_attempts >= settings.max_attempts:
                row.delivery_state = DELIVERY_FAILED
                result.failed += 1
            else:
                row.next_attempt_at = now + timedelta(seconds=backoff_seconds(row.delivery_attempts, rng=rng))
                result.retried += 1
        return result
    for row in rows:
        row.delivery_attempts += 1
        row.delivery_state = DELIVERY_DELIVERED
        row.delivered_at = now
    result.delivered = len(rows)
    return result


class SecurityAuditRelay:
    """Background loop around relay_once."""

    def __init__(self, settings: SecurityAuditSettings, sink, session_scope=default_session_scope):
        self.settings = settings
        self.sink = sink
        self._session_scope = session_scope
        self._stop = threading.Event()
        self._thread = None

    def run_once(self) -> int:
        with self._session_scope() as session:
            result = relay_once(session, self.sink, self.settings)
            session.commit()
        return result.delivered + result.retried + result.failed

    def _run(self):
        while not self._stop.is_set():
            try:
                full_batch = self.run_once() >= self.settings.batch_size
            except Exception as exc:
                log.warning("security audit relay: cycle failed: %s", exc)
                full_batch = False
            if not full_batch:
                self._stop.wait(self.settings.interval_s)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="security-audit-relay")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
