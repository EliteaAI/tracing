"""would_deny on the security audit event (#7120, C-1009): strict bool, OCSF unmapped, outbox column."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def event(audit_pkg, **over):
    return audit_pkg.event.SecurityAuditEventV1(
        event_type="admission", decision="allow", reason_code="BINDING_MISSING", occurred_at=NOW, **over)


@pytest.mark.parametrize("value", [True, False, None])
def test_would_deny_accepts_a_bool_or_none(audit_pkg, value):
    assert event(audit_pkg, would_deny=value).would_deny is value


def test_would_deny_defaults_to_none_for_enforce_and_legacy_events(audit_pkg):
    assert event(audit_pkg).would_deny is None


@pytest.mark.parametrize("value", ["true", "false", "yes", "", 1, 0])
def test_would_deny_rejects_anything_but_a_real_bool(audit_pkg, value):
    with pytest.raises(ValidationError):
        event(audit_pkg, would_deny=value)


def test_v1_stays_additive_old_events_still_validate(audit_pkg):
    legacy = {"event_type": "lifecycle", "reason_code": "CREDENTIAL_REF_ACTIVE"}

    assert audit_pkg.event.SecurityAuditEventV1.model_validate(legacy).would_deny is None


def test_ocsf_puts_would_deny_under_unmapped(audit_pkg):
    for value in (True, False):
        row = audit_pkg.outbox.SecurityAuditEvent(**event(audit_pkg, would_deny=value).model_dump())

        unmapped = audit_pkg.relay.to_ocsf(row)["unmapped"]

        assert unmapped["would_deny"] is value


def test_ocsf_omits_would_deny_for_enforce_events(audit_pkg):
    row = audit_pkg.outbox.SecurityAuditEvent(**event(audit_pkg).model_dump())

    assert "would_deny" not in audit_pkg.relay.to_ocsf(row)["unmapped"]


def test_ocsf_keeps_decision_as_what_happened(audit_pkg):
    row = audit_pkg.outbox.SecurityAuditEvent(**event(audit_pkg, would_deny=True).model_dump())

    ocsf = audit_pkg.relay.to_ocsf(row)

    assert ocsf["status"] == "allow" and ocsf["status_code"] == "BINDING_MISSING"


def test_outbox_has_the_column_and_the_occurred_at_index(audit_pkg):
    table = audit_pkg.outbox.SecurityAuditEvent.__table__

    assert table.c.would_deny.nullable is True
    assert ("occurred_at",) in {tuple(c.name for c in i.columns) for i in table.indexes}


def test_ensure_column_runs_the_idempotent_statements(audit_pkg):
    statements = []

    class Connection:
        def execute(self, statement):
            statements.append(str(statement))

    class Engine:
        def begin(self):
            engine = self

            class Scope:
                def __enter__(self):
                    return Connection()

                def __exit__(self, *exc):
                    return False
            return Scope()

    audit_pkg.sa.ensure_would_deny_column(Engine(), 'cen"try')

    assert statements == [
        'ALTER TABLE "cen""try".security_audit_events ADD COLUMN IF NOT EXISTS would_deny BOOLEAN',
        'CREATE INDEX IF NOT EXISTS ix_security_audit_events_occurred '
        'ON "cen""try".security_audit_events (occurred_at)',
    ]


def test_migration_file_carries_the_same_statements(plugin_root):
    sql = (plugin_root / "migrations" / "006_add_would_deny_to_security_audit_events.sql").read_text()

    assert "ADD COLUMN IF NOT EXISTS would_deny BOOLEAN" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_security_audit_events_occurred" in sql


def test_init_upgrades_an_existing_outbox_right_after_create_all(plugin_root):
    source = (plugin_root / "module.py").read_text()
    body = source.split("def _init_security_audit(self):", 1)[1].split("\n    def ", 1)[0]

    assert body.index("create_all(") < body.index("security_audit.ensure_would_deny_column(db.engine")


def test_api_is_registered_when_the_outbox_exists_even_without_register_api(plugin_root):
    source = (plugin_root / "module.py").read_text()

    assert "self._register_api = _API_TOOLS_IMPORTABLE and (self._legacy_api or self._security_audit_available)" in source
    assert source.count("self.descriptor.init_api()") == 1  # only through _init_api, which documents it
