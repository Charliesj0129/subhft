-- 20260330_001_add_instrument_columns.sql
-- Multi-instrument support: add instrument_type, underlying, strike, option_right, expiry
-- to market_data, orders, and fills tables.
-- Deployment order: run this BEFORE deploying new recorder code.

-- Up

-- hft.market_data
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS instrument_type LowCardinality(String) DEFAULT '';
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS underlying LowCardinality(String) DEFAULT '';
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS strike_scaled Int64 DEFAULT 0;
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS option_right LowCardinality(String) DEFAULT '';
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS expiry Date DEFAULT '1970-01-01';

-- hft.orders
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS instrument_type LowCardinality(String) DEFAULT '';
ALTER TABLE hft.orders ADD COLUMN IF NOT EXISTS oc_type LowCardinality(String) DEFAULT '';

-- hft.fills
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS instrument_type LowCardinality(String) DEFAULT '';
ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS oc_type LowCardinality(String) DEFAULT '';

-- Down

-- ALTER TABLE hft.market_data DROP COLUMN IF EXISTS instrument_type;
-- ALTER TABLE hft.market_data DROP COLUMN IF EXISTS underlying;
-- ALTER TABLE hft.market_data DROP COLUMN IF EXISTS strike_scaled;
-- ALTER TABLE hft.market_data DROP COLUMN IF EXISTS option_right;
-- ALTER TABLE hft.market_data DROP COLUMN IF EXISTS expiry;
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS instrument_type;
-- ALTER TABLE hft.orders DROP COLUMN IF EXISTS oc_type;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS instrument_type;
-- ALTER TABLE hft.fills DROP COLUMN IF EXISTS oc_type;
