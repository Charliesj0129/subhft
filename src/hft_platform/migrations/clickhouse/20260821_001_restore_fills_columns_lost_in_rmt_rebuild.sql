-- Restore the five hft.fills columns that 20260425_001 dropped.
--
-- 20260425_001_fills_replacing_merge_tree.sql rebuilds hft.fills as a
-- ReplacingMergeTree by CREATE-ing hft.fills_new, INSERT..SELECT-ing into it and
-- RENAME-ing over the original. Its CREATE declares the twelve columns from
-- 20260301_001 only, so it silently reverted three ALTERs that had already been
-- applied and recorded:
--
--   20260325_002  tax_scaled
--   20260327_002  decision_price, arrival_price
--   20260330_001  instrument_type, oc_type
--
-- hft.schema_migrations still lists all three as applied -- they were, and were
-- then undone by a later migration written against the initial schema rather
-- than the current one. hft.orders kept its 20260330_001 columns because only
-- fills was rebuilt, which is why the ledger and the database disagree on one
-- table and not the other.
--
-- The consequence was invisible until 2026-08-21: recorder/mapper.py emits all
-- five keys, so every fills insert failed with
-- "Unrecognized column 'tax_scaled' in table hft.fills" and the loader DLQ'd the
-- batch. No fill had reached the loader since 2026-04-25 because the shioaji
-- 1.5.x callback payload was being dropped further upstream; fixing that
-- exposed this immediately, with 15 DLQ batches in .wal/dlq/fills_*.jsonl.
--
-- These are the exact column definitions from the three reverted migrations, so
-- applying this leaves the table identical to what those migrations intended.
-- Additive and idempotent: safe to re-run, and existing rows take the DEFAULT.
--
-- After applying, replay .wal/dlq/fills_*.jsonl -- the fills are in the DLQ, not
-- lost.

-- Up

ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS tax_scaled Int64 DEFAULT 0 Codec(DoubleDelta, LZ4);
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS decision_price Int64 DEFAULT 0;
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS arrival_price Int64 DEFAULT 0;
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS instrument_type LowCardinality(String) DEFAULT '';
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS oc_type LowCardinality(String) DEFAULT '';

-- Down

-- Deliberately not reversible. Dropping these columns is what caused the
-- outage this migration repairs.
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS tax_scaled;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS decision_price;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS arrival_price;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS instrument_type;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS oc_type;
