
## 🛡️ Critical HFT Laws (THE CONSTITUTION)
**Violation of these laws causes critical latency penalties or financial loss.**

1.  **Allocator Law**: 
    *   ❌ **BAD**: `data = [x for x in range(1000)]` (in tick loop)
    *   ✅ **GOOD**: `self.buffer[i] = x` (Pre-allocated)
    *   **Rule**: No `malloc` or heap allocations on the Hot Path. Use Object Pooling.
2.  **Cache Law**: 
    *   ❌ **BAD**: Array of Objects (Pointer Chasing)
    *   ✅ **GOOD**: Structure of Arrays (Contiguous Memory)
    *   **Rule**: Data must be packed for locality. Use `numpy` or Rust `Vec`.
3.  **Async Law**: 
    *   ❌ **BAD**: `requests.get()`, `time.sleep()`, `json.loads(big_file)`
    *   ✅ **GOOD**: `await client.get()`, `await asyncio.sleep()`, `orjson` in thread pool
    *   **Rule**: No blocking IO or compute > 1ms on the main loop.
4.  **Precision Law**: 
    *   ❌ **BAD**: `price = 100.1 + 0.2` (Float)
    *   ✅ **GOOD**: `price = Decimal('100.1')` or `price_micros = 100100000`
    *   **Rule**: Never use `float` for prices/balances.
5.  **Boundary Law**: 
    *   ❌ **BAD**: Copying large lists between Python and Rust.
    *   ✅ **GOOD**: `PyBuffer` protocol, Arrow, Shared Memory.
    *   **Rule**: Zero-Copy Interfaces only.

## 🤖 Commands
- **Setup**: `sudo ./ops.sh setup` (Installs Docker, creates dirs)
- **Install**: `uv sync` or `pip install -e .`
- **Build Rust**: `maturin develop --manifest-path rust_core/Cargo.toml`
- **Test**: `pytest` (Unit) / `sudo ./ops.sh test` (Integration)
- **Lint**: `ruff check .`
- **Type Check**: `mypy .`
- **Run**: `python -m hft_platform run`

## 🧠 Project Intelligence
- **Architecture**: See `docs/ARCHITECTURE.md`
- **Detailed Rules**: See `.agent/rules/` (Auto-loaded).
  - `01-core-laws.md`: Full legal definitions.
  - `10-hft-performance.md`: Optimization techniques.
- **Skills**: Use `create-adr` to record decisions.

## 🎨 Coding Style (Strict)
- **Python**: Type hints (3.12+), `structlog` (no print), `pydantic`/`msgspec` for schemas.
- **Rust**: `clippy` strict, `pyo3` bindings, `thiserror` for errors.
- **Commits**: Conventional Commits (`feat:`, `fix:`, `perf:`).

## 🚩 Red Flags (Code Review)
- [ ] Any `float` usage in financial logic? -> **REJECT**
- [ ] `import pandas` in hot path? -> **REJECT** (Too slow)
- [ ] `unwrap()` in Rust code reachable from Python? -> **REJECT** (Panic risk)
- [ ] No unit tests for new logic? -> **REJECT**