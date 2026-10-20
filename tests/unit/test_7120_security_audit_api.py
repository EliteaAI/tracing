"""Security audit list and summary endpoints (#7120, C-1009): authorization matrix and contract."""
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

NOW = datetime.now(timezone.utc)
PERMISSION = "models.admin.audit_trail.view"

ACTORS = {
    "super_admin": {"role": "super_admin", "mode": "administration", "expect": 200},
    "platform_admin": {"role": "admin", "mode": "administration", "expect": 200},
    "platform_editor": {"role": "editor", "mode": "administration", "expect": 403},
    "platform_viewer": {"role": "viewer", "mode": "administration", "expect": 403},
    "project_admin": {"role": "admin", "mode": "default", "expect": 403},
    "project_editor": {"role": "editor", "mode": "default", "expect": 403},
    "project_viewer": {"role": "viewer", "mode": "default", "expect": 403},
    "token_with_permission": {"token_permissions": {PERMISSION}, "expect": 200},
    "token_without_permission": {"token_permissions": {"models.admin.tracing.view"}, "expect": 403},
    "unauthenticated": None,
}
ENDPOINTS = ["list_api", "summary_api"]


@pytest.fixture
def app():
    return Flask(__name__)


def call(audit_pkg, app, endpoint, query=""):
    handler = getattr(audit_pkg, endpoint).AdminAPI()
    with app.test_request_context(f"/?{query}"):
        return handler.get()


def seed(db):
    db.insert(decision="allow", reason_code="BINDING_MISSING", would_deny=True, project_id=1,
              occurred_at=NOW - timedelta(hours=1))
    db.insert(decision="allow", reason_code="POLICY_ALLOW", would_deny=False, project_id=1,
              occurred_at=NOW - timedelta(hours=2))
    db.insert(decision="deny", reason_code="BINDING_MISSING", project_id=2, occurred_at=NOW - timedelta(hours=3))


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("name", sorted(ACTORS, key=str))
def test_authorization_matrix(audit_pkg, audit_db, app, endpoint, name):
    seed(audit_db)
    actor = ACTORS[name]
    audit_pkg.auth.actor = actor
    expected = 401 if actor is None else actor["expect"]

    body, status = call(audit_pkg, app, endpoint)

    assert status == expected
    if expected != 200:
        assert "reason_code" not in str(body) and "BINDING_MISSING" not in str(body)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_nothing_is_read_before_the_permission_check(audit_pkg, app, monkeypatch, endpoint):
    def boom(*args, **kwargs):
        raise AssertionError("database touched without authorization")
    monkeypatch.setattr(audit_pkg.sa, "default_session_scope", boom)
    for actor in (None, {"role": "viewer", "mode": "administration"}):
        audit_pkg.auth.actor = actor

        _, status = call(audit_pkg, app, endpoint)

        assert status in (401, 403)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_openapi_registration_is_internal_only(audit_pkg, endpoint):
    handler = getattr(audit_pkg, endpoint)

    openapi = handler.AdminAPI.get._openapi
    assert openapi["mcp_tool"] is False and openapi["available_to_users"] is False
    assert openapi["tags"] and openapi["name"]
    assert list(handler.API.mode_handlers) == ["administration"]


def test_both_endpoints_declare_the_audit_permission_and_platform_roles_only(audit_pkg):
    """The matrix above judges by these declarations, so pin them."""
    assert len(audit_pkg.auth.declared) == 2
    for descriptor in audit_pkg.auth.declared:
        assert descriptor["permissions"] == [PERMISSION]
        assert descriptor["recommended_roles"] == {
            "administration": {"admin": True, "editor": False, "viewer": False}}


# --- contract as a permitted caller -----------------------------------------------------------

@pytest.fixture
def operator(audit_pkg):
    audit_pkg.auth.actor = {"role": "admin", "mode": "administration"}


def test_list_returns_total_and_rows(audit_pkg, audit_db, app, operator):
    seed(audit_db)

    body, status = call(audit_pkg, app, "list_api", "would_deny=true&limit=5")

    assert status == 200 and body["total"] == 1
    assert [(r["reason_code"], r["would_deny"]) for r in body["rows"]] == [("BINDING_MISSING", True)]


def test_list_honours_the_limit_cap(audit_pkg, audit_db, app, operator):
    for _ in range(101):
        audit_db.insert(reason_code="X", occurred_at=NOW)

    body, _ = call(audit_pkg, app, "list_api", "limit=1000")

    assert body["total"] == 101 and len(body["rows"]) == 100


@pytest.mark.parametrize("endpoint,query", [
    ("list_api", "would_deny=maybe"), ("list_api", "limit=0"), ("list_api", "offset=-1"),
    ("list_api", "date_to=never"), ("summary_api", "since_hours=0"), ("summary_api", "since_hours=169"),
    ("summary_api", "would_deny=1"),
])
def test_bad_parameters_are_a_400_with_a_code(audit_pkg, audit_db, app, operator, endpoint, query):
    body, status = call(audit_pkg, app, endpoint, query)

    assert status == 400 and body["code"] == "INVALID_REQUEST" and body["error"]


def test_summary_returns_the_grouped_list(audit_pkg, audit_db, app, operator):
    seed(audit_db)

    body, status = call(audit_pkg, app, "summary_api", "since_hours=24")

    assert status == 200
    assert {(r["decision"], r["would_deny"], r["reason_code"], r["count"]) for r in body} == {
        ("allow", True, "BINDING_MISSING", 1), ("allow", False, "POLICY_ALLOW", 1),
        ("deny", None, "BINDING_MISSING", 1)}


def test_summary_filters_by_would_deny(audit_pkg, audit_db, app, operator):
    seed(audit_db)

    body, _ = call(audit_pkg, app, "summary_api", "would_deny=true")

    assert [(r["reason_code"], r["count"]) for r in body] == [("BINDING_MISSING", 1)]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_database_failure_is_a_503_without_internals(audit_pkg, app, operator, monkeypatch, endpoint):
    def broken():
        raise RuntimeError("connection to db.internal:5432 refused")
    monkeypatch.setattr(audit_pkg.sa, "default_session_scope", broken)

    body, status = call(audit_pkg, app, endpoint)

    assert status == 503 and body["code"] == "AUDIT_UNAVAILABLE"
    assert "db.internal" not in str(body)


# --- the older tracing endpoints stay behind `register_api` ----------------------------------

def legacy_handlers(audit_pkg):
    import importlib
    v2 = "tracing_pkg_7120.api.v2"
    status = importlib.import_module(f"{v2}.status")
    collect = importlib.import_module(f"{v2}.collect")
    otlp = importlib.import_module(f"{v2}.otlp")
    return [(status.AdminAPI, "get", {}), (status.PromptLibAPI, "get", {"project_id": 1}),
            (collect.PromptLibAPI, "post", {"project_id": 1}), (otlp.PromptLibAPI, "post", {"project_id": 1})]


@pytest.mark.parametrize("enabled", [False, True])
def test_status_collect_and_otlp_answer_404_unless_register_api_is_set(audit_pkg, app, enabled):
    audit_pkg.auth.actor = {"role": "super_admin", "mode": "administration"}
    module = type("Module", (), {"_legacy_api": enabled, "enabled": False, "config": {},
                                 "get_config": lambda self: {}, "get_tracer": lambda self: None})()
    for cls, method, kwargs in legacy_handlers(audit_pkg):
        handler = cls()
        handler.module = module
        with app.test_request_context("/", method="POST", json={"traces": []}):
            _, status = getattr(handler, method)(**kwargs)

        assert (status == 404) is (not enabled), (cls.__module__, status)
