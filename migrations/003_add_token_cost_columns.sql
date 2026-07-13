-- Migration: 003_add_token_cost_columns
-- Date: 2026-07-13
-- Issue: https://github.com/EliteaAI/elitea_issues/issues/<TBD>
-- Description: Add LLM token usage and cost tracking columns to audit_events

ALTER TABLE centry.audit_events
    ADD COLUMN IF NOT EXISTS input_tokens  INTEGER,
    ADD COLUMN IF NOT EXISTS output_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS llm_cost      NUMERIC(12, 8);

CREATE INDEX IF NOT EXISTS ix_audit_events_model_name
    ON centry.audit_events (model_name);
