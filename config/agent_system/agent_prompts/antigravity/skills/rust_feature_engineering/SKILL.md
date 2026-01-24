---
name: rust-feature-engineering
description: Workflow for implementing high-performance features using Rust and PyO3. Enforces Profile-Driven Development (PDD) and Zero-Copy principles.
---

# Rust Feature Engineering Workflow

**Objective**: Implement calculation-heavy logic in Rust with zero overhead.

## Phase 1: Evidence & Prototype (Python)
**Goal**: Prove it's slow AND Prove it's correct.

1.  **Write Python Prototype**: Implement the math in pure Python/Numpy.
2.  **Verify Correctness**: Write a unit test with deterministic input/output.
3.  **Profile (Evidence)**: Run `py-spy record -o profile.svg --pid <PID>` or `cProfile`.
    *   *Requirement*: Identify the specific line/function consuming > 10% CPU time.
    *   *Decision*: If impact is negligible, **STOP**. Do not optimize prematurely.

## Phase 2: Implementation (Rust)
**Goal**: Calculate in Rust without copying memory.

1.  **Create Struct**: In `src/rust_core/src/lib.rs`.
2.  **Zero-Copy Strategy**:
    *   **Input**: Accept `PyReadonlyArray1<f64>` (Numpy View) instead of `Vec<f64>`.
    *   **Output**: Return `Py<PyArray1<f64>>` or standard types.
3.  **No Allocations**: Use `ndarray` views or pre-allocated buffers.

## Phase 3: Integration (PyO3)
**Goal**: Expose to Python seamlessly.

1.  **Bind**: Use `#[pyclass]` and `#[pymethods]`.
2.  **Build**: `maturin develop --release`.
3.  **Hint File**: Update `.pyi` file for IDE autocomplete.

## Phase 4: Verification (The Parity Check)
**Goal**: Rust result == Python result.

1.  **Parity Test**:
    ```python
    def test_parity():
        data = generate_data()
        py_res = python_impl(data)
        rs_res = rust_impl(data)
        np.testing.assert_allclose(py_res, rs_res, rtol=1e-10)
    ```
2.  **Benchmark Gate**:
    ```bash
    pytest tests/benchmark/ --benchmark-compare
    ```
    *   *Requirement*: Rust implementation must be at least **10x faster** to justify the complexity.
