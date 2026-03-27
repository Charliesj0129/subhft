-- Add TCA price columns to fills table for slippage decomposition
ALTER TABLE hft.fills
    ADD COLUMN IF NOT EXISTS decision_price Int64 DEFAULT 0,
    ADD COLUMN IF NOT EXISTS arrival_price Int64 DEFAULT 0;
