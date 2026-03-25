-- Add TCA columns to hft.trades
ALTER TABLE hft.trades ADD COLUMN IF NOT EXISTS tax_scaled Int64 DEFAULT 0;
ALTER TABLE hft.trades ADD COLUMN IF NOT EXISTS decision_price_scaled Int64 DEFAULT 0;
ALTER TABLE hft.trades ADD COLUMN IF NOT EXISTS arrival_price_scaled Int64 DEFAULT 0;
ALTER TABLE hft.trades ADD COLUMN IF NOT EXISTS gross_pnl_scaled Int64 DEFAULT 0;

-- TCA daily aggregation table
CREATE TABLE IF NOT EXISTS hft.tca_daily (
    date                    Date,
    strategy                LowCardinality(String),
    symbol                  LowCardinality(String),
    trade_count             UInt32,
    volume                  UInt32,
    notional                Int64,
    commission_bps_mean     Float32,
    tax_bps_mean            Float32,
    delay_cost_bps_mean     Float32,
    delay_cost_bps_p95      Float32,
    exec_cost_bps_mean      Float32,
    exec_cost_bps_p95       Float32,
    impact_bps_mean         Float32,
    total_cost_bps_mean     Float32,
    total_cost_bps_p95      Float32,
    generated_at            DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(generated_at)
ORDER BY (date, strategy, symbol)
TTL date + INTERVAL 90 DAY;
