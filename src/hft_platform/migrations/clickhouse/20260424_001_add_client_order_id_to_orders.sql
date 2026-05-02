-- Up
ALTER TABLE hft.orders
    ADD COLUMN IF NOT EXISTS client_order_id String DEFAULT '';

-- Down
ALTER TABLE hft.orders
    DROP COLUMN IF EXISTS client_order_id;
