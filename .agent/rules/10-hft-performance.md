# HFT Performance Guidelines

Optimization checklists and project-specific anti-patterns. For the 5 Core Laws (Allocator / Cache / Async / Precision / Boundary), see `01-core-laws.md`.

## Performance Checklist (Hot Path)

### Memory

- [ ] Objects created in tick loop? → pre-allocate buffers or reuse.
- [ ] GC running during trading? → `gc.disable()` during active hours.
- [ ] Unnecessary copies? → use views / slices / references.

### Data Structures

- [ ] `__slots__` on Python classes (30% less mem, faster access)?
- [ ] All lookups O(1)? No O(n) linear scans?

### Concurrency

- [ ] Event loop lag > 1ms? → check `asyncio` loop lag metrics.
- [ ] Rust code holding GIL unnecessarily? → `Python::allow_threads` for CPU-bound tasks.

## Anti-Patterns

| Pattern                      | Why bad              | Replacement                          |
| ---------------------------- | -------------------- | ------------------------------------ |
| `datetime.now()`             | syscall overhead     | `timebase.now_ns()` (monotonic)      |
| `decimal.Decimal` (hot path) | slow allocation      | scaled int (x10000)                  |
| `pandas.DataFrame` (in loop) | heavy overhead       | `numpy` arrays / dict of arrays      |
| `print()`                    | blocking I/O         | `structlog`                          |
| `try-except` (in loop)       | stack-unwind cost    | branching / return codes             |
| `dataclass` (default)        | mutable overhead     | `msgspec.Struct` or `NamedTuple`     |

## Optimization Techniques (project-specific)

1. **Warm-up**: run a dummy trading session for 10s before market open (JIT/page-cache prewarm).
2. **CPU isolation**: `./ops.sh isolate` to pin strategy threads to isolated cores.
3. **Kernel bypass (advanced)**: evaluate Solarflare / AF_XDP; check `rust_core` capabilities.
