-- Add EMO trade classification direction to market_data table.
-- Values: +1 (BUY), -1 (SELL), 0 (UNKNOWN/unclassified).
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS trade_direction Int8 DEFAULT 0;
