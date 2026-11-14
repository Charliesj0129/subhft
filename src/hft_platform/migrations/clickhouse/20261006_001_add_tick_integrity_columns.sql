-- Keep the fields the normalizer already parses for every tick but the recorder never stored.
--
--   total_volume  cumulative volume of the session as reported by the exchange feed. With it a
--                 repeated delivery is exact (same total_volume = same trade) instead of an
--                 estimate from copy counts, and recorded volume can be checked against the
--                 feed's own running total (also usable for equities).
--   is_simtrade   1 = simulated (pre-open matching) trade, 0 = real.
--   is_odd_lot    1 = odd-lot trade, 0 = round lot.
--
-- Existing rows and any WAL file written before this migration read as 0, which means
-- "not recorded", not "zero cumulative volume" / "real" / "round lot".
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS total_volume Int64 DEFAULT 0;
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS is_simtrade UInt8 DEFAULT 0;
ALTER TABLE hft.market_data ADD COLUMN IF NOT EXISTS is_odd_lot UInt8 DEFAULT 0;
