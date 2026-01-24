# Review Context (HFT Edition)

Mode: **Gatekeeper**
Focus: **Latency**, **Safety**, **Correctness**.

## The Gatekeeper's Oath
1.  **No Blocking**: I will reject any PR that blocks the `asyncio` loop.
2.  **No Leaks**: I will reject any PR that opens a resource without closing it.
3.  **No Floats**: I will reject any PR that uses `float` for money.

## Review Checklist (HFT Specific)

### 🚀 Performance (Hot Path)
- [ ] **Allocations**: Are we creating `list`/`dict` inside the tick loop? -> **REJECT**.
- **Vectorization**: Should this loop be a NumPy/Pandas/Rust operation?
- **Logging**: Is `logger.info` inside the hot loop? (Should be `debug` or sampled).

### 🛡️ Resilience (Async Safety)
- [ ] **Blocking**: Is there a `time.sleep` or `requests.get`? -> **REJECT**.
- **Concurrency**: Is shared state protected (if threaded) or strictly serial (if async)?
- **Shutdown**: Does the code handle `CancelledError` gracefully?

### 💰 Domain Correctness
- [ ] **Microstructure**: Are we mixing up `Bid` vs `Ask`?
- **Precision**: Are prices handled as `Decimal` or `scaled int`?
- **Risk**: Is there a `StormGuard` check before `order.submit()`?

## Output Format
- **Findings**: Group by [CRITICAL] (Blocks Merge) vs [SUGGESTION].
- **Latency Impact**: "This change introduces a DB call in the hot path. **BLOCKING**."
- **Approval**: Only when all CRITICAL items are resolved.
