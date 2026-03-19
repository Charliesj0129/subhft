"""Daily data quality report — profiles previous trading day's market data.

Reads from ClickHouse hft.market_data, profiles each symbol, and outputs
a JSON report to reports/data_quality/.

Usage:
    python scripts/daily_data_quality.py [--date YYYY-MM-DD] [--out-dir reports/data_quality]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from structlog import get_logger  # noqa: E402

from hft_platform.data_quality.profiler import DataProfiler  # noqa: E402

logger = get_logger("daily_data_quality")


def _query_market_data(date_str: str) -> dict[str, list[dict]]:
    """Query ClickHouse for market data on the given date.

    Returns dict mapping symbol -> list of record dicts.
    """
    try:
        import clickhouse_connect
    except ImportError:
        logger.error("clickhouse_connect not installed — cannot query market data")
        return {}

    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    user = os.getenv("HFT_CLICKHOUSE_USER", "default")
    password = os.getenv("HFT_CLICKHOUSE_PASSWORD", "")

    try:
        client = clickhouse_connect.get_client(host=host, port=port, username=user, password=password)
    except Exception as exc:
        logger.error("ClickHouse connection failed", error=str(exc))
        return {}

    query = (
        "SELECT symbol, price, volume, exch_ts, spread "
        "FROM hft.market_data "
        f"WHERE toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}' "
        "ORDER BY symbol, exch_ts"
    )
    try:
        result = client.query(query)
    except Exception as exc:
        logger.error("ClickHouse query failed", error=str(exc))
        return {}

    by_symbol: dict[str, list[dict]] = {}
    for row in result.result_rows:
        symbol, price, volume, exch_ts, spread = row
        by_symbol.setdefault(symbol, []).append({
            "price_scaled": int(price),
            "volume": int(volume),
            "timestamp_ns": int(exch_ts),
            "spread_scaled": int(spread) if spread is not None else 0,
        })
    return by_symbol


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily data quality profiler")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    parser.add_argument("--date", default=yesterday, help="Date to profile (YYYY-MM-DD)")
    parser.add_argument("--out-dir", default="reports/data_quality", help="Output directory")
    args = parser.parse_args()

    date_str = args.date
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Profiling market data", date=date_str)
    by_symbol = _query_market_data(date_str)

    if not by_symbol:
        logger.warning("No market data found", date=date_str)
        report = {"date": date_str, "symbols": [], "profiles": []}
        out_path = out_dir / f"dq_{date_str}.json"
        out_path.write_text(json.dumps(report, indent=2))
        return 0

    profiler = DataProfiler()
    profiles = []
    for symbol, records in sorted(by_symbol.items()):
        profile = profiler.profile_symbol(symbol, date_str, records)
        profiles.append(profile.to_dict())

    report = {
        "date": date_str,
        "symbols": sorted(by_symbol.keys()),
        "symbol_count": len(by_symbol),
        "profiles": profiles,
    }
    out_path = out_dir / f"dq_{date_str}.json"
    out_path.write_text(json.dumps(report, indent=2))
    logger.info("Data quality report written", path=str(out_path), symbols=len(by_symbol))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
