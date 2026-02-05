"""Benchmark tests for FastGate risk validation.

These benchmarks measure the hot-path performance of the Numba JIT
compiled risk gate used for pre-trade validation.
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

pytest.importorskip("pytest_benchmark")

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hft_platform.risk.fast_gate import FastGate, _check_order


def _unique_name() -> str:
    """Generate unique shared memory name for test isolation."""
    return f"bench_ks_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def gate():
    """Create FastGate with isolated shared memory for benchmarks."""
    shm_name = _unique_name()
    with patch.object(
        sys.modules["hft_platform.risk.fast_gate"],
        "KILL_SWITCH_SHM_NAME",
        shm_name,
    ):
        g = FastGate(max_price=100000.0, max_qty=1000.0, create_shm=True)
        yield g
        g.unlink()
        g.close()


# ---------------------------------------------------------------------------
# FastGate.check() Benchmarks
# ---------------------------------------------------------------------------
def test_bench_fast_gate_check_pass(benchmark, gate):
    """Benchmark FastGate.check() for passing orders."""
    result = benchmark(gate.check, 500.0, 10.0)
    assert result == (True, 0)


def test_bench_fast_gate_check_reject_price(benchmark, gate):
    """Benchmark FastGate.check() for price rejection."""
    result = benchmark(gate.check, 200000.0, 10.0)
    assert result == (False, 3)


def test_bench_fast_gate_check_reject_qty(benchmark, gate):
    """Benchmark FastGate.check() for qty rejection."""
    result = benchmark(gate.check, 500.0, 2000.0)
    assert result == (False, 4)


def test_bench_fast_gate_check_kill_switch(benchmark, gate):
    """Benchmark FastGate.check() with kill switch active."""
    gate.set_kill_switch(True)
    result = benchmark(gate.check, 500.0, 10.0)
    assert result == (False, 1)


# ---------------------------------------------------------------------------
# Raw Numba Function Benchmarks
# ---------------------------------------------------------------------------
@pytest.fixture
def numba_arrays():
    """Create numpy arrays for direct Numba function calls."""
    kill_flag = np.array([0], dtype=np.uint8)
    return kill_flag


def test_bench_check_order_numba_direct(benchmark, numba_arrays):
    """Benchmark raw _check_order Numba function."""
    kill_flag = numba_arrays
    result = benchmark(_check_order, 500.0, 10.0, 100000.0, 1000.0, kill_flag)
    assert result == (True, 0)


# ---------------------------------------------------------------------------
# Batch Validation Benchmarks
# ---------------------------------------------------------------------------
def test_bench_fast_gate_batch_100(benchmark, gate):
    """Benchmark 100 consecutive checks."""

    def batch_check():
        for i in range(100):
            gate.check(500.0 + i, 10.0)

    benchmark(batch_check)


def test_bench_fast_gate_batch_1000(benchmark, gate):
    """Benchmark 1000 consecutive checks."""

    def batch_check():
        for i in range(1000):
            gate.check(500.0 + (i % 100), 10.0)

    benchmark(batch_check)
