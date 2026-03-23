-- ClickHouse app-level user for hft-engine (least-privilege)
-- Runs on first startup via /docker-entrypoint-initdb.d/
-- Admin access remains via 'default' user for emergency operations.

CREATE DATABASE IF NOT EXISTS hft;

CREATE USER IF NOT EXISTS hft_app
    IDENTIFIED BY '{hft_ch_app_password}'
    DEFAULT DATABASE hft;

-- Grant only what the application needs:
-- DDL for schema migrations (CREATE/ALTER TABLE)
-- DML for recording (INSERT) and querying (SELECT)
GRANT CREATE DATABASE, CREATE TABLE, ALTER TABLE, DROP TABLE ON hft.* TO hft_app;
GRANT INSERT, SELECT ON hft.* TO hft_app;
GRANT CREATE VIEW, DROP VIEW ON hft.* TO hft_app;
