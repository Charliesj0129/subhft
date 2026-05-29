# HFT Core Laws

Hot path/tick loop violations can create latency spikes or money loss.

1. Allocator: no heap allocation per tick. Reuse preallocated buffers, pools, ring buffers, or Rust.
2. Cache: pack data for locality. Prefer SoA/contiguous arrays/Rust `Vec`; avoid object graphs and pointer chasing.
3. Async: no blocking IO or >1 ms synchronous compute on the main event loop. Offload or use async primitives.
4. Precision: prices, balances, position/accounting values are discrete. Use scaled int x10000 or explicit safe types; no hot-path float price math.
5. Boundary: Python<->Rust crossings must avoid large copies. Prefer `PyBuffer`, Arrow/shared memory, or explicit FFI contracts.

Reject in review: hot-path `datetime.now()`/`time.time()`, `print()`, `requests`, `pandas`, broad silent exceptions, default mutable hot-path dataclasses, Rust `unwrap()` reachable from Python.
