# HFT Core Laws (The Constitution)

These are the immutable laws of the HFT Platform. Violation of these laws results in critical latency penalties or financial loss.

## 1. The Allocator Law (Memory)
**Principle**: `malloc` is slow. Garbage Collection (GC) is unpredictable (Stop-the-World).
- **Rule**: No heap allocations on the Hot Path (Tick Loop).
- **BAD**: `data = [x for x in range(1000)]` (in tick loop)
- **GOOD**: `self.buffer[i] = x` (pre-allocated)
- **Remediation**: Use Object Pooling, Ring Buffers, or Rust.
- **Verification**: Zero GC logs during trading session.

## 2. The Cache Law (Locality)
**Principle**: CPU L1 Cache miss costs ~300 cycles. Pointer chasing destroys performance.
- **Rule**: Data must be packed for locality (Structure of Arrays > Array of Structures).
- **BAD**: Array of Objects (pointer chasing).
- **GOOD**: Structure of Arrays (contiguous memory).
- **Remediation**: Use `numpy` contiguous arrays or Rust `Vec` with `#[repr(C)]`.

## 3. The Async Law (Event Loop)
**Principle**: The Python Event Loop is a single thread. Blocking it stops the world.
- **Rule**: No synchronous IO (File/Network) or compute > 1ms on the main loop.
- **Forbidden**: `requests`, `time.sleep`, large JSON parsing in main thread.
- **BAD**: `requests.get()`, `time.sleep()`, `json.loads(big_file)`.
- **GOOD**: `await client.get()`, `await asyncio.sleep()`, `orjson` in thread pool.
- **Remediation**: Offload to thread pool or use `aiohttp`/`asyncio`.

## 4. The Precision Law (Correctness)
**Principle**: Floating point errors accumulate and cause money loss.
- **Rule**: Price and Balance are Discrete. All prices are scaled int (x10000).
- **Forbidden**: `float` for `price`, `balance`, or `pnl`.
- **BAD**: `price = 100.1 + 0.2` (float).
- **GOOD**: `price = Decimal('100.1')` or `price_micros = 100100000`.
- **Remediation**: Use `Decimal` (slow but safe) or `scaled int` (fast and safe).

## 5. The Boundary Law (FFI)
**Principle**: Crossing Python<->Rust boundary is expensive if data is copied.
- **Rule**: Zero-Copy Interfaces only.
- **BAD**: Copying large lists between Python and Rust.
- **GOOD**: `PyBuffer` protocol, Arrow memory format, shared memory.
- **Remediation**: Use `PyBuffer` Protocol, Arrow memory format, or shared memory.
