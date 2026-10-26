"""Security audit event schema v1 (#6945, C-1009).

A closed, secret-free record of a security-relevant decision or action. It is stored
in the ``security_audit_events`` outbox (see ``security_audit_outbox``) and relayed to
a sink, independently of Langfuse and of the operational ``audit_events`` trail.

Callers must never pass secrets: every string field is checked by
``assert_no_secret_shaped`` and the only free-form text, ``reason_code``, is a bounded
upper-case code. Pure pydantic, no Pylon imports, so it loads anywhere.
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

# OCSF classes: 3002 authentication, 3003 authorize session, 3004 entity management,
# 6003 API activity.
OCSF_CLASS_BY_EVENT_TYPE = {
    "admission": 3003,
    "decision": 3003,
    "approval": 3003,
    "credential_acquired": 3002,
    "probe": 3002,
    "refresh": 3002,
    "rotation": 3004,
    "withdrawal": 3004,
    "issuer_action": 3004,
    "lifecycle": 3004,
    "execution_outcome": 6003,
    # A platform change made through the internal Elitea MCP on a user's behalf by a governed run (#7214): user_ref
    # is the subject, agent_binding_ref the actor, runtime_identity_ref the run's security context
    "platform_operation": 6003,
}
EVENT_TYPES = frozenset(OCSF_CLASS_BY_EVENT_TYPE)

MAX_STRING_LENGTH = 256
REASON_CODE_PATTERN = r"^[A-Z0-9_]+$"

_SECRET_PATTERNS = (
    ("bearer", re.compile(r"(?i)\bbearer\s+\S{8,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+")),
    ("private_key", re.compile(r"-----BEGIN")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}")),
    ("api_key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")),
    ("slack_token", re.compile(r"xox[bp]-[A-Za-z0-9-]{8,}")),
    ("aws_key", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}")),
)


def assert_no_secret_shaped(value: str) -> str:
    """Reject values that look like credentials, or that are too long to be a reference.

    The error names the shape, never the value.
    """
    if len(value) > MAX_STRING_LENGTH:
        raise ValueError(f"value longer than {MAX_STRING_LENGTH} characters")
    for shape, pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            raise ValueError(f"secret-shaped value rejected ({shape})")
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SecurityAuditEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    occurred_at: datetime = Field(default_factory=_utcnow)

    tenant: Optional[str] = None
    project_id: Optional[int] = None
    application_id: Optional[int] = None
    environment: Optional[str] = None

    caller_ref: Optional[str] = None
    user_ref: Optional[str] = None
    mandate_ref: Optional[str] = None
    agent_binding_ref: Optional[str] = None
    runtime_identity_ref: Optional[str] = None

    run_id: Optional[str] = None
    attempt: Optional[int] = None
    parent_call_id: Optional[str] = None
    toolkit_instance_id: Optional[str] = None
    operation_id: Optional[str] = None
    target_ref: Optional[str] = None

    credential_ref: Optional[str] = None
    credential_generation: Optional[int] = None

    decision: Optional[Literal["allow", "deny"]] = None
    reason_code: Optional[str] = Field(default=None, max_length=64, pattern=REASON_CODE_PATTERN)
    # Audit-only rehearsal (#7120): True = the run was allowed but enforce would have refused it
    # with ``reason_code``. None = an enforce or legacy event. Strict: a string is rejected.
    would_deny: Optional[StrictBool] = None
    policy_revision: Optional[str] = None
    revocation_epoch: Optional[int] = None
    approver_ref: Optional[str] = None

    vault_request_id: Optional[str] = None
    provider_request_id: Optional[str] = None
    outcome: Optional[
        Literal["success", "denied", "interaction_required", "failed", "target_outcome_unknown"]
    ] = None

    traceparent: Optional[str] = None
    ocsf_class: int

    @model_validator(mode="before")
    @classmethod
    def _derive_ocsf_class(cls, data):
        if isinstance(data, dict) and data.get("event_type") in OCSF_CLASS_BY_EVENT_TYPE:
            derived = OCSF_CLASS_BY_EVENT_TYPE[data["event_type"]]
            if data.get("ocsf_class", derived) != derived:
                raise ValueError("ocsf_class is derived from event_type and cannot be overridden")
            data = {**data, "ocsf_class": derived}
        return data

    @field_validator("event_id")
    @classmethod
    def _event_id_is_uuid(cls, value):
        uuid.UUID(value)
        return value

    @field_validator("event_type")
    @classmethod
    def _known_event_type(cls, value):
        if value not in EVENT_TYPES:
            raise ValueError(f"event_type must be one of {sorted(EVENT_TYPES)}")
        return value

    @field_validator("occurred_at")
    @classmethod
    def _timezone_aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value

    @field_validator("*")
    @classmethod
    def _no_secrets_in_strings(cls, value):
        if isinstance(value, str):
            assert_no_secret_shaped(value)
        return value
