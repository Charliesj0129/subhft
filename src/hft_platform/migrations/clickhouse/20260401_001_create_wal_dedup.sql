-- Up
-- WAL replay dedup table (EC-1)
-- Tracks content hashes of WAL batches already inserted into ClickHouse
-- to prevent duplicate rows on replay. TTL cleans up entries after 30 days.
CREATE DATABASE IF NOT EXISTS hft;

CREATE TABLE IF NOT EXISTS hft._wal_dedup (
    table  String,
    hash   String,
    row_count UInt64,
    ts     Int64
) ENGINE = MergeTree()
ORDER BY (table, hash)
TTL toDateTime(ts / 1000000000) + INTERVAL 30 DAY;

-- Down
-- DROP TABLE IF EXISTS hft._wal_dedup;
