"""Security audit outbox, relay, backpressure policy and metrics (#6945, C-1009)."""
import importlib
import json
import sys
import types
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from pylon.core.tools import log
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def sa(monkeypatch, plugin_root):
    """The plugin's models/utils packages on a real declarative base, without the plugin __init__."""
    class Base(DeclarativeBase):
        pass

    monkeypatch.setitem(sys.modules, "tools", types.SimpleNamespace(
        db=types.SimpleNamespace(Base=Base), config=types.SimpleNamespace(POSTGRES_SCHEMA="centry")))
    root = "tracing_sa_6945"
    for name, sub in ((root, ""), (f"{root}.models", "models"), (f"{root}.utils", "utils"), (f"{root}.rpc", "rpc")):
        package = types.ModuleType(name)
        package.__path__ = [str(plugin_root / sub) if sub else str(plugin_root)]
        monkeypatch.setitem(sys.modules, name, package)
    security_audit = importlib.import_module(f"{root}.utils.security_audit")
    relay = importlib.import_module(f"{root}.utils.security_audit_relay")
    outbox = importlib.import_module(f"{root}.models.security_audit_outbox")
    event = importlib.import_module(f"{root}.models.security_audit_event")
    rpc = importlib.import_module(f"{root}.rpc.security_audit")
    security_audit.configure(security_audit.SecurityAuditSettings())
    yield types.SimpleNamespace(sa=security_audit, relay=relay, outbox=outbox, event=event, rpc=rpc)
    for name in [n for n in sys.modules if n.startswith(root)]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def make_event(sa, **over):
    return sa.event.SecurityAuditEventV1(event_type="decision", decision="allow",
                                         reason_code="POLICY_ALLOW", project_id=7,
                                         credential_ref="vault:github/1", occurred_at=NOW, **over)


class FakeSession:
    def __init__(self, rows=(), scalar=0):
        self.rows, self.scalar = list(rows), scalar
        self.added, self.statements, self.commits = [], [], 0

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1

    def execute(self, statement):
        self.statements.append(statement)
        return types.SimpleNamespace(
            scalars=lambda: types.SimpleNamespace(all=lambda: self.rows),
            scalar_one=lambda: self.scalar,
            scalar_one_or_none=lambda: self.scalar,
            one=lambda: (self.scalar, self.scalar),
        )


def scope_for(session):
    @contextmanager
    def scope():
        yield session
    return scope


class FakeSink:
    def __init__(self, fail_times=0):
        self.fail_times, self.calls, self.delivered = fail_times, 0, []

    def deliver(self, events):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OSError("sink down")
        self.delivered.extend(events)


def pending_rows(sa, count=2):
    rows = []
    for _ in range(count):
        row = sa.outbox.SecurityAuditEvent(**make_event(sa).model_dump())
        row.delivery_state, row.delivery_attempts = "pending", 0
        rows.append(row)
    return rows


# --- outbox row -------------------------------------------------------------------------------

def test_record_adds_row_to_the_callers_session_without_committing(sa):
    session = FakeSession()
    event = make_event(sa)

    row = sa.sa.record_security_event(session, event)

    assert session.added == [row]
    assert session.commits == 0
    assert row.event_id == event.event_id and row.credential_ref == "vault:github/1"


def test_record_validates_dict_input(sa):
    with pytest.raises(ValueError):
        sa.sa.record_security_event(FakeSession(), {"event_type": "decision", "extra": 1})


def test_table_shape(sa):
    table = sa.outbox.SecurityAuditEvent.__table__

    assert table.name == "security_audit_events" and table.schema == "centry"
    indexed = {tuple(c.name for c in i.columns): i.unique for i in table.indexes}
    assert indexed[("delivery_state", "next_attempt_at")] is False
    assert indexed[("project_id", "occurred_at")] is False
    assert indexed[("run_id",)] is False and indexed[("credential_ref",)] is False
    assert indexed[("event_id",)] is True
    for name in sa.event.SecurityAuditEventV1.model_fields:
        assert name in table.c


def test_autocommit_records_and_commits(sa):
    session = FakeSession()

    sa.sa.record_security_event_autocommit(make_event(sa), session_scope=scope_for(session))

    assert len(session.added) == 1 and session.commits == 1
    assert sa.sa.security_audit_drop_total.total() == 0


def failing_scope():
    @contextmanager
    def scope():
        raise RuntimeError("db down")
        yield
    return scope


def test_autocommit_failure_under_buffer_counts_a_drop(sa):
    sa.sa.configure(sa.sa.SecurityAuditSettings(failure_policy="buffer"))
    before = sa.sa.security_audit_drop_total.total()

    assert sa.sa.record_security_event_autocommit(make_event(sa), session_scope=failing_scope()) is None
    assert sa.sa.security_audit_drop_total.total() == before + 1


def test_autocommit_failure_under_fail_closed_raises_and_drops_nothing(sa):
    sa.sa.configure(sa.sa.SecurityAuditSettings(failure_policy="fail_closed"))
    before = sa.sa.security_audit_drop_total.total()

    with pytest.raises(sa.sa.SecurityAuditUnavailable):
        sa.sa.record_security_event_autocommit(make_event(sa), session_scope=failing_scope())
    assert sa.sa.security_audit_drop_total.total() == before


def test_invalid_event_is_a_caller_error_not_a_drop(sa):
    before = sa.sa.security_audit_drop_total.total()

    with pytest.raises(ValueError):
        sa.sa.record_security_event_autocommit({"event_type": "nope"}, session_scope=failing_scope())
    assert sa.sa.security_audit_drop_total.total() == before


# --- settings ---------------------------------------------------------------------------------

def test_settings_defaults(sa):
    s = sa.sa.SecurityAuditSettings.from_config(None)

    assert (s.failure_policy, s.backlog_limit, s.relay_enabled) == ("buffer", 10000, False)
    assert (s.interval_s, s.batch_size, s.max_attempts) == (5, 100, 10)


def test_settings_from_config(sa):
    s = sa.sa.SecurityAuditSettings.from_config({
        "failure_policy": "fail_closed", "backlog_limit": 3,
        "relay": {"enabled": True, "interval_s": 2, "batch_size": 10, "max_attempts": 4,
                  "sink": {"type": "jsonl", "path": "/tmp/x.jsonl"}},
    })

    assert s.failure_policy == "fail_closed" and s.backlog_limit == 3
    assert s.relay_enabled and s.sink_path == "/tmp/x.jsonl" and s.max_attempts == 4


@pytest.mark.parametrize("config", [
    {"failure_policy": "drop"},
    {"backlog_limit": 0},
    {"relay": {"enabled": True}},
    {"relay": {"enabled": True, "sink": {"type": "kafka", "path": "x"}}},
    {"relay": {"batch_size": 0}},
])
def test_invalid_settings_are_refused(sa, config):
    with pytest.raises(ValueError):
        sa.sa.SecurityAuditSettings.from_config(config)


# --- backoff and relay ------------------------------------------------------------------------

def test_backoff_schedule_is_full_jitter_exponential_capped(sa):
    ceiling = [sa.relay.backoff_seconds(n, rng=lambda: 1.0) for n in range(1, 11)]

    assert ceiling == [1, 2, 4, 8, 16, 32, 64, 128, 256, 300]
    assert all(sa.relay.backoff_seconds(n, rng=lambda: 0.0) == 0 for n in range(1, 11))
    assert sa.relay.backoff_seconds(4, rng=lambda: 0.5) == 4


def test_due_query_locks_with_skip_locked_and_only_pending(sa):
    sql = str(sa.relay.due_events_query(NOW, 100).compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "delivery_state" in sql and "next_attempt_at <=" in sql
    assert "LIMIT" in sql


def test_successful_delivery_marks_rows_delivered(sa):
    rows, sink = pending_rows(sa), FakeSink()

    result = sa.relay.relay_once(FakeSession(rows), sink, sa.sa.SecurityAuditSettings(), now=NOW)

    assert (result.delivered, result.retried, result.failed) == (2, 0, 0)
    assert [r.delivery_state for r in rows] == ["delivered", "delivered"]
    assert all(r.delivered_at == NOW and r.delivery_attempts == 1 for r in rows)
    assert len(sink.delivered) == 2


def test_failed_delivery_schedules_a_retry_with_backoff(sa):
    rows = pending_rows(sa)

    result = sa.relay.relay_once(FakeSession(rows), FakeSink(fail_times=1),
                                 sa.sa.SecurityAuditSettings(), now=NOW, rng=lambda: 1.0)

    assert (result.delivered, result.retried, result.failed) == (0, 2, 0)
    assert [r.delivery_state for r in rows] == ["pending", "pending"]
    assert all(r.delivery_attempts == 1 and r.next_attempt_at == NOW + timedelta(seconds=1) for r in rows)
    assert all(r.delivered_at is None for r in rows)


def test_row_fails_permanently_after_max_attempts(sa):
    rows = pending_rows(sa, 1)
    rows[0].delivery_attempts = 2
    settings = sa.sa.SecurityAuditSettings(max_attempts=3)

    result = sa.relay.relay_once(FakeSession(rows), FakeSink(fail_times=9), settings, now=NOW)

    assert (result.retried, result.failed) == (0, 1)
    assert rows[0].delivery_state == "failed" and rows[0].delivery_attempts == 3


def test_sink_that_fails_n_times_then_recovers_delivers_exactly_once(sa):
    rows, sink = pending_rows(sa, 1), FakeSink(fail_times=2)
    settings = sa.sa.SecurityAuditSettings(max_attempts=5)

    for step in range(4):
        due = [r for r in rows if r.delivery_state == "pending"]
        sa.relay.relay_once(FakeSession(due), sink, settings, now=NOW + timedelta(seconds=400 * step))

    assert rows[0].delivery_state == "delivered" and rows[0].delivery_attempts == 3
    assert [e["metadata"]["uid"] for e in sink.delivered] == [rows[0].event_id]


def test_empty_batch_does_not_touch_the_sink(sa):
    sink = FakeSink()

    result = sa.relay.relay_once(FakeSession([]), sink, sa.sa.SecurityAuditSettings(), now=NOW)

    assert sink.calls == 0 and result.delivered == 0


def test_run_once_commits_the_state_change(sa):
    session = FakeSession(pending_rows(sa, 1))
    relay = sa.relay.SecurityAuditRelay(sa.sa.SecurityAuditSettings(), FakeSink(), scope_for(session))

    assert relay.run_once() == 1
    assert session.commits == 1


def test_relay_thread_starts_and_stops(sa):
    session = FakeSession([])
    relay = sa.relay.SecurityAuditRelay(sa.sa.SecurityAuditSettings(interval_s=0.01), FakeSink(), scope_for(session))

    relay.start()
    relay.stop()

    assert relay._thread is None


# --- sink -------------------------------------------------------------------------------------

def test_jsonl_sink_writes_one_parseable_ocsf_object_per_line(sa, tmp_path):
    rows = pending_rows(sa, 2)
    path = tmp_path / "nested" / "audit.jsonl"
    sink = sa.relay.JsonLinesFileSink(str(path))

    sink.deliver([sa.relay.to_ocsf(r) for r in rows])
    sink.deliver([sa.relay.to_ocsf(rows[0])])

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 3
    first = lines[0]
    assert first["class_uid"] == 3003 and first["category_uid"] == 3
    assert first["metadata"]["uid"] == rows[0].event_id
    assert first["time"] == int(NOW.timestamp() * 1000)
    assert first["activity_name"] == "decision" and first["status"] == "allow"
    assert first["status_code"] == "POLICY_ALLOW"
    assert first["unmapped"]["credential_ref"] == "vault:github/1"
    assert "tenant" not in first["unmapped"]


def test_ocsf_category_follows_class(sa):
    row = sa.outbox.SecurityAuditEvent(**sa.event.SecurityAuditEventV1(event_type="execution_outcome").model_dump())

    assert sa.relay.to_ocsf(row)["class_uid"] == 6003 and sa.relay.to_ocsf(row)["category_uid"] == 6


# --- backpressure and metrics -----------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_backlog_exceeded_compares_pending_count_to_limit(sa):
    sa.sa.configure(sa.sa.SecurityAuditSettings(backlog_limit=10))
    clock = Clock()

    assert sa.sa.audit_backlog_exceeded(count_pending=lambda: 10, clock=clock) is False
    clock.t += 60
    assert sa.sa.audit_backlog_exceeded(count_pending=lambda: 11, clock=clock) is True


def test_backlog_check_is_cached_for_a_few_seconds(sa):
    sa.sa.configure(sa.sa.SecurityAuditSettings(backlog_limit=10))
    clock, calls = Clock(), []

    def count():
        calls.append(1)
        return 99

    sa.sa.audit_backlog_exceeded(count_pending=count, clock=clock)
    clock.t += 1
    sa.sa.audit_backlog_exceeded(count_pending=count, clock=clock)
    clock.t += sa.sa.BACKLOG_CACHE_TTL_S
    sa.sa.audit_backlog_exceeded(count_pending=count, clock=clock)

    assert len(calls) == 2


def test_unknown_backlog_is_treated_as_exceeded(sa):
    def broken():
        raise RuntimeError("db down")

    assert sa.sa.audit_backlog_exceeded(count_pending=broken, clock=Clock()) is True


@pytest.mark.parametrize("policy,pending,allowed", [
    ("fail_closed", 5, True),
    ("fail_closed", 50, False),
    ("buffer", 5, True),
    ("buffer", 50, True),
])
def test_admission_policy_matrix(sa, policy, pending, allowed):
    sa.sa.configure(sa.sa.SecurityAuditSettings(failure_policy=policy, backlog_limit=10))

    assert sa.sa.security_audit_admission_allowed(count_pending=lambda: pending, clock=Clock()) is allowed


def test_buffer_policy_never_queries_the_backlog(sa):
    sa.sa.configure(sa.sa.SecurityAuditSettings(failure_policy="buffer"))

    def boom():
        raise AssertionError("must not query")

    assert sa.sa.security_audit_admission_allowed(count_pending=boom, clock=Clock()) is True


def test_rpc_delegates_to_admission_policy(sa):
    sa.sa.configure(sa.sa.SecurityAuditSettings(failure_policy="buffer"))

    assert sa.rpc.RPC().security_audit_admission_allowed() is True


def test_metrics_snapshot(sa):
    class Session(FakeSession):
        def execute(self, statement):
            self.statements.append(statement)
            return types.SimpleNamespace(one=lambda: (7, NOW - timedelta(seconds=30)))

    session = Session()
    snapshot = sa.sa.security_audit_metrics(session_scope=scope_for(session), now=NOW)

    assert len(session.statements) == 1  # depth and lag are read with one query
    sql = str(session.statements[0].compile(dialect=postgresql.dialect())).lower()
    assert sql.count("select") == 3 and "count(" in sql and "min(" in sql and "security_audit_events.occurred_at" in sql
    assert sql.count("delivery_state in") == 2 and "delivered_at" not in sql  # the delivery index, no scan on delivered_at
    assert snapshot["security_audit_outbox_depth"] == 7
    assert snapshot["security_audit_delivery_lag_seconds"] == 30
    assert snapshot["security_audit_drop_total"] == sa.sa.security_audit_drop_total.total()


def test_undelivered_means_pending_or_failed(sa):
    assert set(sa.sa.UNDELIVERED_STATES) == {"pending", "failed"}


class DdlEngine:
    def __init__(self, dialect="postgresql"):
        self.dialect = types.SimpleNamespace(name=dialect)
        self.statements = []

    def begin(self):
        engine = self

        class Scope:
            def __enter__(self):
                return types.SimpleNamespace(execute=lambda statement: engine.statements.append(str(statement)))

            def __exit__(self, *exc):
                return False
        return Scope()


def test_the_partial_index_ddl_matches_the_gauge_filter(sa):
    engine = DdlEngine()
    sa.sa.ensure_undelivered_index(engine, 'cen"try')

    assert engine.statements == [
        'CREATE INDEX IF NOT EXISTS ix_security_audit_events_undelivered ON "cen""try".security_audit_events '
        "(occurred_at) WHERE delivery_state IN ('pending', 'failed')"]
    # the gauge statement filters on exactly the states of the index predicate
    session = FakeSession()
    session.execute = lambda statement: (session.statements.append(statement),
                                         types.SimpleNamespace(one=lambda: (0, None)))[1]
    sa.sa.security_audit_metrics(session_scope=scope_for(session), now=NOW)
    gauge_sql = str(session.statements[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert gauge_sql.count("delivery_state IN ('pending', 'failed')") == 2
    assert sa.sa.undelivered_predicate_sql() == "delivery_state IN ('pending', 'failed')"


def test_the_partial_index_is_skipped_outside_postgresql(sa):
    engine = DdlEngine("sqlite")

    sa.sa.ensure_undelivered_index(engine, "main")

    assert engine.statements == []


class _Scope:
    def __init__(self, inner):
        self.inner = inner

    def __enter__(self):
        return self.inner.__enter__()

    def __exit__(self, *exc):
        return False


class ConcurrentEngine:
    """An autocommit connection that answers the validity read of the index (None: missing) and records the rest."""

    def __init__(self, valid_before=None, valid_after=True, dialect="postgresql"):
        self.dialect = types.SimpleNamespace(name=dialect)
        self.statements, self.options, self.answers = [], [], [valid_before, valid_after]

    def connect(self):
        engine = self

        class Connection:
            def execution_options(self, **options):
                engine.options.append(options)
                return self

            def execute(self, statement, params=None):
                engine.statements.append(str(statement))
                answer = engine.answers.pop(0) if "indisvalid" in str(statement) else None
                return types.SimpleNamespace(scalar=lambda: answer)

        class Scope:
            def __enter__(self):
                return Connection()

            def __exit__(self, *exc):
                return False
        return Scope()


def test_the_summary_index_is_built_concurrently_in_autocommit_and_covers_the_summary(sa):
    """#7294 (review S5): the outbox takes every audit write and is never purged, so the build must not block it:
    CONCURRENTLY, in autocommit, waiting at most 5 s for its locks; the index carries the summary's columns."""
    engine = ConcurrentEngine()
    assert sa.sa.ensure_summary_index(engine, 'cen"try') is True
    assert engine.options == [{"isolation_level": "AUTOCOMMIT"}]
    built = [s for s in engine.statements if "indisvalid" not in s]
    assert built == [
        "SET lock_timeout = '5s'",
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_security_audit_events_summary ON "cen""try".security_audit_events '
        "(occurred_at) INCLUDE (decision, would_deny, reason_code)",
        "RESET lock_timeout"]
    [index] = [item for item in sa.outbox.SecurityAuditEvent.__table__.indexes
               if item.name == "ix_security_audit_events_summary"]
    assert [column.name for column in index.columns] == ["occurred_at"]
    assert index.dialect_options["postgresql"]["include"] == ["decision", "would_deny", "reason_code"]


def test_a_valid_summary_index_is_left_alone_and_an_invalid_one_is_left_to_migration_008(sa, monkeypatch):
    present = ConcurrentEngine(valid_before=True)
    assert sa.sa.ensure_summary_index(present, "centry") is True
    assert [s for s in present.statements if "indisvalid" not in s] == []   # steady state: one catalog read
    warned = []
    monkeypatch.setattr(sa.sa.log, "warning", lambda *args: warned.append(args))
    broken = ConcurrentEngine(valid_before=False)
    assert sa.sa.ensure_summary_index(broken, "centry") is False
    assert [s for s in broken.statements if "indisvalid" not in s] == []   # not rebuilt over an invalid one
    timed_out = ConcurrentEngine(valid_before=None, valid_after=False)   # the build stopped at its lock timeout
    real_connect = timed_out.connect

    def raising_connect():
        scope = real_connect()
        inner = scope.__enter__()
        real_execute = inner.execute

        def execute(statement, params=None):
            if "CREATE INDEX" in str(statement):
                timed_out.statements.append(str(statement))
                raise RuntimeError("canceling statement due to lock timeout")
            return real_execute(statement, params)
        inner.execute = execute
        return types.SimpleNamespace(__enter__=lambda: inner, __exit__=lambda *exc: False)

    timed_out.connect = lambda: _Scope(raising_connect())
    assert sa.sa.ensure_summary_index(timed_out, "centry") is False
    assert timed_out.statements[-2:] == ["RESET lock_timeout", timed_out.statements[-1]]   # reset, then re-read
    assert len([w for w in warned if "008_add_summary_index" in " ".join(map(str, w))]) == 2
    assert sa.sa.ensure_summary_index(ConcurrentEngine(dialect="sqlite"), "main") is None


def test_the_summary_index_migration_carries_the_same_index(plugin_root):
    sql = (plugin_root / "migrations" / "008_add_summary_index_to_security_audit_events.sql").read_text()
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_security_audit_events_summary" in sql
    assert "(occurred_at)" in sql and "INCLUDE (decision, would_deny, reason_code)" in sql


def test_the_partial_index_migration_carries_the_same_statement(plugin_root):
    sql = (plugin_root / "migrations" / "007_add_undelivered_index_to_security_audit_events.sql").read_text()

    assert "CREATE INDEX IF NOT EXISTS ix_security_audit_events_undelivered" in sql
    assert "(occurred_at)" in sql and "WHERE delivery_state IN ('pending', 'failed')" in sql


def test_lag_is_zero_when_nothing_is_undelivered(sa):
    session = FakeSession(scalar=None)

    snapshot = sa.sa.security_audit_metrics(session_scope=scope_for(session), now=NOW)

    assert snapshot["security_audit_delivery_lag_seconds"] == 0
    assert snapshot["security_audit_outbox_depth"] == 0


def test_audit_gauges_use_the_audit_snapshot(sa, monkeypatch):
    registered = {}

    class Recorder:
        def gauge_callback(self, name, description, fn, unit="1"):
            registered[name] = (fn, unit)

    monkeypatch.setattr(sa.sa, "security_audit_metrics", lambda: {
        "security_audit_outbox_depth": 4, "security_audit_delivery_lag_seconds": 12.5})

    sa.sa.register_audit_gauges(Recorder())

    assert set(registered) == {"security_audit_outbox_depth", "security_audit_delivery_lag_seconds"}
    assert registered["security_audit_outbox_depth"][0]() == 4
    assert registered["security_audit_delivery_lag_seconds"] == (registered["security_audit_delivery_lag_seconds"][0], "s")
    assert registered["security_audit_delivery_lag_seconds"][0]() == 12.5


def test_both_gauges_share_one_reading_per_export(sa, monkeypatch):
    registered, reads = {}, []

    class Recorder:
        def gauge_callback(self, name, description, fn, unit="1"):
            registered[name] = fn

    def reading():
        reads.append(1)
        return {"security_audit_outbox_depth": 4, "security_audit_delivery_lag_seconds": 2.5}

    clock = Clock()
    monkeypatch.setattr(sa.sa, "security_audit_metrics", reading)
    sa.sa.register_audit_gauges(Recorder(), clock=clock)

    assert registered["security_audit_outbox_depth"]() == 4
    assert registered["security_audit_delivery_lag_seconds"]() == 2.5
    assert len(reads) == 1
    clock.t += sa.sa.GAUGE_SNAPSHOT_TTL_S
    registered["security_audit_outbox_depth"]()
    assert len(reads) == 2


def test_drop_counter_is_the_shared_helper(sa, monkeypatch):
    lines = []
    monkeypatch.setattr(log, "info", lines.append, raising=False)
    monkeypatch.setattr(sa.sa.security_audit_drop_total, "_log_state", {})  # no earlier line inside the window
    before = sa.sa.security_audit_drop_total.total()
    sa.sa.security_audit_drop_total.inc()

    assert sa.sa.security_audit_drop_total.total() == before + 1
    assert lines == ["security_metric name=security_audit_drop_total value=1"]


def test_module_initialises_security_audit_before_the_tracing_enabled_gate(plugin_root):
    source = (plugin_root / "module.py").read_text()
    init = source[source.index("    def init(self):"):source.index("    def ready(self):")]

    assert init.index("self._init_security_audit()") < init.index("if not self._enabled:")
    assert "self._security_audit_relay.stop()" in source[source.index("    def deinit(self):"):]
