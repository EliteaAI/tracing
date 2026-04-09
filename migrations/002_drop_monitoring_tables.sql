-- Migration: 002_drop_monitoring_tables
-- Date: 2026-02-26
-- Issue: https://github.com/EliteaAI/admin_ui/issues/16
-- Description: Drop legacy monitoring plugin tables after migration to audit_events

DROP TABLE IF EXISTS centry.monitoring_like;
DROP TABLE IF EXISTS centry.monitoring_acceptance_events;
DROP TABLE IF EXISTS centry.monitoring_metric_meta;
DROP TABLE IF EXISTS centry.monitoring_metric;
DROP TABLE IF EXISTS centry.monitoring_acceptance;
