# HFT Performance Governance
**The Laws of Physics for this Platform.**

## 1. The Allocator Law (Memory)
**Principle**: `malloc` is slow. GC is unpredictable.
- **Rule**: Thou shalt not allocate new objects on the Hot Path (Tick Loop).
- **Remediation**: Use **Object Pooling** or see `skills/rust_feature_engineering`.

## 2. The Cache Law (Locality)
**Principle**: CPU L1 Cache miss costs ~300 cycles.
- **Rule**: Data must be packed for locality.
- **Remediation**: Use **SoA** in Rust. See `skills/rust_feature_engineering`.
    - *Bad*: `List[Order]` (Pointers chasing pointers).
    - *Good*: `OrderBook { prices: np.array, sizes: np.array }`.

## 3. The Async Law (Event Loop)
**Principle**: The Event Loop is a single thread. Blocking it stops the world.
- **Rule**: No synchronous IO or compute > 1ms.
- **Enforcement**:
    - **Development**: Enable `PYTHONASYNCIODEBUG=1` to detect slow callbacks.
    - **Production**: MUST use `uvloop` policy.
    - **Forbidden**: `requests`, `time.sleep`, large JSON parsing in main thread.

## 4. The Precision Law (Correctness)
**Principle**: Floating point errors lose money.
- **Rule**: Price is Discrete.
- **Enforcement**:
    - Use `Decimal` or `scaled int` (e.g., price * 1e8).
    - **Forbidden**: `float` for `price`, `balance`, or `pnl`.

## 5. The Boundary Law (FFI)
**Principle**: Crossing Python<->Rust is expensive if copied.
- **Rule**: Zero-Copy Interfaces.
- **Enforcement**:
    - Use `PyBuffer` Protocol.
    - Pass pointers/views, not clones.
