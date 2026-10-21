"""Security audit read side (#7120, C-1009): filters, pagination bounds, summary GROUP BY.

Runs the real statements against an in-memory database holding the real outbox table.
"""
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
T0 = NOW


def seed(db):
    db.insert(event_type="admission", decision="allow", reason_code="BINDING_MISSING", would_deny=True,
              project_id=1, user_ref="user:1", agent_binding_ref="binding:a", occurred_at=T0 - timedelta(hours=1))
    db.insert(event_type="admission", decision="allow", reason_code="BINDING_MISSING", would_deny=True,
              project_id=1, user_ref="user:2", agent_binding_ref="binding:a", occurred_at=T0 - timedelta(hours=2))
    db.insert(event_type="admission", decision="allow", reason_code="POLICY_ALLOW", would_deny=False,
              project_id=1, user_ref="user:1", credential_ref="vault:1", occurred_at=T0 - timedelta(hours=3))
    db.insert(event_type="admission", decision="deny", reason_code="BINDING_MISSING",
              project_id=2, user_ref="user:3", occurred_at=T0 - timedelta(hours=4))
    db.insert(event_type="decision", decision="allow", reason_code="POLICY_ALLOW",
              project_id=2, credential_ref="vault:2", occurred_at=T0 - timedelta(hours=30))


def run(audit_pkg, **args):
    sa = audit_pkg.sa
    return sa.query_security_events(sa.parse_filters(args), sa.parse_page(args))


def reasons(rows):
    return [(r["reason_code"], r["would_deny"]) for r in rows]


# --- list: filters ----------------------------------------------------------------------------

def test_no_filter_returns_everything_newest_first(audit_pkg, audit_db):
    seed(audit_db)

    total, rows = run(audit_pkg)

    assert total == 5
    assert [r["occurred_at"] for r in rows] == sorted((r["occurred_at"] for r in rows), reverse=True)


def test_would_deny_true_selects_only_rehearsed_refusals(audit_pkg, audit_db):
    seed(audit_db)

    total, rows = run(audit_pkg, would_deny="true")

    assert total == 2 and set(reasons(rows)) == {("BINDING_MISSING", True)}


def test_would_deny_false_excludes_enforce_events_without_the_flag(audit_pkg, audit_db):
    seed(audit_db)

    total, rows = run(audit_pkg, would_deny="false")

    assert total == 1 and reasons(rows) == [("POLICY_ALLOW", False)]


@pytest.mark.parametrize("args,expected", [
    ({"project_id": "2"}, 2),
    ({"user_ref": "user:1"}, 2),
    ({"agent_binding_ref": "binding:a"}, 2),
    ({"credential_ref": "vault:1"}, 1),
    ({"event_type": "decision"}, 1),
    ({"reason_code": "BINDING_MISSING"}, 3),
    ({"reason_code": "BINDING_MISSING", "would_deny": "true", "project_id": "1", "user_ref": "user:2"}, 1),
])
def test_each_filter_narrows_the_total(audit_pkg, audit_db, args, expected):
    seed(audit_db)

    total, rows = run(audit_pkg, **args)

    assert total == expected == len(rows)


def test_date_range_is_inclusive_and_accepts_z_and_naive(audit_pkg, audit_db):
    seed(audit_db)
    lo = (T0 - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    hi = (T0 - timedelta(hours=1)).replace(tzinfo=None).isoformat()

    total, _ = run(audit_pkg, date_from=lo, date_to=hi)

    assert total == 3


def test_filter_values_are_parameters_not_sql(audit_pkg, audit_db):
    seed(audit_db)

    total, _ = run(audit_pkg, user_ref="user:1' OR '1'='1")

    assert total == 0


@pytest.mark.parametrize("args", [
    {"would_deny": "maybe"}, {"project_id": "x"}, {"project_id": "-1"}, {"date_from": "yesterday"},
    {"user_ref": "u" * 257}, {"limit": "0"}, {"limit": "-5"}, {"limit": "abc"}, {"offset": "-1"},
])
def test_malformed_parameters_raise_invalid_query(audit_pkg, args):
    with pytest.raises(audit_pkg.sa.InvalidQuery):
        run(audit_pkg, **args)


# --- list: pagination and sorting -------------------------------------------------------------

def test_default_page_is_twenty(audit_pkg):
    page = audit_pkg.sa.parse_page({})

    assert (page.limit, page.offset, page.sort_by, page.descending) == (20, 0, "occurred_at", True)


def test_limit_is_capped_at_one_hundred_and_limit_plus_one_is_clamped(audit_pkg):
    assert audit_pkg.sa.parse_page({"limit": "100"}).limit == 100
    assert audit_pkg.sa.parse_page({"limit": "101"}).limit == 100
    assert audit_pkg.sa.parse_page({"limit": "1"}).limit == 1


def test_pages_do_not_overlap_and_cover_the_total(audit_pkg, audit_db):
    seed(audit_db)

    first = run(audit_pkg, limit="2", offset="0")[1]
    second = run(audit_pkg, limit="2", offset="2")[1]
    third = run(audit_pkg, limit="2", offset="4")[1]
    beyond = run(audit_pkg, limit="2", offset="6")

    ids = [r["event_id"] for r in first + second + third]
    assert len(ids) == len(set(ids)) == 5
    assert beyond[0] == 5 and beyond[1] == []


def test_a_limit_above_the_cap_returns_at_most_the_cap(audit_pkg, audit_db):
    for _ in range(105):
        audit_db.insert(reason_code="X", occurred_at=T0)

    total, rows = run(audit_pkg, limit="101")

    assert total == 105 and len(rows) == 100


def test_unknown_sort_by_falls_back_and_never_reaches_a_column_name(audit_pkg, audit_db):
    seed(audit_db)

    assert audit_pkg.sa.parse_page({"sort_by": "delivery_state; DROP TABLE x"}).sort_by == "occurred_at"
    assert audit_pkg.sa.parse_page({"sort_by": "user_ref"}).sort_by == "occurred_at"
    with pytest.raises(audit_pkg.sa.InvalidQuery):
        audit_pkg.sa.query_security_events(audit_pkg.sa.SecurityEventFilters(),
                                           audit_pkg.sa.Page(sort_by="delivery_state"))


@pytest.mark.parametrize("field", sorted({"occurred_at", "event_type", "decision", "reason_code", "project_id"}))
def test_every_whitelisted_sort_field_runs_in_both_directions(audit_pkg, audit_db, field):
    seed(audit_db)

    asc = run(audit_pkg, sort_by=field, sort_order="asc")[1]
    desc = run(audit_pkg, sort_by=field, sort_order="desc")[1]

    assert len(asc) == len(desc) == 5
    assert audit_pkg.sa.parse_page({"sort_by": field}).sort_by == field


def test_rows_carry_v1_fields_and_no_outbox_state(audit_pkg, audit_db):
    seed(audit_db)

    row = run(audit_pkg, limit="1")[1][0]

    assert set(row) == set(audit_pkg.event.SecurityAuditEventV1.model_fields)
    assert not {"delivery_state", "delivery_attempts", "id"} & set(row)
    assert isinstance(row["occurred_at"], str)


# --- summary ----------------------------------------------------------------------------------

def test_summary_groups_by_decision_would_deny_and_reason_most_frequent_first(audit_pkg, audit_db):
    seed(audit_db)

    rows = audit_pkg.sa.summarize_security_events(24, now=T0)

    assert rows == [
        {"decision": "allow", "would_deny": True, "reason_code": "BINDING_MISSING", "count": 2},
        {"decision": "deny", "would_deny": None, "reason_code": "BINDING_MISSING", "count": 1},
        {"decision": "allow", "would_deny": False, "reason_code": "POLICY_ALLOW", "count": 1},
    ]


def test_summary_window_excludes_older_events(audit_pkg, audit_db):
    seed(audit_db)

    assert sum(r["count"] for r in audit_pkg.sa.summarize_security_events(24, now=T0)) == 4
    assert sum(r["count"] for r in audit_pkg.sa.summarize_security_events(48, now=T0)) == 5
    assert sum(r["count"] for r in audit_pkg.sa.summarize_security_events(2, now=T0)) == 2


def test_summary_filters_by_would_deny(audit_pkg, audit_db):
    seed(audit_db)

    rows = audit_pkg.sa.summarize_security_events(24, True, now=T0)

    assert rows == [{"decision": "allow", "would_deny": True, "reason_code": "BINDING_MISSING", "count": 2}]


def test_summary_of_nothing_is_an_empty_list(audit_pkg, audit_db):
    assert audit_pkg.sa.summarize_security_events(24, now=T0) == []


def test_summary_is_one_group_by_statement(audit_pkg, audit_db):
    seed(audit_db)
    statements = []
    from sqlalchemy import event as sa_event
    sa_event.listen(audit_db.engine, "before_cursor_execute",
                    lambda conn, cur, statement, *a: statements.append(statement))

    audit_pkg.sa.summarize_security_events(24, now=T0)

    assert len(statements) == 1 and "GROUP BY" in statements[0]


@pytest.mark.parametrize("value", ["0", "169", "-1", "x"])
def test_since_hours_outside_one_to_168_is_refused(audit_pkg, value):
    with pytest.raises(audit_pkg.sa.InvalidQuery):
        audit_pkg.sa.parse_since_hours({"since_hours": value})


def test_since_hours_bounds_and_default(audit_pkg):
    assert audit_pkg.sa.parse_since_hours({}) == 24
    assert audit_pkg.sa.parse_since_hours({"since_hours": "1"}) == 1
    assert audit_pkg.sa.parse_since_hours({"since_hours": "168"}) == 168


def test_summary_keeps_rehearsed_and_enforce_events_with_one_reason_apart(audit_pkg, audit_db):
    for would_deny in (True, False, None, None):
        audit_db.insert(decision="allow", reason_code="POLICY_ALLOW", would_deny=would_deny, occurred_at=T0)

    rows = audit_pkg.sa.summarize_security_events(1, now=T0)

    assert {(r["would_deny"], r["count"]) for r in rows} == {(True, 1), (False, 1), (None, 2)}
