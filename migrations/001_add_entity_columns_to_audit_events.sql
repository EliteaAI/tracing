-- Migration: 001_add_entity_columns_to_audit_events
-- Date: 2026-02-26
-- Issue: https://github.com/EliteaAI/admin_ui/issues/15
-- Description: Add entity tracking columns to audit_events for usage analytics

ALTER TABLE centry.audit_events
    ADD COLUMN IF NOT EXISTS entity_type VARCHAR(32),
    ADD COLUMN IF NOT EXISTS entity_id INTEGER,
    ADD COLUMN IF NOT EXISTS entity_name VARCHAR(256);

CREATE INDEX IF NOT EXISTS ix_audit_events_entity
    ON centry.audit_events (entity_type, entity_id);
