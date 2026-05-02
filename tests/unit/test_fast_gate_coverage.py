"""Coverage tests for hft_platform.risk.fast_gate — missing line ranges.

Targets: FastGate init, _check_order numba function, kill switch,
context manager, shared memory lifecycle, close, unlink, destructor,
and error codes.
"""

from __future__ import annotations

import multiprocessing.shared_memory as shm

import numpy as np
import pytest

from hft_platform.risk.fast_gate import (
    DEFAULT_PRICE_SCALE,
    KILL_SWITCH_SHM_NAME,
    FastGate,
    _check_order,
)


@pytest.fixture
def _cleanup_shm():
    """Ensure kill switch SHM is cleaned up after each test."""
    yield
    try:
        existing = shm.SharedMemory(name=KILL_SWITCH_SHM_NAME)
        existing.close()
        existing.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# _check_order numba function (lines 29-30, 33-34, 36-37, 40-41, 43-44, 46)
# ---------------------------------------------------------------------------


def test_check_order_ok():
    kill_flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(100_000_000, 1, 10_000_000_000, 100, kill_flag)
    assert ok is True
    assert code == 0


def test_check_order_kill_switch():
    kill_flag = np.array([1], dtype=np.uint8)
    ok, code = _check_order(100_000_000, 1, 10_000_000_000, 100, kill_flag)
    assert ok is False
    assert code == 1


def test_check_order_negative_price():
    kill_flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(-1, 1, 10_000_000_000, 100, kill_flag)
    assert ok is False
    assert code == 2


def test_check_order_zero_price():
    kill_flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(0, 1, 10_000_000_000, 100, kill_flag)
    assert ok is False
    assert code == 2


def test_check_order_price_too_high():
    kill_flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(20_000_000_000, 1, 10_000_000_000, 100, kill_flag)
    assert ok is False
    assert code == 3


def test_check_order_qty_too_high():
    kill_flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(100_000_000, 200, 10_000_000_000, 100, kill_flag)
    assert ok is False
    assert code == 4


def test_check_order_negative_qty():
    kill_flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(100_000_000, -1, 10_000_000_000, 100, kill_flag)
    assert ok is False
    assert code == 5


def test_check_order_zero_qty():
    kill_flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(100_000_000, 0, 10_000_000_000, 100, kill_flag)
    assert ok is False
    assert code == 5


# ---------------------------------------------------------------------------
# FastGate init and check (lines 79, 81-82, 95, 100, 109, 112)
# ---------------------------------------------------------------------------


def test_fast_gate_init_and_check(_cleanup_shm):
    gate = FastGate(create_shm=True)
    try:
        ok, code = gate.check(100_000_000, 1)
        assert ok is True
        assert code == 0
    finally:
        gate.unlink()
        gate.close()


def test_fast_gate_default_price_scale(_cleanup_shm):
    gate = FastGate(create_shm=True)
    try:
        assert gate.price_scale == DEFAULT_PRICE_SCALE
    finally:
        gate.unlink()
        gate.close()


# ---------------------------------------------------------------------------
# Kill switch (lines 116, 121-124, 126, 128-130)
# ---------------------------------------------------------------------------


def test_fast_gate_kill_switch(_cleanup_shm):
    gate = FastGate(create_shm=True)
    try:
        gate.set_kill_switch(True)
        ok, code = gate.check(100_000_000, 1)
        assert ok is False
        assert code == 1

        gate.set_kill_switch(False)
        ok2, code2 = gate.check(100_000_000, 1)
        assert ok2 is True
        assert code2 == 0
    finally:
        gate.unlink()
        gate.close()


# ---------------------------------------------------------------------------
# Context manager (lines 147, 153, 171, 180-181)
# ---------------------------------------------------------------------------


def test_fast_gate_context_manager(_cleanup_shm):
    with FastGate(create_shm=True) as gate:
        ok, code = gate.check(100_000_000, 1)
        assert ok is True
        gate.unlink()
    # After __exit__, shm should be closed
    assert gate.ks_shm is None


# ---------------------------------------------------------------------------
# close and unlink (lines 187, 189, 191)
# ---------------------------------------------------------------------------


def test_fast_gate_close(_cleanup_shm):
    gate = FastGate(create_shm=True)
    gate.unlink()
    gate.close()
    assert gate.ks_shm is None
    assert gate.ks_array is None


def test_fast_gate_double_close(_cleanup_shm):
    gate = FastGate(create_shm=True)
    gate.unlink()
    gate.close()
    gate.close()  # second close should not raise
    assert gate.ks_shm is None


def test_fast_gate_unlink_missing(_cleanup_shm):  # noqa: no-assert
    gate = FastGate(create_shm=True)
    gate.unlink()
    gate.unlink()  # should not raise even if already unlinked
    gate.close()


# ---------------------------------------------------------------------------
# check raises when not initialized
# ---------------------------------------------------------------------------


def test_fast_gate_check_raises_when_not_initialized(_cleanup_shm):
    gate = FastGate(create_shm=True)
    gate.unlink()
    gate.close()
    with pytest.raises(RuntimeError, match="Kill switch shared memory not initialized"):
        gate.check(100_000_000, 1)


def test_fast_gate_set_kill_switch_raises_when_not_initialized(_cleanup_shm):
    gate = FastGate(create_shm=True)
    gate.unlink()
    gate.close()
    with pytest.raises(RuntimeError, match="Kill switch shared memory not initialized"):
        gate.set_kill_switch(True)


# ---------------------------------------------------------------------------
# Destructor (lines 187, 189, 191)
# ---------------------------------------------------------------------------


def test_fast_gate_destructor(_cleanup_shm):
    gate = FastGate(create_shm=True)
    gate.unlink()
    gate.__del__()  # should not raise
    assert gate.ks_shm is None


# ---------------------------------------------------------------------------
# Existing SHM (line 100 — FileExistsError path)
# ---------------------------------------------------------------------------


def test_fast_gate_existing_shm(_cleanup_shm):
    gate1 = FastGate(create_shm=True)
    try:
        # Second create=True should handle FileExistsError
        gate2 = FastGate(create_shm=True)
        ok, code = gate2.check(100_000_000, 1)
        assert ok is True
        gate2.close()
    finally:
        gate1.unlink()
        gate1.close()


# ---------------------------------------------------------------------------
# Non-create mode (line 109 — auto-create fallback)
# ---------------------------------------------------------------------------


def test_fast_gate_auto_create_fallback(_cleanup_shm):
    """When create_shm=False but SHM does not exist, auto-create for dev mode."""
    gate = FastGate(create_shm=False)
    try:
        ok, code = gate.check(100_000_000, 1)
        assert ok is True
    finally:
        gate.unlink()
        gate.close()
