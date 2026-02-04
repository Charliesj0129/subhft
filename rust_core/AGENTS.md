# AGENTS.md - Rust Core Domain

> **Context**: This context is injected when working within `rust_core/`.
> **Inheritance**: Inherits global laws from `../AGENTS.md`.

## 1. Safety & Stability
- **NO PANICS**: Never use `.unwrap()` or `.expect()` in production code reachable from Python.
    - **Bad**: `let val = option.unwrap();`
    - **Good**: `let val = option.ok_or_else(|| PyValueError::new_err("Missing"))?;`
- **Error Handling**: All public functions must return `PyResult<T>`.

## 2. FFI & Performance
- **GIL Management**: Release GIL for long-running CPU tasks using `Python::allow_threads`.
- **Zero Copy**: Prefer `PyReadonlyArrayDyn` (numpy view) over `Vec<f64>` (copy) for input data.
- **Inlining**: Use `#[inline(always)]` for hot-path math functions.

## 3. Structure
- **Crate Layout**:
    - `src/lib.rs`: Only PyO3 module definitions.
    - `src/engine/`: Core logic (pure Rust, no Python dep if possible).
    - `src/types/`: Shared data structures.
