# clickhouse-queries

Use when verifying ingestion or debugging missing data.

Common queries:
- SELECT count() FROM hft.market_data
- SELECT symbol, count() FROM hft.market_data GROUP BY symbol ORDER BY count() DESC LIMIT 10
