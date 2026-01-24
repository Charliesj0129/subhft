# The Darwin Gate (Evolutionary Governance)

Mode: **Survival of the Fittest**
Focus: **Measurable Performance Evolution**.

## The Evolution Guarantee
Code is only allowed to merge if it is **Faster** or **More Robost** than its predecessor.
We do not accept "Cleanup" on the Hot Path if it adds 500ns latency.

## The Authorized Enforcer
Only **`agents/performance-engineer.md`** can certify a Pass.
They MUST use **`hft-clickhouse` MCP** to generate the verification data.

## The Gatekeeper's Checklist
Any PR touching `@hot_path` methods (Feed, Strategy, Execution) MUST provide:

1.  **The Baseline**:
    *   Command: `pytest tests/benchmark/test_hotpath.py --benchmark-json=baseline.json`
    *   Result: `Mean: 54.3us`

2.  **The Challenger**:
    *   Command: `pytest tests/benchmark/test_hotpath.py --benchmark-json=challenger.json`
    *   Result: `Mean: 48.1us`

3.  **The Verdict**:
    *   Command: `py.test-benchmark compare baseline.json challenger.json`
    *   **FAIL** if `Speedup < 1.0x` (Slower).
    *   **FAIL** if `StdDev > Baseline + 20%` (Jittery).

## Behavior
- **Zero Trust**: "I think it's faster" is not valid. Show me the histogram.
- **Micro-Benchmarks**: Test the function in isolation using `timeit` / `pytest-benchmark`.
- **Macro-Benchmarks**: Replay 1 hour of Tick Data and measure `Tick-to-Trade` latency.

## Output Format (for `quant-architect` verification)
```json
{
  "gate_status": "PASS",
  "metric": "Tick-to-Signal Latency",
  "baseline_mean": "54.3us",
  "challenger_mean": "48.1us",
  "improvement": "+11.4%",
  "p_value": 0.001
}
```
