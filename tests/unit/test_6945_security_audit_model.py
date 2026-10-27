"""Security audit event schema v1 (#6945, C-1009): strict, secret-free, OCSF-classed."""
import importlib.util
import sys
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


@pytest.fixture(scope="module")
def m(plugin_root):
    spec = importlib.util.spec_from_file_location(
        "security_audit_event_v1", plugin_root / "models" / "security_audit_event.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def full_event(m, **over):
    base = dict(
        event_id=str(uuid.uuid4()),
        event_type="decision",
        occurred_at=datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc),
        tenant="acme", project_id=7, application_id=11, environment="prod",
        caller_ref="user:3", user_ref="user:3", mandate_ref="mandate:1",
        agent_binding_ref="binding:2", runtime_identity_ref="nhi:5",
        run_id="run-1", attempt=1, parent_call_id="call-0", toolkit_instance_id="tk-9",
        operation_id="op-1", target_ref="github:org/repo", credential_ref="vault:github/1",
        credential_generation=4, decision="allow", reason_code="POLICY_ALLOW",
        policy_revision="rev-12", revocation_epoch=3, approver_ref="user:8",
        vault_request_id="vreq-1", provider_request_id="preq-1", outcome="success",
        traceparent="00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )
    base.update(over)
    return m.SecurityAuditEventV1(**base)


def test_full_valid_event_is_accepted(m):
    event = full_event(m)

    assert event.ocsf_class == 3003
    assert event.decision == "allow"


def test_minimal_event_gets_identity_and_time(m):
    event = m.SecurityAuditEventV1(event_type="lifecycle")

    assert uuid.UUID(event.event_id)
    assert event.occurred_at.tzinfo is not None


def test_unknown_field_is_rejected(m):
    with pytest.raises(ValidationError):
        full_event(m, free_text="hello")


def test_event_type_outside_the_set_is_rejected(m):
    with pytest.raises(ValidationError):
        full_event(m, event_type="login")


def test_naive_timestamp_is_rejected(m):
    with pytest.raises(ValidationError):
        full_event(m, occurred_at=datetime(2026, 10, 15, 12, 0))


@pytest.mark.parametrize("field,value", [("decision", "maybe"), ("outcome", "ok")])
def test_enum_fields_reject_unknown_values(m, field, value):
    with pytest.raises(ValidationError):
        full_event(m, **{field: value})


@pytest.mark.parametrize("value", ["lowercase", "HAS SPACE", "X" * 65, "BAD-DASH", ""])
def test_reason_code_is_a_bounded_code_not_free_text(m, value):
    with pytest.raises(ValidationError):
        full_event(m, reason_code=value)


STRING_FIELDS = [
    "tenant", "environment", "caller_ref", "user_ref", "mandate_ref", "agent_binding_ref",
    "runtime_identity_ref", "run_id", "parent_call_id", "toolkit_instance_id", "operation_id",
    "target_ref", "credential_ref", "policy_revision", "approver_ref", "vault_request_id",
    "provider_request_id", "traceparent",
]
SECRET_SHAPED = [
    "Bearer abcdefghijklmnop",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln",
    "-----BEGIN PRIVATE KEY-----",
    "ghp_" + "a" * 36,
    "github_pat_" + "A" * 30,
    "sk-" + "a" * 32,
    "xoxb-1234567890-abcdef",
    "AKIA" + "A" * 16,
    "x" * 257,
]


@pytest.mark.parametrize("field", STRING_FIELDS)
@pytest.mark.parametrize("secret", SECRET_SHAPED)
def test_secret_shaped_strings_are_rejected_on_every_string_field(m, field, secret):
    with pytest.raises(ValidationError):
        full_event(m, **{field: secret})


def test_event_id_must_be_a_uuid(m):
    with pytest.raises(ValidationError):
        full_event(m, event_id="not-a-uuid")


@pytest.mark.parametrize("value", [
    "user:3", "vault:project/12/github", "task-123", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    "x" * 256,
])
def test_ordinary_references_are_not_flagged(m, value):
    m.assert_no_secret_shaped(value)


def test_sanitizer_error_never_echoes_the_value(m):
    secret = "ghp_" + "a" * 36
    with pytest.raises(ValueError) as exc:
        m.assert_no_secret_shaped(secret)

    assert secret not in str(exc.value)


@pytest.mark.parametrize("event_type,ocsf", [
    ("admission", 3003), ("decision", 3003), ("approval", 3003),
    ("credential_acquired", 3002), ("probe", 3002), ("refresh", 3002),
    ("rotation", 3004), ("withdrawal", 3004), ("issuer_action", 3004), ("lifecycle", 3004),
    ("execution_outcome", 6003), ("platform_operation", 6003),
])
def test_ocsf_class_is_derived_from_event_type(m, event_type, ocsf):
    assert m.SecurityAuditEventV1(event_type=event_type).ocsf_class == ocsf


def test_every_event_type_has_an_ocsf_class(m):
    assert set(m.OCSF_CLASS_BY_EVENT_TYPE) == set(m.EVENT_TYPES)


def test_a_platform_operation_names_subject_actor_context_operation_and_target(m):
    """#7214: a write through the internal Elitea MCP by a governed run, in the v1 fields (no new column)."""
    event = m.SecurityAuditEventV1(
        event_type="platform_operation", decision="allow", reason_code="PLATFORM_OPERATION", project_id=2,
        user_ref="idp:tid-1:oid-1", agent_binding_ref="9b2c6f1e-0000-4000-8000-000000000001",
        runtime_identity_ref="context:6c1d5a2e-0000-4000-8000-000000000002", run_id="run-1",
        operation_id="build:post_applications_prompt_lib", target_ref="POST /api/v2/elitea_core/applications/prompt_lib/2")
    assert event.ocsf_class == 6003
    with pytest.raises(ValidationError):   # the run token itself can never be the target
        m.SecurityAuditEventV1(event_type="platform_operation", target_ref="Bearer eio1.abcdefghijklmnop")


def test_conflicting_ocsf_class_is_rejected(m):
    with pytest.raises(ValidationError):
        m.SecurityAuditEventV1(event_type="decision", ocsf_class=6003)
