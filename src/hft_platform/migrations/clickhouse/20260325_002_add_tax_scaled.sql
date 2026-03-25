ALTER TABLE hft.fills ADD COLUMN IF NOT EXISTS tax_scaled Int64 DEFAULT 0 Codec(DoubleDelta, LZ4);
