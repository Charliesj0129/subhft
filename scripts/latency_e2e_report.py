#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
from pathlib import Path


def _run_clickhouse(query: str, container: str) -> str:
    cmd = ["docker", "exec", container, "clickhouse-client", "--query", query]
    out = subprocess.check_output(cmd, text=True).strip()
    return out


def _parse_quantiles(line: str):
    raw = line.strip().strip("[]")
    if not raw:
        return []
    if "," in raw and " " not in raw:
        parts = [p for p in raw.split(",") if p]
    else:
        parts = [p for p in raw.split() if p]
    return [float(p) for p in parts]


def _ns_to_ms(value_ns):
    return float(value_ns) / 1e6


def _write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-min", type=int, default=10)
    parser.add_argument("--container", default=os.getenv("CH_CONTAINER", "clickhouse"))
    parser.add_argument("--out", default="reports/latency_e2e.json")
    parser.add_argument("--symbols-limit", type=int, default=50)
    parser.add_argument("--metrics-url", default=os.getenv("HFT_METRICS_URL", "http://localhost:9090/metrics"))
    args = parser.parse_args()

    ns_window = args.window_min * 60 * 1_000_000_000

    q_base = f"""
SELECT
    quantiles(0.5, 0.9, 0.95, 0.99)(ingest_ts - exch_ts) as q,
    avg(ingest_ts - exch_ts) as avg_ns,
    max(ingest_ts - exch_ts) as max_ns,
    count() as cnt
FROM hft.market_data
WHERE ingest_ts >= toInt64(toUnixTimestamp64Nano(now64())) - {ns_window}
""".strip()

    base_line = _run_clickhouse(q_base, args.container)
    q_vals = _parse_quantiles(base_line.split("\t")[0].replace("[", "").replace("]", ""))
    avg_ns = float(base_line.split("\t")[1])
    max_ns = float(base_line.split("\t")[2])
    cnt = int(base_line.split("\t")[3])

    q_syms = f"""
SELECT
    symbol,
    count() as c,
    quantile(0.95)(ingest_ts - exch_ts) as p95_ns,
    quantile(0.99)(ingest_ts - exch_ts) as p99_ns
FROM hft.market_data
WHERE ingest_ts >= toInt64(toUnixTimestamp64Nano(now64())) - {ns_window}
GROUP BY symbol
ORDER BY c DESC
LIMIT {args.symbols_limit}
""".strip()

    sym_lines = _run_clickhouse(q_syms, args.container)
    rows = []
    for line in sym_lines.splitlines():
        parts = [p for p in line.split("\t") if p]
        if len(parts) != 4:
            continue
        symbol, c, p95_ns, p99_ns = parts
        rows.append(
            {
                "symbol": symbol,
                "count": int(c),
                "p95_ms": _ns_to_ms(p95_ns),
                "p99_ms": _ns_to_ms(p99_ns),
            }
        )

    report = {
        "window_min": args.window_min,
        "count": cnt,
        "p50_ms": _ns_to_ms(q_vals[0]) if q_vals else None,
        "p90_ms": _ns_to_ms(q_vals[1]) if len(q_vals) > 1 else None,
        "p95_ms": _ns_to_ms(q_vals[2]) if len(q_vals) > 2 else None,
        "p99_ms": _ns_to_ms(q_vals[3]) if len(q_vals) > 3 else None,
        "avg_ms": _ns_to_ms(avg_ns),
        "max_ms": _ns_to_ms(max_ns),
        "symbols": rows,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True))

    # CSV summary
    csv_path = Path("reports/latency_by_symbol.csv")
    csv_lines = ["symbol,count,p95_ms,p99_ms"]
    for row in rows:
        csv_lines.append(
            f"{row['symbol']},{row['count']},{row['p95_ms']:.3f},{row['p99_ms']:.3f}"
        )
    _write_text(csv_path, "\n".join(csv_lines) + "\n")

    # Simple text heatmap
    heat_path = Path("reports/latency_heatmap.txt")
    heat_lines = ["symbol  p95_ms  p99_ms  heat"]
    for row in rows:
        p95 = row["p95_ms"] or 0.0
        blocks = int(min(40, max(1, p95 / 0.5)))
        heat_lines.append(f"{row['symbol']:<8} {p95:7.3f} {row['p99_ms']:7.3f} {'#' * blocks}")
    _write_text(heat_path, "\n".join(heat_lines) + "\n")

    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
