## Summary

<!-- Brief description of the change -->

## Test Plan

- [ ] Unit tests added/updated
- [ ] Benchmark results reviewed

---

<!-- HFT Design Review: Required for feat: and perf: PRs -->
<!-- Delete sections below if this is a docs/chore/fix PR -->

## HFT Design Review

### Allocation Audit
<!-- Are there any heap allocations on the hot path? -->
<!-- List pre-allocated buffers, object pools, or numpy arrays used. -->
- [ ] No `malloc`/heap allocations on hot path
- [ ] Object pools used where applicable

### Latency Budget
<!-- What is the expected latency for this change? -->
<!-- Specify target latency (e.g., < 50us for normalizer, < 100us for LOB update). -->
- [ ] Latency target defined: ___ us
- [ ] Benchmark results within budget

### Threading Model
<!-- Does this change affect threading or async behavior? -->
<!-- Describe any new tasks, locks, or shared state. -->
- [ ] No blocking IO on main event loop
- [ ] Async boundaries documented

### Data Layout
<!-- How is data organized in memory? -->
<!-- Prefer Structure-of-Arrays over Array-of-Structures. -->
- [ ] Cache-friendly data layout (contiguous memory)
- [ ] No pointer chasing in hot path

### Failure Mode
<!-- What happens when this code fails? -->
<!-- Describe fallback behavior, error propagation, and recovery. -->
- [ ] Graceful degradation defined
- [ ] No `unwrap()` in Rust code reachable from Python
- [ ] Risk limits enforced
