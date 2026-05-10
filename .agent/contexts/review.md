# Code Review Context

Mode: PR review, code analysis
Focus: HFT correctness, latency, safety, and regression risk

## Behavior
- Retrieve the touched contracts, call path, tests, and relevant `.agent/rules` before judging.
- Prioritize production trading risk, financial correctness, hot-path regressions, and missing fail-closed behavior.
- Report findings with file/line evidence and concrete remediation.
- Keep summaries secondary to actionable findings.

## Review Checklist
- [ ] Float prices or precision loss in financial logic
- [ ] Blocking IO, sleeps, pandas, or allocations on hot path
- [ ] Time source violations (`datetime.now()` / `time.time()` instead of `timebase.now_ns()`)
- [ ] Risk/order/execution paths fail open instead of fail closed
- [ ] Broker latency/backtest assumptions are unrealistic or undocumented
- [ ] Rust panic/unwrap can cross into Python runtime
- [ ] Missing focused tests for changed behavior

## Output Format
Group findings by file, severity first
