-- Migration: 003_add_token_cost_columns
-- Date: 2026-07-13
-- Issue: ADR-0008 Analytics Enhancement
-- Description: Add LLM token usage and cost tracking columns to audit_events

-- llm_cost uses NUMERIC(18, 8): 10 integer digits so a large or misconfigured
-- upstream response_cost is stored rather than overflowing and failing the write.
ALTER TABLE centry.audit_events
    ADD COLUMN IF NOT EXISTS input_tokens  INTEGER,
    ADD COLUMN IF NOT EXISTS output_tokens INTEGER,
    ADD COLUMN IF NOT EXISTS llm_cost      NUMERIC(18, 8);

CREATE INDEX IF NOT EXISTS ix_audit_events_model_name
    ON centry.audit_events (model_name);
