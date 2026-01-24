---
name: clickhouse-queries
description: Deep Analyzer. Performs statistical analysis on ClickHouse data. Returns P50/P99 latency and throughput metrics in JSON. Use for Performance Verification and Darwin Gate checks.
tools: Bash
---

# Deep Analyzer (clickhouse-queries)

**Mode**: Statistical Verification
**Output**: JSON Metric Report

## Usage
Run the analyzer script to get statistical bounds on system performance.

```bash
python3 skills/clickhouse-queries/analyze_metrics.py --minutes 5
```

## Integration with Darwin Gate
Use the `p99` output to verify against `contracts/SLAs.md`.
*   If `p99 > 1ms`: **FAIL** Darwin Gate.
*   If `count == 0`: **FAIL** Data Integrity.
