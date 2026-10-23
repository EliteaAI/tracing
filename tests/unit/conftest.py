"""Fixtures for tests that need the plugin's models/utils/api packages on a real SQLAlchemy base."""
import functools
import importlib
import sys
import types
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session
from sqlalchemy.pool import StaticPool

class AuthState:
    """Who the fake ``check_api`` sees as the caller. ``None`` actor = unauthenticated."""

    def __init__(self):
        self.actor = None
        self.declared = []  # every check_api descriptor, in decoration order


def _check_api(state):
    """Mirror of the platform decorator for the cases the matrix covers.

    Tokens need the permission itself; sessions are judged by the endpoint's recommended roles
    for the mode they act in, and super admin passes. 401 without a caller, 403 otherwise.
    """
    def check_api(descriptor):
        state.declared.append(descriptor)

        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                actor = state.actor
                if actor is None:
                    return {"error": "unauthorized"}, 401
                if actor.get("token_permissions") is not None:
                    allowed = bool(set(descriptor["permissions"]) & actor["token_permissions"])
                elif actor["role"] == "super_admin":
                    allowed = True
                else:
                    allowed = descriptor["recommended_roles"].get(actor["mode"], {}).get(actor["role"], False)
                if not allowed:
                    return {"error": "forbidden"}, 403
                return func(*args, **kwargs)
            return wrapper
        return decorator
    return check_api


def _register_openapi(**metadata):
    def decorator(func):
        func._openapi = metadata
        return func
    return decorator


@pytest.fixture
def audit_pkg(monkeypatch, plugin_root):
    """models/utils/api of the plugin loaded under a private package name, ``tools`` stubbed."""
    class Base(DeclarativeBase):
        pass

    state = AuthState()
    tools = types.SimpleNamespace(
        db=types.SimpleNamespace(Base=Base),
        config=types.SimpleNamespace(POSTGRES_SCHEMA="centry", ADMINISTRATION_MODE="administration",
                                     DEFAULT_MODE="default"),
        api_tools=types.SimpleNamespace(
            APIModeHandler=type("APIModeHandler", (), {}),
            APIBase=type("APIBase", (), {}),
            with_modes=lambda params: [f"<string:mode>/{p}".rstrip("/") for p in params],
        ),
        auth=types.SimpleNamespace(decorators=types.SimpleNamespace(check_api=_check_api(state))),
        register_openapi=_register_openapi,
    )
    tools.config.ADMINISTRATION_MODE = "administration"
    monkeypatch.setitem(sys.modules, "tools", tools)
    root = "tracing_pkg_7120"
    for name, sub in ((root, ""), (f"{root}.models", "models"), (f"{root}.utils", "utils"),
                      (f"{root}.api", "api"), (f"{root}.api.v2", "api/v2")):
        package = types.ModuleType(name)
        package.__path__ = [str(plugin_root / sub) if sub else str(plugin_root)]
        monkeypatch.setitem(sys.modules, name, package)
    ns = types.SimpleNamespace(
        auth=state,
        sa=importlib.import_module(f"{root}.utils.security_audit"),
        relay=importlib.import_module(f"{root}.utils.security_audit_relay"),
        outbox=importlib.import_module(f"{root}.models.security_audit_outbox"),
        event=importlib.import_module(f"{root}.models.security_audit_event"),
        list_api=importlib.import_module(f"{root}.api.v2.security_audit"),
        summary_api=importlib.import_module(f"{root}.api.v2.security_audit_summary"),
        base=Base,
    )
    ns.sa.configure(ns.sa.SecurityAuditSettings())
    yield ns
    for name in [n for n in sys.modules if n.startswith(root)]:
        sys.modules.pop(name, None)  # not monkeypatch.delitem: its undo would put the module back


@pytest.fixture
def audit_db(audit_pkg, monkeypatch):
    """An in-memory database holding the real outbox table; the read side is pointed at it."""
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           execution_options={"schema_translate_map": {"centry": None}})
    audit_pkg.base.metadata.create_all(engine)

    @contextmanager
    def scope():
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(audit_pkg.sa, "default_session_scope", scope)
    ids = iter(range(1, 10_000))

    def insert(**fields):
        fields.setdefault("event_type", "admission")
        event = audit_pkg.event.SecurityAuditEventV1(**fields)
        with Session(engine) as session:
            session.add(audit_pkg.outbox.SecurityAuditEvent(id=next(ids), **event.model_dump()))
            session.commit()
        return event

    return types.SimpleNamespace(engine=engine, scope=scope, insert=insert)
