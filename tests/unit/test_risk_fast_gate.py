"""Unit tests for Numba JIT risk gate with kill switch.

Tests cover: normal pass, kill switch, price/qty validation,
shared memory isolation, and JIT warmup verification.
"""

import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hft_platform.risk.fast_gate import (
    FastGate,
    _check_order,
)


@pytest.fixture
def unique_shm_name():
    """Generate unique shared memory name for test isolation."""
    return f"test_kill_switch_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def gate(unique_shm_name):
    """Create FastGate with isolated shared memory.

    Note: FastGate now uses scaled integers for prices (default scale: 1_000_000).
    max_price=100_000_000_000 = 100,000.0 in display terms (100000 * 1_000_000)
    max_qty=1000 (integer quantity)
    """
    with patch.object(
        sys.modules["hft_platform.risk.fast_gate"],
        "KILL_SWITCH_SHM_NAME",
        unique_shm_name,
    ):
        g = FastGate(max_price=100_000_000_000, max_qty=1000, create_shm=True)
        yield g
        g.unlink()
        g.close()


class TestNormalPass:
    def test_valid_order_passes(self, gate):
        """Valid price and qty returns (True, 0).

        Uses scaled integers: 500 * 1_000_000 = 500_000_000
        """
        ok, code = gate.check(500_000_000, 10)
        assert ok is True
        assert code == 0

    def test_boundary_values_pass(self, gate):
        """Price and qty at boundaries still pass."""
        ok, code = gate.check(99_999_900_000, 999)
        assert ok is True
        assert code == 0

    def test_minimal_values_pass(self, gate):
        """Minimal positive values pass (scaled integer > 0)."""
        ok, code = gate.check(1, 1)
        assert ok is True
        assert code == 0


class TestKillSwitch:
    def test_kill_switch_active_rejects(self, gate):
        """When kill switch is active, all orders rejected with code 1."""
        gate.set_kill_switch(True)
        ok, code = gate.check(500_000_000, 10)
        assert ok is False
        assert code == 1

    def test_kill_switch_toggle(self, gate):
        """Kill switch can be toggled on and off."""
        ok, code = gate.check(500_000_000, 10)
        assert ok is True

        gate.set_kill_switch(True)
        ok, code = gate.check(500_000_000, 10)
        assert ok is False
        assert code == 1

        gate.set_kill_switch(False)
        ok, code = gate.check(500_000_000, 10)
        assert ok is True

    def test_kill_switch_value_persistence(self, gate):
        """Kill switch value persists in shared memory."""
        gate.set_kill_switch(True)
        assert gate.ks_array[0] == 1

        gate.set_kill_switch(False)
        assert gate.ks_array[0] == 0


class TestPriceValidation:
    def test_price_negative_rejected(self, gate):
        """Negative price rejected with code 2."""
        ok, code = gate.check(-100_000_000, 10)
        assert ok is False
        assert code == 2

    def test_price_zero_rejected(self, gate):
        """Zero price rejected with code 2."""
        ok, code = gate.check(0, 10)
        assert ok is False
        assert code == 2

    def test_price_exceeds_max_rejected(self, gate):
        """Price exceeding max rejected with code 3."""
        ok, code = gate.check(100_001_000_000, 10)
        assert ok is False
        assert code == 3

    def test_price_at_max_passes(self, gate):
        """Price exactly at max passes."""
        ok, code = gate.check(100_000_000_000, 10)
        assert ok is True
        assert code == 0


class TestQtyValidation:
    def test_qty_negative_rejected(self, gate):
        """Negative qty rejected with code 5."""
        ok, code = gate.check(500_000_000, -10)
        assert ok is False
        assert code == 5

    def test_qty_zero_rejected(self, gate):
        """Zero qty rejected with code 5."""
        ok, code = gate.check(500_000_000, 0)
        assert ok is False
        assert code == 5

    def test_qty_exceeds_max_rejected(self, gate):
        """Qty exceeding max rejected with code 4."""
        ok, code = gate.check(500_000_000, 1001)
        assert ok is False
        assert code == 4

    def test_qty_at_max_passes(self, gate):
        """Qty exactly at max passes."""
        ok, code = gate.check(500_000_000, 1000)
        assert ok is True
        assert code == 0


class TestValidationPriority:
    def test_kill_switch_checked_first(self, gate):
        """Kill switch rejection takes priority over other checks."""
        gate.set_kill_switch(True)
        ok, code = gate.check(-100_000_000, -10)
        assert ok is False
        assert code == 1

    def test_price_neg_before_max(self, gate):
        """Negative price checked before max price."""
        ok, code = gate.check(-100_000_000, 10)
        assert code == 2

    def test_price_before_qty(self, gate):
        """Price validation before qty validation."""
        ok, code = gate.check(-100_000_000, -10)
        assert code == 2


class TestSharedMemoryIsolation:
    def test_multiple_gates_share_kill_switch(self, unique_shm_name):
        """Multiple FastGate instances share same kill switch via shm."""
        with patch.object(
            sys.modules["hft_platform.risk.fast_gate"],
            "KILL_SWITCH_SHM_NAME",
            unique_shm_name,
        ):
            gate1 = FastGate(max_price=10_000_000_000, max_qty=100, create_shm=True)
            gate2 = FastGate(max_price=10_000_000_000, max_qty=100, create_shm=False)

            try:
                assert gate1.check(100_000_000, 10) == (True, 0)
                assert gate2.check(100_000_000, 10) == (True, 0)

                gate1.set_kill_switch(True)

                assert gate1.check(100_000_000, 10) == (False, 1)
                assert gate2.check(100_000_000, 10) == (False, 1)

                gate2.set_kill_switch(False)

                assert gate1.check(100_000_000, 10) == (True, 0)
                assert gate2.check(100_000_000, 10) == (True, 0)
            finally:
                gate1.unlink()
                gate1.close()
                gate2.close()


class TestJITWarmup:
    def test_jit_compiled_on_init(self, gate):
        """JIT compilation happens during __init__ warmup."""
        start = time.perf_counter_ns()
        for _ in range(1000):
            gate.check(500_000_000, 10)
        elapsed_ns = time.perf_counter_ns() - start

        avg_ns = elapsed_ns / 1000
        assert avg_ns < 100_000

    def test_check_order_jit_function_direct(self):
        """Test _check_order numba function directly with scaled integers."""
        kill_flag = np.array([0], dtype=np.uint8)

        ok, code = _check_order(500_000_000, 10, 10_000_000_000, 100, kill_flag)
        assert ok is True
        assert code == 0

        kill_flag[0] = 1
        ok, code = _check_order(500_000_000, 10, 10_000_000_000, 100, kill_flag)
        assert ok is False
        assert code == 1


class TestEdgeCases:
    def test_integer_precision_at_boundary(self, gate):
        """Integer precision near boundaries (no float imprecision issues)."""
        ok, code = gate.check(99_999_999_999, 10)
        assert ok is True

        ok, code = gate.check(100_000_000_001, 10)
        assert ok is False
        assert code == 3

    def test_very_small_values(self, gate):
        """Minimal positive integers pass."""
        ok, code = gate.check(1, 1)
        assert ok is True
        assert code == 0

    def test_different_max_configs(self, unique_shm_name):
        """Different gates can have different max configs."""
        with patch.object(
            sys.modules["hft_platform.risk.fast_gate"],
            "KILL_SWITCH_SHM_NAME",
            unique_shm_name,
        ):
            gate1 = FastGate(max_price=1_000_000_000, max_qty=10, create_shm=True)
            gate2 = FastGate(max_price=5_000_000_000, max_qty=50, create_shm=False)

            try:
                assert gate1.check(2_000_000_000, 20) == (False, 3)
                assert gate2.check(2_000_000_000, 20) == (True, 0)
            finally:
                gate1.unlink()
                gate1.close()
                gate2.close()


class TestLifecycle:
    def test_create_unlink_recreate(self, unique_shm_name):
        """Shared memory can be unlinked and recreated."""
        with patch.object(
            sys.modules["hft_platform.risk.fast_gate"],
            "KILL_SWITCH_SHM_NAME",
            unique_shm_name,
        ):
            gate1 = FastGate(max_price=10_000_000_000, max_qty=100, create_shm=True)
            gate1.set_kill_switch(True)
            gate1.unlink()
            gate1.close()

            gate2 = FastGate(max_price=10_000_000_000, max_qty=100, create_shm=True)
            try:
                ok, code = gate2.check(100_000_000, 10)
                assert ok is True
            finally:
                gate2.unlink()
                gate2.close()

    def test_close_without_unlink(self, unique_shm_name):
        """Close without unlink allows reconnection."""
        with patch.object(
            sys.modules["hft_platform.risk.fast_gate"],
            "KILL_SWITCH_SHM_NAME",
            unique_shm_name,
        ):
            gate1 = FastGate(max_price=10_000_000_000, max_qty=100, create_shm=True)
            gate1.set_kill_switch(True)
            gate1.close()

            gate2 = FastGate(max_price=10_000_000_000, max_qty=100, create_shm=False)
            try:
                ok, code = gate2.check(100_000_000, 10)
                assert ok is False
                assert code == 1
            finally:
                gate2.unlink()
                gate2.close()

    def test_unlink_idempotent(self, unique_shm_name):
        """Multiple unlink calls don't raise errors."""
        with patch.object(
            sys.modules["hft_platform.risk.fast_gate"],
            "KILL_SWITCH_SHM_NAME",
            unique_shm_name,
        ):
            gate = FastGate(max_price=10_000_000_000, max_qty=100, create_shm=True)
            gate.unlink()
            gate.unlink()
            assert True
            gate.close()
