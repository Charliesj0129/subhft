# Development Context (HFT Edition)

Mode: Active HFT Engineering
Focus: **Performance**, Correctness, and maintainability.

## The Hybrid Dev Mantra
1.  **Orchestrate in Python**: For IO, setup, and glue code. (Use `asyncio`, `uvloop`)
2.  **Calculate in Rust**: For **ANY** data processing on the hot path (Tick -> Feature). (Use `PyO3`)

## Priorities
1.  **Latency Budget**: Does this add microseconds? If yes, justification required.
2.  **Memory Safety**: Zero-copy wherever possible. No new `list`/`dict` in `while True` loops.
3.  **Correctness**: Unit tests must cover edge cases (disconnects, bad data).

## Behavior
- **Hot Path Awareness**: Explicitly label functions as `@hot_path` (mental or actual decorator).
- **Zero Allocation**: Avoid `[x for x in list]` inside the tick loop. Use `numpy` in-place or Rust iterators.
- **Fail Fast**: Explicitly handle errors. `try-except-pass` is forbidden.

## Workflow
1.  **Spec First**: Don't code until you define the Input/Output and Latency Constraint.
2.  **Test Harness**: Write a test that keeps the event loop running (don't block).
3.  **Benchmark**: Use `pytest-benchmark` or simple `time.perf_counter_ns()` for critical sections.

## Tools to favor
- **Rust/Maturin**: For extending Python.
- **asyncio/uvloop**: For network IO.
- **pytest-asyncio**: For testing.
- **SnakeViz/cProfile**: For bottleneck analysis.

## Output
- **Change Impact**: "Replaced Python loop with Rust implementation -> 100x speedup."
- **Risk Analysis**: "New Rust extension requires build step."
