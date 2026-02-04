# HFT Platform - Global Agents Context

> **Auto-Injected Context**: This file is automatically loaded by Codex CLI as the base context.
> **Consistency**: These rules align with `.agent/rules/` (Gemini) and `CLAUDE.md` (Claude).

## 1. The Constitution (Immutable HFT Laws)
**Violation of these laws causes critical latency penalties or financial loss.**

1.  **The Allocator Law (Memory)**
    *   **Rule**: No `malloc` or heap allocations on the Hot Path (Tick Loop).
    *   **Reason**: GC pauses and allocator contention destroy tail latency.
    *   **Action**: Use Object Pooling, Ring Buffers, or pre-allocated numpy arrays.

2.  **The Cache Law (Locality)**
    *   **Rule**: Data must be packed for spatial locality (Structure of Arrays > Array of Structures).
    *   **Reason**: CPU L1 Cache miss costs ~300 cycles.
    *   **Action**: Use contiguous memory (`numpy` arrays, Rust `Vec` with `#[repr(C)]`).

3.  **The Async Law (Event Loop)**
    *   **Rule**: No blocking IO (File/Network) or compute > 1ms on the main thread.
    *   **Reason**: Blocking the loop stops market data processing.
    *   **Action**: Offload to thread pool or use `asyncio`/`uvloop`.

4.  **The Precision Law (Correctness)**
    *   **Rule**: Never use `float` for prices, balances, or PnL.
    *   **Reason**: Floating point errors accumulate.
    *   **Action**: Use `Decimal` (accounting) or `scaled int` (hft internal).

5.  **The Boundary Law (FFI)**
    *   **Rule**: Zero-Copy Interfaces.
    *   **Reason**: Copying data between Python and Rust is too slow.
    *   **Action**: Use `PyBuffer` Protocol / Arrow memory format.

## 2. Project Standards
- **Python**: 3.12+, strict type hints, `ruff` linting.
- **Rust**: 1.75+, `clippy` strict, `pyo3` for bindings.
- **Logging**: `structlog` (JSON) only. No `print()`.
- **DB**: ClickHouse (Time-series), Redis (State).

## 3. Operations
- **System**: Use `sudo ./ops.sh` for setup, tuning, and testing.
- **Build**: Use `maturin` for Rust extensions.
