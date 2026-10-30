-- Migration: 008_add_summary_index_to_security_audit_events
-- Date: 2026-10-30
-- Issue: #7294 (C-1009 performance pass)
-- Description: Covering index of the operator audit summary (GROUP BY decision, would_deny, reason_code over a window
-- of occurred_at), so the window is read from the index alone.
--
-- The plugin builds the same index, idempotently, when it starts (PostgreSQL only; it waits at most 5 s for the table
-- and tries again at the next start). For a large outbox build it ahead of the deploy without blocking audit writes:
-- run this file outside a transaction (CONCURRENTLY). Safe to repeat. If a concurrent build was interrupted, drop the
-- invalid index (DROP INDEX CONCURRENTLY centry.ix_security_audit_events_summary) and run it again.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_security_audit_events_summary
    ON centry.security_audit_events (occurred_at)
    INCLUDE (decision, would_deny, reason_code);
