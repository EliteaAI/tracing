"""#6756: security_audit_* methods other plugins call in-process (record in the caller's transaction)."""
import importlib
import sys
import types
from contextlib import contextmanager

import pytest
from sqlalchemy.orm import DeclarativeBase

SECRET_ISH = "vault:topsecret-reference-123"


@pytest.fixture
def ctx(monkeypatch, plugin_root):
    """The plugin's models/utils/methods packages on a real declarative base, without the plugin __init__."""
    class Base(DeclarativeBase):
        pass

    monkeypatch.setitem(sys.modules, "tools", types.SimpleNamespace(
        db=types.SimpleNamespace(Base=Base), config=types.SimpleNamespace(POSTGRES_SCHEMA="centry")))
    root = "tracing_sa_6756"
    for name, sub in ((root, ""), (f"{root}.models", "models"), (f"{root}.utils", "utils"),
                      (f"{root}.methods", "methods")):
        package = types.ModuleType(name)
        package.__path__ = [str(plugin_root / sub) if sub else str(plugin_root)]
        monkeypatch.setitem(sys.modules, name, package)
    audit = importlib.import_module(f"{root}.utils.security_audit")
    methods = importlib.import_module(f"{root}.methods.security_audit")
    audit.configure(audit.SecurityAuditSettings())
    yield types.SimpleNamespace(audit=audit, method=methods.Method)  # pylon binds the module as ``self``: call unbound
    for name in [n for n in sys.modules if n.startswith(root)]:
        monkeypatch.delitem(sys.modules, name, raising=False)


class FakeSession:
    def __init__(self):
        self.added, self.commits = [], 0

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


def available():
    return types.SimpleNamespace(_security_audit_available=True)


def event_dict(**over):
    return {"event_type": "decision", "decision": "allow", "reason_code": "POLICY_ALLOW",
            "project_id": 7, "credential_ref": SECRET_ISH, **over}


def test_record_adds_one_outbox_row_to_the_callers_session_without_committing(ctx):
    session = FakeSession()

    result = ctx.method.security_audit_record(available(), session, event_dict())

    assert len(session.added) == 1 and session.commits == 0
    assert result == {"event_id": session.added[0].event_id}


def test_record_rejects_invalid_event_without_echoing_values(ctx):
    session = FakeSession()

    with pytest.raises(ValueError) as caught:
        ctx.method.security_audit_record(
            available(), session, event_dict(event_type="nope", unknown_field=SECRET_ISH))

    message = str(caught.value)
    assert "event_type" in message and "unknown_field" in message
    assert SECRET_ISH not in message and "nope" not in message
    assert session.added == []


def test_record_rejects_a_secret_shaped_value_naming_only_the_shape(ctx):
    token = "ghp_" + "a" * 20
    with pytest.raises(ValueError) as caught:
        ctx.method.security_audit_record(available(), FakeSession(), event_dict(credential_ref=token))

    assert "credential_ref" in str(caught.value) and token not in str(caught.value)


def test_record_rejects_a_non_dict_event(ctx):
    with pytest.raises(ValueError, match="must be a dict"):
        ctx.method.security_audit_record(available(), FakeSession(), "not a dict")


def test_autocommit_returns_the_event_id(ctx, monkeypatch):
    session = FakeSession()

    @contextmanager
    def scope():
        yield session

    monkeypatch.setattr(ctx.audit, "default_session_scope", scope)

    result = ctx.method.security_audit_record_autocommit(available(), event_dict())

    assert len(session.added) == 1 and session.commits == 1
    assert result == {"event_id": session.added[0].event_id}


def test_autocommit_returns_none_when_buffered_and_raises_when_fail_closed(ctx, monkeypatch):
    @contextmanager
    def broken():
        raise OSError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(ctx.audit, "default_session_scope", broken)

    assert ctx.method.security_audit_record_autocommit(available(), event_dict()) == {"event_id": None}
    assert ctx.audit.security_audit_drop_total.total() >= 1

    ctx.audit.configure(ctx.audit.SecurityAuditSettings(failure_policy=ctx.audit.FAIL_CLOSED))
    with pytest.raises(ctx.audit.SecurityAuditUnavailable):
        ctx.method.security_audit_record_autocommit(available(), event_dict())


def test_admission_allowed_local_delegates_in_process(ctx, monkeypatch):
    monkeypatch.setattr(ctx.audit, "security_audit_admission_allowed", lambda: False)
    assert ctx.method.security_audit_admission_allowed_local(available()) is False

    monkeypatch.setattr(ctx.audit, "security_audit_admission_allowed", lambda: True)
    assert ctx.method.security_audit_admission_allowed_local(available()) is True


@pytest.mark.parametrize("module", [types.SimpleNamespace(), types.SimpleNamespace(_security_audit_available=False)])
def test_every_method_fails_closed_when_the_outbox_is_not_initialised(ctx, module):
    with pytest.raises(RuntimeError, match="security audit outbox not available"):
        ctx.method.security_audit_record(module, FakeSession(), event_dict())
    with pytest.raises(RuntimeError, match="security audit outbox not available"):
        ctx.method.security_audit_record_autocommit(module, event_dict())
    with pytest.raises(RuntimeError, match="security audit outbox not available"):
        ctx.method.security_audit_admission_allowed_local(module)


def test_module_marks_the_outbox_available_only_after_the_db_tool_check():
    import pathlib
    source = (pathlib.Path(__file__).resolve().parents[2] / 'module.py').read_text()
    body = source.split('def _init_security_audit(self):', 1)[1].split('\n    def ', 1)[0]
    assert body.index('from tools import db') < body.index('self._security_audit_available = True')
    assert body.index('self._security_audit_available = True') < body.index('self.descriptor.init_methods()')
