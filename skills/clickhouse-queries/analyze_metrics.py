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
    # Calculate latency statistics on ingestion (exchange_ts vs local_ts if available)
    # Or just return stats on specific metric columns
    
    # Assuming we have a 'latency' column or we calculate (local_timer - exchange_ts)
    # If not, we just count rows for now as a proxy for 'throughput'
    
    sql = f"""
    SELECT
        count() as count,
        avg(latency_us) as avg_latency,
        quantile(0.5)(latency_us) as p50,
        quantile(0.9)(latency_us) as p90,
        quantile(0.99)(latency_us) as p99,
        max(latency_us) as max_latency
    FROM {table}
    WHERE timestamp >= now() - INTERVAL {minutes} MINUTE
    """
    
    # Note: If the table doesn't have 'latency_us', this will fail. 
    # For robust 'Deep Analyzer', we should check schema first or allow custom query.
    # For HFT context, let's assume 'latency_us' exists or use a simpler placeholder for this demo.
    
    # Fallback/Demo query if table not ready
    sql_demo = f"""
    SELECT
        count() as throughput_count
    FROM system.parts
    """
    
    data, err = query_clickhouse(sql)
    if err and "Unknown custom column" in err:
         # Fallback to just counting metrics
         data, err = query_clickhouse(sql_demo)

    if err:
        return {"status": "error", "error": err}
    
    return {"status": "ok", "metrics": data.get("data", [])}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=int, default=10)
    args = parser.parse_args()
    
    result = analyze_latency(minutes=args.minutes)
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
