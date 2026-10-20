"""Durable outbox for security audit events (#6945, C-1009).

``security_audit_events`` is both the audit record and the transactional outbox: a
caller adds the row in the same transaction as the security mutation it describes
(``utils.security_audit.record_security_event``), and the relay delivers pending rows
to a sink. Columns mirror ``SecurityAuditEventV1`` one to one.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from tools import db, config as c

DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"


class SecurityAuditEvent(db.Base):
    __tablename__ = 'security_audit_events'
    __table_args__ = (
        Index('ix_security_audit_events_delivery', 'delivery_state', 'next_attempt_at'),
        Index('ix_security_audit_events_project_occurred', 'project_id', 'occurred_at'),
        Index('ix_security_audit_events_run_id', 'run_id'),
        Index('ix_security_audit_events_credential_ref', 'credential_ref'),
        Index('ix_security_audit_events_occurred', 'occurred_at'),
        Index('ux_security_audit_events_event_id', 'event_id', unique=True),
        {'schema': c.POSTGRES_SCHEMA},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ocsf_class: Mapped[int] = mapped_column(Integer, nullable=False)

    tenant: Mapped[str] = mapped_column(String(256), nullable=True)
    project_id: Mapped[int] = mapped_column(Integer, nullable=True)
    application_id: Mapped[int] = mapped_column(Integer, nullable=True)
    environment: Mapped[str] = mapped_column(String(256), nullable=True)

    caller_ref: Mapped[str] = mapped_column(String(256), nullable=True)
    user_ref: Mapped[str] = mapped_column(String(256), nullable=True)
    mandate_ref: Mapped[str] = mapped_column(String(256), nullable=True)
    agent_binding_ref: Mapped[str] = mapped_column(String(256), nullable=True)
    runtime_identity_ref: Mapped[str] = mapped_column(String(256), nullable=True)

    run_id: Mapped[str] = mapped_column(String(256), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=True)
    parent_call_id: Mapped[str] = mapped_column(String(256), nullable=True)
    toolkit_instance_id: Mapped[str] = mapped_column(String(256), nullable=True)
    operation_id: Mapped[str] = mapped_column(String(256), nullable=True)
    target_ref: Mapped[str] = mapped_column(String(256), nullable=True)

    credential_ref: Mapped[str] = mapped_column(String(256), nullable=True)
    credential_generation: Mapped[int] = mapped_column(Integer, nullable=True)

    decision: Mapped[str] = mapped_column(String(8), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=True)
    would_deny: Mapped[bool] = mapped_column(Boolean, nullable=True)
    policy_revision: Mapped[str] = mapped_column(String(256), nullable=True)
    revocation_epoch: Mapped[int] = mapped_column(BigInteger, nullable=True)
    approver_ref: Mapped[str] = mapped_column(String(256), nullable=True)

    vault_request_id: Mapped[str] = mapped_column(String(256), nullable=True)
    provider_request_id: Mapped[str] = mapped_column(String(256), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=True)
    traceparent: Mapped[str] = mapped_column(String(256), nullable=True)

    # Outbox state
    delivery_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DELIVERY_PENDING, server_default=DELIVERY_PENDING)
    delivery_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text('0'))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
