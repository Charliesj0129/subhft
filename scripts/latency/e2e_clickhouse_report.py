#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path

import clickhouse_connect


def _client():
    host = os.getenv("HFT_CLICKHOUSE_HOST", os.getenv("CLICKHOUSE_HOST", "localhost"))
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", os.getenv("CLICKHOUSE_PORT", "8123")))
    return clickhouse_connect.get_client(host=host, port=port)


def _quantile_query(table: str, latency_expr: str, window_ns: int) -> str:
    return f"""
    SELECT
        count() AS n,
        quantileTDigest(0.5)({latency_expr}) AS p50,
        quantileTDigest(0.9)({latency_expr}) AS p90,
        quantileTDigest(0.95)({latency_expr}) AS p95,
        quantileTDigest(0.99)({latency_expr}) AS p99,
        max({latency_expr}) AS max
    FROM {table}
    WHERE ingest_ts >= toUInt64(toUnixTimestamp64Nano(now64())) - {window_ns}
      AND {latency_expr} >= 0
    """


def _heatmap_query(table: str, latency_expr: str, window_ns: int, time_bucket_s: int, latency_bucket_us: int) -> str:
    return f"""
    SELECT
        toStartOfInterval(toDateTime64(ingest_ts / 1000000000.0, 3), INTERVAL {time_bucket_s} SECOND) AS ts_bucket,
        intDiv({latency_expr}, {latency_bucket_us}) * {latency_bucket_us} AS latency_bucket_us,
        count() AS cnt
    FROM {table}
    WHERE ingest_ts >= toUInt64(toUnixTimestamp64Nano(now64())) - {window_ns}
      AND {latency_expr} >= 0
    GROUP BY ts_bucket, latency_bucket_us
    ORDER BY ts_bucket, latency_bucket_us
    """


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate end-to-end latency report from ClickHouse.")
    parser.add_argument("--window-min", type=int, default=10)
    parser.add_argument("--time-bucket-s", type=int, default=10)
    parser.add_argument("--latency-bucket-us", type=int, default=500)
    parser.add_argument("--out-prefix", default="reports/e2e_latency")
    args = parser.parse_args()

    window_ns = args.window_min * 60 * 1_000_000_000

    client = _client()

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)

    results = {}

    # Market data E2E: ingest_ts - exch_ts
    md_latency_expr = "(ingest_ts - exch_ts) / 1000.0"
    md_summary = client.query(_quantile_query("hft.market_data", md_latency_expr, window_ns)).result_rows
    results["market_data_e2e_us"] = {
        "window_min": args.window_min,
        "n": md_summary[0][0] if md_summary else 0,
        "p50": md_summary[0][1] if md_summary else 0.0,
        "p90": md_summary[0][2] if md_summary else 0.0,
        "p95": md_summary[0][3] if md_summary else 0.0,
        "p99": md_summary[0][4] if md_summary else 0.0,
        "max": md_summary[0][5] if md_summary else 0.0,
    }

    md_heatmap = client.query(
        _heatmap_query("hft.market_data", md_latency_expr, window_ns, args.time_bucket_s, args.latency_bucket_us)
    ).result_rows

    heatmap_path = out_prefix.with_suffix(".market_data.heatmap.csv")
    with heatmap_path.open("w", encoding="utf-8") as fh:
        fh.write("ts_bucket,latency_bucket_us,count\n")
        for row in md_heatmap:
            fh.write(f"{row[0]},{row[1]},{row[2]}\n")

    # Orders internal latency if available
    try:
        order_summary = client.query(
            _quantile_query("hft.orders", "latency_us", window_ns)
        ).result_rows
        results["orders_latency_us"] = {
            "window_min": args.window_min,
            "n": order_summary[0][0] if order_summary else 0,
            "p50": order_summary[0][1] if order_summary else 0.0,
            "p90": order_summary[0][2] if order_summary else 0.0,
            "p95": order_summary[0][3] if order_summary else 0.0,
            "p99": order_summary[0][4] if order_summary else 0.0,
            "max": order_summary[0][5] if order_summary else 0.0,
        }
    except Exception:
        results["orders_latency_us"] = {"window_min": args.window_min, "n": 0}

    summary_path = out_prefix.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    print(f"Wrote {summary_path} and {heatmap_path}")


if __name__ == "__main__":
    main()
