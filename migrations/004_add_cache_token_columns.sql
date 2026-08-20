-- Migration: 004_add_cache_token_columns
-- Date: 2026-08-20
-- Issue: EL-6267 Prompt cache token and cost reporting
-- Description: Add prompt-cache token columns to audit_events

-- Cache tokens are stored alongside input_tokens/output_tokens so analytics can
-- price and report cache reads and cache writes separately. Nullable so rows
-- written before this migration stay distinguishable from a genuine zero.
ALTER TABLE centry.audit_events
    ADD COLUMN IF NOT EXISTS cache_read_tokens     INTEGER,
    ADD COLUMN IF NOT EXISTS cache_creation_tokens INTEGER;
