-- 20260505_002_add_audit_columns_to_fills.sql
-- L7 (loop_v1 convergence): add the canonical 7-column audit chain to hft.fills.
--
-- Same 7 columns as 20260505_001_add_audit_columns_to_orders.sql; see that
-- file for column-by-column rationale. Splitting into two migrations lets
-- ClickHouse apply each ALTER atomically — a partial failure in one table
-- does not leave the other half-applied. The dual-write mapper detects
-- partial state (only one of the two applied) and refuses to start.
--
-- FillEvent does not currently carry trace_id (contracts/execution.py:37).
-- L8 will add it via a router.py:275 lookup over (client_order_id ->
-- OrderCommand -> OrderIntent.trace_id). Until then this column will be
-- empty for live fills — which is safe because the dual-write mapper
-- writes the default '' for missing fields.

-- Up
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS trace_id String DEFAULT '';
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS feature_snapshot_id String DEFAULT '';
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS risk_decision_id String DEFAULT '';
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS strategy_version String DEFAULT '';
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS config_hash String DEFAULT '';
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS git_sha String DEFAULT '';
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS data_session_id String DEFAULT '';

-- Down
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS trace_id;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS feature_snapshot_id;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS risk_decision_id;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS strategy_version;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS config_hash;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS git_sha;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS data_session_id;
