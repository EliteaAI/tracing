-- Migration: 006_add_would_deny_to_security_audit_events
-- Date: 2026-10-20
-- Issue: #7120 (C-1009) audit-only governance mode
-- Description: Add the nullable would_deny flag and an occurred_at index to security_audit_events

-- would_deny is additive in schema v1: NULL = an enforce or legacy event, TRUE = audit-only
-- allowed the run but enforce would have refused it with reason_code, FALSE = audit-only
-- allowed it and enforce would too. The index backs the "last N hours" summary and list.
--
-- The plugin applies the same two statements, idempotently, when it starts, so this file is
-- the record for operators who run migrations ahead of the deploy. Both are safe to repeat.
ALTER TABLE centry.security_audit_events
    ADD COLUMN IF NOT EXISTS would_deny BOOLEAN;

CREATE INDEX IF NOT EXISTS ix_security_audit_events_occurred
    ON centry.security_audit_events (occurred_at);
