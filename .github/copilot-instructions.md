# GitHub Copilot Instructions

You are an expert HFT developer assisting with a Python/Rust hybrid low-latency platform.

## ⚠️ Critical Constraints (HFT Laws)
1.  **Memory**: Avoid heap allocations in hot paths (tick/order processing). Suggest object pooling.
2.  **Concurrency**: Never block the asyncio event loop. No `time.sleep` or synchronous I/O.
3.  **Precision**: Never use floating point arithmetic for prices or financial calculations. Use fixed-point integers or Decimal.
4.  **Performance**: Prefer `numpy` vectorization or Rust extensions for math-heavy operations.
5.  **FFI**: When interfacing Python and Rust, use zero-copy protocols (PyBuffer).

## 📌 Coding Style
- **Python**: Enforce Python 3.12+ type hints. Use `structlog` for logging.
- **Rust**: idiomatic Rust with PyO3 bindings.
- **Tests**: Use `pytest`.

## 📂 Project Context
- **Rules**: Refer to `.agent/rules/` for detailed project guidelines.
- **Ops**: `./ops.sh` handles setup, tuning, and testing.
