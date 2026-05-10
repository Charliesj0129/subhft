---
name: hft-hot-path-dev
description: Use when writing or modifying code on the trading hot path (normalizer, LOB engine, feature engine, strategy runner, risk engine, order adapter). Enforces the 5 HFT Constitution Laws and latency discipline.
---

# HFT Hot-Path Development

Guard rails for code touching the tick-to-order critical path. Any code between `ShioajiClient.callback` and `OrderAdapter.place_order()` is hot path.

## Hot Path Boundary

```
ShioajiClient.callback → raw_queue → MarketDataService → Normalizer → LOBEngine
→ FeatureEngine → RingBufferBus → StrategyRunner → RiskEngine → OrderAdapter
```

Everything inside this chain MUST follow the 5 Laws.

## The 5 Laws Checklist

Run this checklist before any hot-path commit:

### Law 1: Allocator (No heap allocs in tick loop)

```python
# FORBIDDEN in hot path:
data = [x for x in range(n)]       # list comprehension = malloc
result = {"key": value}              # dict literal = malloc
obj = MyClass()                      # class instantiation = malloc
f"{variable}"                        # f-string = malloc

# REQUIRED:
self._buffer[i] = value              # pre-allocated buffer
self._reuse_dict.clear()             # reuse, don't recreate
```

**Verify**: `grep -n "= \[" <file>` — any list creation in loop is suspect.

### Law 2: Cache (Data packed for locality)

```python
# FORBIDDEN:
class Tick:                          # Array of Objects = pointer chasing
    price: float
    volume: int

# REQUIRED:
prices: np.ndarray                   # Structure of Arrays = contiguous
volumes: np.ndarray
# Or use __slots__ on dataclass
```

**Verify**: Hot-path classes must have `__slots__`. Check: `grep -n "class.*:" <file>` then verify `__slots__` present.

### Law 3: Async (No blocking > 1ms on event loop)

```python
# FORBIDDEN:
requests.get(url)                    # blocking HTTP
time.sleep(n)                        # blocking sleep
json.loads(big_payload)              # CPU-bound parsing
open(path).read()                    # blocking file I/O

# REQUIRED:
await client.get(url)                # async HTTP
await asyncio.sleep(n)               # async sleep
orjson.loads(payload)                # fast parser
await aiofiles.open(path)            # async file I/O
```

**Verify**: `grep -n "requests\.\|time\.sleep\|json\.loads\|open(" <file>`

### Law 4: Precision (No float for money)

```python
# FORBIDDEN:
price = 100.15                       # float = IEEE 754 error
balance += fill_price * qty          # float arithmetic

# REQUIRED:
price = 1001500                      # scaled int x10000
balance += fill_price_scaled * qty   # integer arithmetic
```

**Verify**: `grep -n "float\|price.*=.*\." <file>` in risk/order/execution paths.

### Law 5: Boundary (Zero-copy across Python/Rust FFI)

```python
# FORBIDDEN:
data = list(rust_result)             # copies entire buffer
py_list = [x for x in rust_array]   # materializes into Python heap

# REQUIRED:
buffer = rust_module.get_buffer()    # PyBuffer protocol
view = np.frombuffer(buffer)         # zero-copy view
```

**Verify**: Check any `rust_core` call site for unnecessary `.tolist()` or list comprehensions.

## Discipline Enforcement

The project has automated checks. Run before commit:

```bash
make discipline          # 9 AST rules (HFT-D001..HFT-P003)
make dependency-boundary # Import layer enforcement
make lint                # Ruff E/F/I/W/BLE/T20/UP/SIM
```

Key discipline rules:
- **HFT-D001** (CRITICAL): `except Exception: pass` — silent exception swallow
- **HFT-A001** (HIGH): Broker SDK imports outside `feed_adapter/<broker>/`
- **HFT-P001** (HIGH): `datetime.now()` / `time.time()` on hot path (use `timebase.now_ns()`)
- **HFT-P002** (HIGH): `import pandas` on hot path
- **HFT-P003** (HIGH): `requests.get/post` on hot path

## Hot-Path File Patterns

When modifying these files, extra scrutiny is required:

| File | Critical concern |
|------|-----------------|
| `feed_adapter/normalizer.py` | Scaled int output, Rust/Python path parity |
| `feed_adapter/lob_engine.py` | Pre-allocated book arrays, stats computation |
| `feature/engine.py` | Feature warmup guard, no alloc in `process_lob_stats()` |
| `strategy/runner.py` | Event dispatch latency, no blocking in `process_event()` |
| `risk/engine.py` | RiskFeedback completeness, pending counter integrity |
| `order/adapter.py` | Queue coalescing, deadline_ns monotonic |
| `engine/event_bus.py` | RingBuffer publish_nowait, overflow policy |

## Timestamp Discipline

```python
# FORBIDDEN:
import datetime
ts = datetime.datetime.now()         # system call overhead, not monotonic
ts = time.time()                     # epoch seconds, float precision loss

# REQUIRED:
from hft_platform.utils.timebase import now_ns
ts = now_ns()                        # monotonic-aligned nanoseconds
```

## Testing Hot-Path Changes

Every hot-path change MUST include:
1. **Unit test** with scaled int assertions (x10000)
2. **Monotonic time** assertions where timestamps are involved
3. **Fail-closed** test: if Rust path fails, Python fallback works
4. Run `make benchmark` to check for latency regression

```bash
make test-file FILE=tests/unit/test_normalizer.py
make hotpath-profile    # per-stage latency profile
```
