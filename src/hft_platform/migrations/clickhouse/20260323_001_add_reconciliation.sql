-- Up: post-market 3-way reconciliation results table
CREATE TABLE IF NOT EXISTS hft.reconciliation
(
    event_date    Date,
    status        LowCardinality(String),  -- 'MATCH' or 'MISMATCH'
    broker_pnl    Int64,
    platform_pnl  Int64,
    ch_pnl        Int64,
    details       String DEFAULT '',
    timestamp_ns  UInt64
) ENGINE = MergeTree()
ORDER BY event_date
TTL event_date + INTERVAL 365 DAY;

-- Down
-- DROP TABLE IF EXISTS hft.reconciliation;
