#!/usr/bin/env bash
set -euo pipefail

# Simple ClickHouse sanity checks for market data flow.
# Usage:
#   CLICKHOUSE_CONTAINER=clickhouse ./scripts/monitor_clickhouse.sh
#   SYMBOL=2330 ./scripts/monitor_clickhouse.sh  (filters per-symbol)

CH_CONTAINER="${CLICKHOUSE_CONTAINER:-clickhouse}"
CH_CLIENT="docker exec ${CH_CONTAINER} clickhouse-client --query"

symbol_filter=""
if [ -n "${SYMBOL:-}" ]; then
  symbol_filter="WHERE symbol='${SYMBOL}'"
fi

echo "[CH] Total count / min_ts / max_ts"
$CH_CLIENT "SELECT count(), min(toDateTime64(exch_ts/1e9,3)), max(toDateTime64(exch_ts/1e9,3)) FROM hft.market_data ${symbol_filter}"

echo "[CH] Last 10 symbols by count"
$CH_CLIENT "SELECT symbol, count() AS c, min(toDateTime64(exch_ts/1e9,3)) AS min_ts, max(toDateTime64(exch_ts/1e9,3)) AS max_ts FROM hft.market_data ${symbol_filter} GROUP BY symbol ORDER BY c DESC LIMIT 10"

echo "[CH] Last 5 minutes ingress (rows)"
$CH_CLIENT "SELECT count() FROM hft.market_data WHERE toDateTime64(exch_ts/1e9,3) > now() - INTERVAL 5 MINUTE ${symbol_filter}"
