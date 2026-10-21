-- Migration: 007_add_undelivered_index_to_security_audit_events
-- Date: 2026-10-21
-- Issue: #6754, #6945 (C-1009) security metrics
-- Description: Partial index that answers the outbox gauges (count and oldest occurred_at of undelivered rows)

-- ix_security_audit_events_delivery is (delivery_state, next_attempt_at) and cannot answer
-- min(occurred_at); delivered rows are never purged, so the gauge query would walk them on every export.
--
-- The plugin applies the same statement, idempotently, when it starts (PostgreSQL only), so this file
-- is the record for operators who run migrations ahead of the deploy. Safe to repeat.
CREATE INDEX IF NOT EXISTS ix_security_audit_events_undelivered
    ON centry.security_audit_events (occurred_at)
    WHERE delivery_state IN ('pending', 'failed');
