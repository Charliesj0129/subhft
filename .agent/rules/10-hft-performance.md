# HFT Performance

Hot-path checklist:

- No per-tick object/list/dict allocation; preallocate/reuse.
- No unnecessary copies; use views/references/zero-copy FFI.
- O(1) lookups only; no scans in tick loops.
- Use `__slots__`, `msgspec.Struct`, `NamedTuple`, packed arrays, or Rust for hot data.
- Event-loop lag budget is 1 ms; CPU-heavy Rust should release the GIL.
- Disable/avoid GC during active trading only with explicit lifecycle handling.

Anti-patterns: `datetime.now()`/`time.time()` -> `timebase.now_ns()`; `Decimal` in hot path -> scaled int; `pandas` in loop -> arrays/Rust; `print()` -> `structlog`; exceptions for control flow -> branch/return codes.

Advanced ops such as CPU isolation or kernel-bypass experiments require explicit verification and docs.
