-- Migration: 005_drop_audit_events_usage_columns
-- Date: 2026-09-16
-- Issue: EL-6575 Remove usage columns from audit_events once Usage owns them
-- Description: Drop the token/cost columns and their provenance stamps from audit_events

-- Reverses 003_add_token_cost_columns and 004_add_cache_token_columns. The usage
-- plugin's usage_event table is now the single source of truth for tokens and cost
-- (#6574): every analytics and Usage reader was repointed there, and audit_events
-- keeps only the audit-trail shape — who / what / when / how long / did it fail.
--
-- token_source and cost_source were never added by a migration; they arrived via
-- elitea_core's schema guard and SQLAlchemy create_all, so IF EXISTS is load-bearing
-- for any environment that ran neither.
--
-- Run once, ahead of deploying the code that stops writing them. Both directions are
-- safe: the writer filters the event dict against the ORM model, so a column dropped
-- here is simply never named in an INSERT.
ALTER TABLE centry.audit_events
    DROP COLUMN IF EXISTS input_tokens,
    DROP COLUMN IF EXISTS output_tokens,
    DROP COLUMN IF EXISTS cache_read_tokens,
    DROP COLUMN IF EXISTS cache_creation_tokens,
    DROP COLUMN IF EXISTS llm_cost,
    DROP COLUMN IF EXISTS token_source,
    DROP COLUMN IF EXISTS cost_source;

-- Index backfill for already-provisioned databases. These nine indexes are declared
-- in tracing/models/audit_event.py, so create_all provisions them for a fresh table,
-- but existing databases got them from elitea_core's audit_events schema guard —
-- deleted by this issue. All nine are on retained columns; none of them is affected
-- by the drop above. Kept here so the table's owner carries the guarantee.
CREATE INDEX IF NOT EXISTS ix_audit_events_timestamp         ON centry.audit_events (timestamp);
CREATE INDEX IF NOT EXISTS ix_audit_events_user_id           ON centry.audit_events (user_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_project_id        ON centry.audit_events (project_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_trace_id          ON centry.audit_events (trace_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_entity            ON centry.audit_events (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_model_name        ON centry.audit_events (model_name);
CREATE INDEX IF NOT EXISTS ix_audit_events_project_timestamp ON centry.audit_events (project_id, timestamp);
CREATE INDEX IF NOT EXISTS ix_audit_events_tool_name         ON centry.audit_events (tool_name) WHERE tool_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_audit_events_is_error          ON centry.audit_events (is_error) WHERE is_error IS TRUE;
