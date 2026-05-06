-- 20260505_001_add_audit_columns_to_orders.sql
-- L7 (loop_v1 convergence): add the canonical 7-column audit chain to hft.orders.
--
-- Why these 7 columns:
--   trace_id            — links to diagnostics/trace.py emit_always() chain
--   feature_snapshot_id — reference into the feature plane state at decision time
--   risk_decision_id    — reference into audit.risk_log for the gating decision
--   strategy_version    — git SHA of the deployed strategy code
--   config_hash         — SHA256[:16] of the loop YAML in effect
--   git_sha             — build_info git_sha (matches HFT_GIT_SHA build-arg)
--   data_session_id     — sim/shadow/live session identifier
--
-- All columns are String DEFAULT '' (nullable-equivalent) so existing rows
-- remain valid without a backfill, and new rows can populate progressively.
-- Pairs with 20260505_002_add_audit_columns_to_fills.sql — the dual-write
-- mapper in recorder/writer.py refuses to start unless both migrations are
-- applied OR neither is, to prevent partial-state ambiguity.

-- Up
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS trace_id String DEFAULT '';
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS feature_snapshot_id String DEFAULT '';
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS risk_decision_id String DEFAULT '';
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS strategy_version String DEFAULT '';
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS config_hash String DEFAULT '';
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS git_sha String DEFAULT '';
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS data_session_id String DEFAULT '';

-- Down
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS trace_id;
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS feature_snapshot_id;
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS risk_decision_id;
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS strategy_version;
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS config_hash;
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS git_sha;
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS data_session_id;
