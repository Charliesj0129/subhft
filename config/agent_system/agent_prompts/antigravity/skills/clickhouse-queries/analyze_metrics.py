#!/usr/bin/env python3
import subprocess
import json
import sys
import argparse

def query_clickhouse(sql):
    cmd = ["clickhouse-client", "--format", "JSON", "--query", sql]
    try:
        # Use simple curl if clickhouse-client not installed/configured in path
        # For now assuming docker exec or similar if needed, but let's try direct curl for broader compatibility
        # Actually, let's assume we can use curl to localhost:8123
        cmd = [
            "curl", "-s", "http://localhost:8123/",
            "--data-binary", sql + " FORMAT JSON"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None, result.stderr
        return json.loads(result.stdout), None
    except Exception as e:
        return None, str(e)

def analyze_latency(table="hft.market_data", minutes=10):
    # Use ingest_ts/exch_ts delta (ns -> us) to avoid needing latency_us column.
    sql = f"""
    SELECT
        count() as count,
        avg((ingest_ts - exch_ts) / 1000) as avg_latency_us,
        quantile(0.5)((ingest_ts - exch_ts) / 1000) as p50,
        quantile(0.9)((ingest_ts - exch_ts) / 1000) as p90,
        quantile(0.99)((ingest_ts - exch_ts) / 1000) as p99,
        max((ingest_ts - exch_ts) / 1000) as max_latency_us
    FROM {table}
    WHERE ingest_ts > 0
      AND exch_ts > 0
      AND ingest_ts >= (toUnixTimestamp64Nano(toDateTime64(now(), 9)) - {minutes} * 60 * 1000000000)
    """

    data, err = query_clickhouse(sql)
    if err:
        return {"status": "error", "error": err}

    if not isinstance(data, dict):
        return {"status": "error", "error": "Unexpected response"}

    return {"status": "ok", "metrics": data.get("data", [])}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=10)
    args = parser.parse_args()
    
    result = analyze_latency(minutes=args.minutes)
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
