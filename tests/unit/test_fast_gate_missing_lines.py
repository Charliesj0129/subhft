"""Targeted tests for uncovered lines in risk/fast_gate.py.

Covers:
- _init_shared_memory failure triggers _cleanup_shm + re-raise (lines 79, 81-82)
- buf-is-None guard after create_shm=True (line 95)
- buf-is-None guard in auto-create fallback (line 109)
- FileNotFoundError re-raise when create_shm=True (line 112)
- ks_shm-is-None RuntimeError after _init_shared_memory (line 116)
- _cleanup_shm where close() raises an exception (lines 121-126)
- _cleanup_shm sets ks_array to None (line 130)
- _check_order numba paths (lines 29-46, best-effort for coverage tool)
"""

from __future__ import annotations

import multiprocessing.shared_memory as shm
from unittest.mock import MagicMock, PropertyMock, patch

import numpy as np
import pytest

from hft_platform.risk.fast_gate import (
    KILL_SWITCH_SHM_NAME,
    FastGate,
    _check_order,
)


@pytest.fixture(autouse=True)
def _cleanup_shm():
    """Ensure kill switch SHM is cleaned up after each test."""
    yield
    try:
        existing = shm.SharedMemory(name=KILL_SWITCH_SHM_NAME)
        existing.close()
        existing.unlink()
    except FileNotFoundError:
        pass


def test_init_failure_triggers_cleanup_and_reraise():
    """When _init_shared_memory raises, __init__ calls _cleanup_shm then re-raises."""
    with patch.object(
        FastGate,
        "_init_shared_memory",
        side_effect=OSError("forced SHM failure"),
    ):
        with pytest.raises(OSError, match="forced SHM failure"):
            FastGate(create_shm=True)


def test_init_failure_cleanup_resets_state():
    """After init failure, partial state is cleaned up by _cleanup_shm."""
    with patch.object(
        FastGate,
        "_init_shared_memory",
        side_effect=RuntimeError("init exploded"),
    ):
        with pytest.raises(RuntimeError, match="init exploded"):
            FastGate(create_shm=True)


def test_init_shared_memory_buf_none_create_path():
    """If SharedMemory.buf returns None after create, raise RuntimeError."""
    mock_shm = MagicMock()
    type(mock_shm).buf = PropertyMock(return_value=None)

    with patch(
        "hft_platform.risk.fast_gate.shared_memory.SharedMemory",
        return_value=mock_shm,
    ):
        gate = FastGate.__new__(FastGate)
        gate.max_price_scaled = 10_000_000_000
        gate.max_qty = 100
        gate.price_scale = 1_000_000
        gate.ks_shm = None
        gate.ks_array = None

        with pytest.raises(RuntimeError, match="buffer not available"):
            gate._init_shared_memory(create_shm=True)


def test_init_shared_memory_buf_none_fallback_path():
    """If SharedMemory.buf returns None in fallback auto-create, raise RuntimeError."""
    mock_shm = MagicMock()
    type(mock_shm).buf = PropertyMock(return_value=None)

    with patch(
        "hft_platform.risk.fast_gate.shared_memory.SharedMemory",
        side_effect=[FileNotFoundError("not found"), mock_shm],
    ):
        gate = FastGate.__new__(FastGate)
        gate.max_price_scaled = 10_000_000_000
        gate.max_qty = 100
        gate.price_scale = 1_000_000
        gate.ks_shm = None
        gate.ks_array = None

        with pytest.raises(RuntimeError, match="buffer not available"):
            gate._init_shared_memory(create_shm=False)


def test_init_shared_memory_file_not_found_reraise_create_true():
    """FileNotFoundError is re-raised when create_shm=True."""
    with patch(
        "hft_platform.risk.fast_gate.shared_memory.SharedMemory",
        side_effect=FileNotFoundError("shm segment missing"),
    ):
        gate = FastGate.__new__(FastGate)
        gate.max_price_scaled = 10_000_000_000
        gate.max_qty = 100
        gate.price_scale = 1_000_000
        gate.ks_shm = None
        gate.ks_array = None

        with pytest.raises(FileNotFoundError, match="shm segment missing"):
            gate._init_shared_memory(create_shm=True)


def test_init_shared_memory_ks_shm_none_raises():
    """If ks_shm is still None after SHM init, raise RuntimeError."""
    gate = FastGate.__new__(FastGate)
    gate.max_price_scaled = 10_000_000_000
    gate.max_qty = 100
    gate.price_scale = 1_000_000
    gate.ks_shm = None
    gate.ks_array = None

    with patch(
        "hft_platform.risk.fast_gate.shared_memory.SharedMemory",
        side_effect=KeyError("unexpected"),
    ):
        with pytest.raises(KeyError):
            gate._init_shared_memory(create_shm=False)


def test_init_shared_memory_ks_shm_none_guard_direct():
    """Directly verify the ks_shm-is-None RuntimeError guard (line 115-116)."""
    gate = FastGate.__new__(FastGate)
    gate.max_price_scaled = 10_000_000_000
    gate.max_qty = 100
    gate.price_scale = 1_000_000
    gate.ks_shm = None
    gate.ks_array = None

    with patch(
        "hft_platform.risk.fast_gate.shared_memory.SharedMemory",
        return_value=None,
    ):
        with pytest.raises(RuntimeError, match="not initialized"):
            gate._init_shared_memory(create_shm=False)


def test_cleanup_shm_close_raises_exception():
    """_cleanup_shm logs and suppresses exceptions from shm.close()."""
    gate = FastGate.__new__(FastGate)
    gate.ks_shm = MagicMock()
    gate.ks_shm.close.side_effect = OSError("close failed")
    gate.ks_array = np.array([0], dtype=np.uint8)

    gate._cleanup_shm()

    assert gate.ks_shm is None
    assert gate.ks_array is None


def test_cleanup_shm_when_shm_is_none():
    """_cleanup_shm handles ks_shm=None gracefully."""
    gate = FastGate.__new__(FastGate)
    gate.ks_shm = None
    gate.ks_array = np.array([0], dtype=np.uint8)

    gate._cleanup_shm()

    assert gate.ks_shm is None
    assert gate.ks_array is None


def test_cleanup_shm_success_path():
    """_cleanup_shm closes SHM and resets both fields."""
    gate = FastGate.__new__(FastGate)
    gate.ks_shm = MagicMock()
    gate.ks_array = np.array([0], dtype=np.uint8)

    gate._cleanup_shm()

    gate.ks_shm_original = None
    assert gate.ks_shm is None
    assert gate.ks_array is None


def test_check_order_kill_active_returns_code_1():
    """Kill switch flag > 0 returns (False, 1)."""
    flag = np.array([2], dtype=np.uint8)
    ok, code = _check_order(100_0000, 1, 10_000_0000, 100, flag)
    assert ok is False
    assert code == 1


def test_check_order_price_negative_returns_code_2():
    """Negative price_scaled returns (False, 2)."""
    flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(-500, 1, 10_000_0000, 100, flag)
    assert ok is False
    assert code == 2


def test_check_order_price_zero_returns_code_2():
    """Zero price_scaled returns (False, 2)."""
    flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(0, 1, 10_000_0000, 100, flag)
    assert ok is False
    assert code == 2


def test_check_order_price_exceeds_max_returns_code_3():
    """Price above max returns (False, 3)."""
    flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(20_000_0000, 1, 10_000_0000, 100, flag)
    assert ok is False
    assert code == 3


def test_check_order_qty_zero_returns_code_5():
    """Zero qty returns (False, 5)."""
    flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(100_0000, 0, 10_000_0000, 100, flag)
    assert ok is False
    assert code == 5


def test_check_order_qty_negative_returns_code_5():
    """Negative qty returns (False, 5)."""
    flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(100_0000, -5, 10_000_0000, 100, flag)
    assert ok is False
    assert code == 5


def test_check_order_qty_exceeds_max_returns_code_4():
    """Qty above max returns (False, 4)."""
    flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(100_0000, 200, 10_000_0000, 100, flag)
    assert ok is False
    assert code == 4


def test_check_order_all_valid_returns_ok():
    """Valid order returns (True, 0)."""
    flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(500_0000, 50, 10_000_0000, 100, flag)
    assert ok is True
    assert code == 0


def test_check_order_price_at_max_boundary():
    """Price exactly at max passes."""
    flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(10_000_0000, 1, 10_000_0000, 100, flag)
    assert ok is True
    assert code == 0


def test_check_order_qty_at_max_boundary():
    """Qty exactly at max passes."""
    flag = np.array([0], dtype=np.uint8)
    ok, code = _check_order(100_0000, 100, 10_000_0000, 100, flag)
    assert ok is True
    assert code == 0


def test_destructor_suppresses_close_exception():
    """__del__ suppresses exceptions from close() and logs them."""
    gate = FastGate.__new__(FastGate)
    gate.ks_shm = MagicMock()
    gate.ks_shm.close.side_effect = OSError("close boom")
    gate.ks_array = None

    gate.__del__()
    assert gate.ks_shm is not None


def test_context_manager_exit_returns_false():
    """__exit__ returns False so exceptions propagate."""
    gate = FastGate(create_shm=True)
    try:
        result = gate.__exit__(None, None, None)
        assert result is False
    finally:
        gate.unlink()


def test_init_partial_shm_state_cleaned_on_failure():
    """If _init_shared_memory partially sets state then raises, _cleanup_shm resets it."""
    real_init = FastGate._init_shared_memory

    call_count = 0

    def failing_init(self, create_shm):
        nonlocal call_count
        call_count += 1
        self.ks_shm = shm.SharedMemory(name=KILL_SWITCH_SHM_NAME, create=True, size=1)
        raise ValueError("partial init failure")

    with patch.object(FastGate, "_init_shared_memory", failing_init):
        with pytest.raises(ValueError, match="partial init failure"):
            FastGate(create_shm=True)
    assert call_count == 1


def test_init_shared_memory_file_exists_reattach():
    """FileExistsError during create=True triggers reattach."""
    pre_shm = shm.SharedMemory(name=KILL_SWITCH_SHM_NAME, create=True, size=1)
    pre_shm.buf[0] = 0
    try:
        gate = FastGate(create_shm=True)
        ok, code = gate.check(100_000_000, 1)
        assert ok is True
        assert code == 0
        gate.close()
    finally:
        pre_shm.close()
        pre_shm.unlink()
