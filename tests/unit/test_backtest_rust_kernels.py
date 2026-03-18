"""WU-08: Parity tests for Rust backtest kernels.

Tests run against Python fallback if Rust module is not built.
"""

from __future__ import annotations

import numpy as np
import pytest

# Try importing Rust kernels; fall back to Python implementations
try:
    from hft_platform.rust_core import (
        apply_latency_to_positions as rust_apply_latency,
    )
    from hft_platform.rust_core import (
        signals_to_positions as rust_signals_to_positions,
    )

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

from research.backtest.hft_native_runner import _signals_to_positions as py_signals_to_positions


def _py_apply_latency(desired: np.ndarray, submit_steps: int) -> np.ndarray:
    """Simplified Python reference for latency application."""
    n = len(desired)
    executed = np.zeros(n, dtype=np.float64)
    pending_due = -1
    pending_target = 0.0
    for i in range(1, n):
        executed[i] = executed[i - 1]
        if pending_due >= 0 and i >= pending_due:
            executed[i] = pending_target
            pending_due = -1
        target = float(desired[i])
        if target == float(desired[i - 1]):
            continue
        if target == float(executed[i]):
            pending_due = -1
            continue
        pending_due = min(n - 1, i + submit_steps)
        pending_target = target
    return executed


# ---------------------------------------------------------------------------
# Python baseline tests (always run)
# ---------------------------------------------------------------------------
class TestPythonBaseline:
    def test_signals_to_positions_empty(self):
        result = py_signals_to_positions(np.array([]), 0.5, 3)
        assert len(result) == 0

    def test_signals_to_positions_basic(self):
        signals = np.array([0.0, 1.0, 1.0, -1.0, -1.0, 0.0])
        pos = py_signals_to_positions(signals, 0.5, 5)
        assert pos[0] == 0.0
        assert pos[1] == 1.0
        assert pos[2] == 2.0
        assert pos[3] == 1.0
        assert pos[4] == 0.0
        assert pos[5] == 0.0  # holds at 0

    def test_signals_to_positions_clamp(self):
        signals = np.ones(10)
        pos = py_signals_to_positions(signals, 0.5, 3)
        assert float(np.max(pos)) <= 3.0

    def test_apply_latency_basic(self):
        desired = np.array([0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0])
        executed = _py_apply_latency(desired, 2)
        # Position change at i=2 arrives at i=4
        assert executed[2] == 0.0
        assert executed[3] == 0.0
        assert executed[4] == 1.0

    def test_apply_latency_deterministic(self):
        desired = np.array([0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0])
        r1 = _py_apply_latency(desired, 2)
        r2 = _py_apply_latency(desired, 2)
        np.testing.assert_array_equal(r1, r2)


# ---------------------------------------------------------------------------
# Rust parity tests (skipped if Rust not built)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not RUST_AVAILABLE, reason="Rust backtest_kernels not built")
class TestRustParity:
    def test_signals_to_positions_parity(self):
        np.random.seed(42)
        signals = np.random.randn(1000)
        py_result = py_signals_to_positions(signals, 0.3, 5)
        rust_result = np.asarray(rust_signals_to_positions(signals.tolist(), 0.3, 5))
        np.testing.assert_allclose(py_result, rust_result, atol=1e-10)

    def test_apply_latency_parity(self):
        desired = np.array([0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 0.0, -1.0, -1.0, -1.0])
        py_result = _py_apply_latency(desired, 2)
        rust_result = np.asarray(rust_apply_latency(desired.tolist(), 2))
        np.testing.assert_allclose(py_result, rust_result, atol=1e-10)

    def test_signals_to_positions_empty_rust(self):
        result = rust_signals_to_positions([], 0.5, 3)
        assert len(result) == 0

    def test_clamp_rust(self):
        signals = [1.0] * 20
        result = rust_signals_to_positions(signals, 0.5, 3)
        assert max(result) <= 3.0
