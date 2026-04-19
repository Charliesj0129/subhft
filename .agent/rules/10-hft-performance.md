# HFT Performance Guidelines

> **Context**: This file provides optimization checklists and anti-patterns for High-Frequency Trading.

## ⚡ Performance Checklist

### 1. Memory Management (The Hot Path)

- [ ] **Allocations**: Are we creating objects in the loop? (Dicts, Lists, Classes)
  - _Fix_: Pre-allocate buffers or reuse objects.
- [ ] **GC Pressure**: Is the garbage collector running during trading?
  - _Fix_: Disable GC during active trading hours (`gc.disable()`).
- [ ] **Copying**: Are we copying data unnecessarily?
  - _Fix_: Use views, slices, or references.

### 2. Data Structures

- [ ] **Locality**: Is data accessed sequentially?
  - _Check_: L1/L2 cache miss rates.
- [ ] **Layout**: Are we using `__slots__` for Python classes?
  - _Benefit_: 30% less memory, faster access.
- [ ] **Lookup**: Are lookups O(1)?
  - _Avoid_: O(n) linear scans.

### 3. Concurrency

- [ ] **Event Loop**: Is the lag > 1ms?
  - _Check_: `asyncio` loop lag metrics.
- [ ] **GIL**: Is Rust code holding the GIL unnecessarily?
  - _Fix_: `Python::allow_threads` for CPU-bound tasks.

## 🛑 Anti-Patterns (Red Flags)

| Pattern                      | Why it's bad                                                 | Replacement                          |
| ---------------------------- | ------------------------------------------------------------ | ------------------------------------ |
| `datetime.now()`             | System call overhead                                         | `loop.time()` (monotonic)            |
| `decimal.Decimal`            | Slow allocation. Forbidden in Hot Path, Allowed in Cold Path | Integer math (micros) / `scaled int` |
| `pandas.DataFrame` (in loop) | Heavy overhead                                               | `numpy` arrays / Dict of arrays      |
| `print()`                    | Blocking I/O                                                 | `structlog` (async/buffered)         |
| `try-except` (in loop)       | Stack unwinding cost                                         | Branching / Return codes             |
| `dataclass` (default)        | Mutable overhead                                             | `msgspec.Struct` or `NamedTuple`     |

## 🛠 Optimization Techniques

### 1. The "Warm-up" Strategy

JIT compilers (PyPy, Numba) and OS page caches need warm-up.

- **Action**: Run a dummy trading session for 10s before market open.

### 2. CPU Isolation

OS interrupts kill tail latency.

- **Action**: Use `./ops.sh isolate` to pin strategy threads to isolated cores.

### 3. Kernel Bypass (Advanced)

If standard networking is too slow:

- **Action**: Evaluate Solarflare / AF_XDP (Check `rust_core` capabilities).
