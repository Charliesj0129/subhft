# L7 Audit-Column Migration Runbook

**Migrations:** `20260505_001_add_audit_columns_to_orders.sql`, `20260505_002_add_audit_columns_to_fills.sql`

**Tables affected:** `hft.orders`, `hft.fills`

**New columns (7 each, all `String DEFAULT ''`):** `trace_id`, `feature_snapshot_id`, `risk_decision_id`, `strategy_version`, `config_hash`, `git_sha`, `data_session_id`.

**Why these are loop_v1 critical.** Pre-L7 you can see an order or fill row but you cannot reconstruct *why* it was placed. The 7 audit columns let an operator answer: which trace, which feature snapshot, which risk decision, which strategy version, which config, which build, which session. Without them, L8's per-order canonical explanation has nowhere to land.

**Dual-write contract.** The recorder's `DataWriter` (`src/hft_platform/recorder/writer.py`) detects whether both migrations are applied at startup and refuses to start in partial state (`L7PartialMigrationError`). Either both apply, both roll back, or the engine will not boot. There is no half-applied operating mode.

---

## Preflight

Run these against the target ClickHouse node BEFORE applying.

```bash
# 1. Verify ClickHouse is reachable.
clickhouse-client --host "$CH_HOST" --port "$CH_PORT" -q "SELECT 1"

# 2. Capture current row counts (used by post-apply verification).
clickhouse-client --host "$CH_HOST" --port "$CH_PORT" -q "
  SELECT 'hft.orders' AS table, count() AS rows FROM hft.orders
  UNION ALL
  SELECT 'hft.fills', count() FROM hft.fills
" > /tmp/l7_preflight_counts.txt

# 3. Capture a sample row per table for shape verification.
clickhouse-client --host "$CH_HOST" --port "$CH_PORT" -q \
  "SELECT * FROM hft.orders ORDER BY ingest_ts DESC LIMIT 1 FORMAT Vertical" \
  > /tmp/l7_preflight_sample_orders.txt

clickhouse-client --host "$CH_HOST" --port "$CH_PORT" -q \
  "SELECT * FROM hft.fills ORDER BY ts_exchange DESC LIMIT 1 FORMAT Vertical" \
  > /tmp/l7_preflight_sample_fills.txt

# 4. Verify the L7 migration files are present in the running image.
ls -la src/hft_platform/migrations/clickhouse/20260505_*.sql
```

Stop and escalate if any of: row counts unexpectedly low, sample row missing core fields (`order_id`, `client_order_id`, `instrument_type`), migration files not present in image.

---

## Apply

The recorder applies migrations automatically on startup via `apply_schema()` (`src/hft_platform/recorder/schema.py`). For a controlled apply outside engine restart:

```bash
# Option A: run the standalone applier (recommended for production).
uv run python -c "
from hft_platform.recorder.writer import DataWriter
import os
os.environ['HFT_CLICKHOUSE_ENABLED'] = '1'
w = DataWriter()
w.connect()
"

# Option B: re-bounce the recorder service. apply_schema() runs at startup.
docker compose restart hft-engine
```

Both paths run **both** L7 migrations atomically because they are listed sequentially in the migration directory and `apply_schema` rolls forward in filename order.

`apply_schema` writes one row per migration into `hft.schema_migrations`. After apply, expect:

```bash
clickhouse-client -q "
  SELECT version, name, applied_at
  FROM hft.schema_migrations
  WHERE version IN ('20260505_001', '20260505_002')
  ORDER BY version
"
```

Both rows must appear before continuing.

---

## Backfill

Existing rows already return `''` for the new columns (ClickHouse default-on-read). The backfill script is for operator visibility and optional physical materialization:

```bash
# Dry-run: report row counts and empty-audit row counts.
uv run python scripts/ops/backfill_audit_columns.py --dry-run

# Optional: physically materialize defaults into storage parts.
# This issues `ALTER TABLE ... UPDATE col = '' WHERE col = ''` per audit
# column per table (14 mutations total). Mutations are async — track via
# `SELECT * FROM system.mutations WHERE not is_done`.
uv run python scripts/ops/backfill_audit_columns.py --materialize
```

The script refuses to run in partial-migration state (matches the recorder's behavior).

---

## Verify

```bash
# 1. Both migrations recorded.
clickhouse-client -q "
  SELECT count() FROM hft.schema_migrations
  WHERE version IN ('20260505_001', '20260505_002')
"
# Expect: 2

# 2. New columns exist on hft.orders.
clickhouse-client -q "DESCRIBE hft.orders FORMAT Vertical" \
  | grep -E '(trace_id|feature_snapshot_id|risk_decision_id|strategy_version|config_hash|git_sha|data_session_id)'
# Expect: 7 lines, type String, default expression "''" for each.

# 3. Same for hft.fills.
clickhouse-client -q "DESCRIBE hft.fills FORMAT Vertical" \
  | grep -E '(trace_id|feature_snapshot_id|risk_decision_id|strategy_version|config_hash|git_sha|data_session_id)'
# Expect: 7 lines.

# 4. Row counts unchanged.
clickhouse-client -q "
  SELECT 'hft.orders' AS table, count() AS rows FROM hft.orders
  UNION ALL
  SELECT 'hft.fills', count() FROM hft.fills
" > /tmp/l7_postapply_counts.txt
diff /tmp/l7_preflight_counts.txt /tmp/l7_postapply_counts.txt
# Expect: empty diff.

# 5. Existing rows return empty audit fields.
clickhouse-client -q "
  SELECT trace_id, feature_snapshot_id, git_sha
  FROM hft.orders
  ORDER BY ingest_ts DESC LIMIT 3
"
# Expect: three rows of empty strings.

# 6. Engine restarted clean (no L7PartialMigrationError in logs).
docker compose logs hft-engine | grep -E '(l7_audit_columns_detected|L7PartialMigrationError)'
# Expect: l7_audit_columns_detected mode=extended active_tables=['hft.fills', 'hft.orders']
```

Stop and escalate on any mismatch — particularly any row-count change.

---

## Rollback

Rollback drops the columns. **It is destructive iff any row has populated audit fields.** The dual-write mapper writes `''` until L8 wires real audit values, so during the loop_v1 stabilization window the fields will be empty and rollback is safe.

```bash
# 1. Confirm no row has populated audit fields. Any non-empty value means
#    a live writer has already produced real audit data; rolling back will
#    discard it.
clickhouse-client -q "
  SELECT
    countIf(trace_id != '') AS orders_trace,
    countIf(git_sha != '') AS orders_git,
    countIf(feature_snapshot_id != '') AS orders_feat
  FROM hft.orders
"

clickhouse-client -q "
  SELECT
    countIf(trace_id != '') AS fills_trace,
    countIf(git_sha != '') AS fills_git
  FROM hft.fills
"

# Stop and escalate if any non-zero value.

# 2. Drop the columns (both tables).
for col in trace_id feature_snapshot_id risk_decision_id strategy_version config_hash git_sha data_session_id; do
  clickhouse-client -q "ALTER TABLE hft.orders DROP COLUMN IF EXISTS $col"
  clickhouse-client -q "ALTER TABLE hft.fills  DROP COLUMN IF EXISTS $col"
done

# 3. Remove migration entries so apply_schema re-runs cleanly next bounce.
clickhouse-client -q "
  ALTER TABLE hft.schema_migrations DELETE
  WHERE version IN ('20260505_001', '20260505_002')
"
```

After rollback the recorder's dual-write detector will see neither migration applied and run in legacy mode — no audit columns emitted.

---

## Failure modes

| Symptom | Likely cause | Action |
|---|---|---|
| Recorder fails to start with `L7PartialMigrationError` | One of the two ALTERs aborted (disk full, replica lag) | Inspect `system.errors`. Either complete the missing migration manually or roll back the applied half. |
| Verify step 4 shows row-count delta | A live writer kept inserting during apply; OR a TTL-driven part rotation completed mid-window | Re-run preflight count + verify count after a quiescent minute. Escalate if persistent. |
| `DESCRIBE` shows audit columns but with wrong type | A pre-existing column with the same name (very unlikely; `hft.orders`/`hft.fills` were grepped before naming) | Stop. Manually inspect column origin via `system.parts_columns`. |
| Backfill script reports `partial_migration_state` | Same as first row | Same as first row. |
