# rust_core/ — PyO3 hot-path kernels

Project-wide rules: `../AGENTS.md`. This file lists only what differs here.

- No `.unwrap()`/`.expect()` on any path reachable from Python. Public
  functions return `PyResult<T>`; map errors with `ok_or_else(|| PyValueError::new_err(..))?`.
- Release the GIL (`Python::allow_threads`) for CPU-heavy work. Take numpy input
  as `PyReadonlyArrayDyn` views, not `Vec<f64>` copies.
- `lib.rs` holds only PyO3 module registration; keep logic in pure-Rust
  modules that do not depend on Python so they test with plain `cargo test`.
- The built `.so` is bind-mounted on the production host (deploy Class A): a
  Rust change ships as a rebuilt artifact, not a source sync.
- Python fallbacks for Rust kernels must be explicit and observable (metric or log).
- Verify with `make build-rust`, `cargo clippy`, `cargo test`, and the
  parity tests that compare Python and Rust outputs.
