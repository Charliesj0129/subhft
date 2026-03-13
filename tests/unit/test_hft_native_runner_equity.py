"""Tests for signal-based equity computation in HftNativeRunner.

Covers _estimate_step_ns, _apply_latency_to_positions, _compute_equity_curve,
and the integration point in _run_adapter_slice.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from research.backtest.hft_native_runner import (
    _apply_latency_to_positions,
    _compute_equity_curve,
    _estimate_step_ns,
)
from research.backtest.types import BacktestConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config(**overrides: object) -> BacktestConfig:
    """Build a BacktestConfig with sensible test defaults."""
    defaults: dict[str, object] = {
        "data_paths": ["/tmp/fake.npy"],
        "taker_fee_bps": 0.0,
        "maker_fee_bps": 0.0,
        "signal_threshold": 0.3,
        "max_position": 5,
        "initial_equity": 1_000_000.0,
        "submit_ack_latency_ms": 36.0,
        "modify_ack_latency_ms": 43.0,
        "cancel_ack_latency_ms": 47.0,
        "live_uplift_factor": 1.0,
        "local_decision_pipeline_latency_us": 0,
    }
    defaults.update(overrides)
    return BacktestConfig(**defaults)  # type: ignore[arg-type]


def _make_structured_array(n: int, *, local_ts_step_ns: int = 2_000_000) -> np.ndarray:
    """Create a structured array with local_ts field (nanosecond timestamps)."""
    dt = np.dtype([("local_ts", np.int64), ("bid_px", np.float64), ("ask_px", np.float64)])
    arr = np.zeros(n, dtype=dt)
    arr["local_ts"] = np.arange(n, dtype=np.int64) * local_ts_step_ns
    arr["bid_px"] = 100.0
    arr["ask_px"] = 101.0
    return arr


# ---------------------------------------------------------------------------
# _compute_equity_curve
# ---------------------------------------------------------------------------
class TestComputeEquityCurve:
    def test_basic(self) -> None:
        """Prices go up, long position -> equity increases."""
        prices = np.array([100.0, 101.0, 102.0], dtype=np.float64)
        positions = np.array([1.0, 1.0, 1.0], dtype=np.float64)
        config = _make_config(taker_fee_bps=0.0)

        equity = _compute_equity_curve(prices, positions, config)

        assert equity.shape == (3,)
        assert equity[0] == config.initial_equity
        # pnl_step[0] = 1.0 * (101-100) = 1.0
        # pnl_step[1] = 1.0 * (102-101) = 1.0
        assert equity[1] == pytest.approx(config.initial_equity + 1.0)
        assert equity[2] == pytest.approx(config.initial_equity + 2.0)

    def test_with_fees(self) -> None:
        """With fees, equity should be less than the zero-fee case."""
        # Position changes at tick 1 (0->1) and tick 3 (1->2) to generate
        # turnover entries that fall within the fee computation window.
        prices = np.array([100.0, 101.0, 102.0, 103.0, 104.0], dtype=np.float64)
        positions = np.array([0.0, 1.0, 1.0, 2.0, 2.0], dtype=np.float64)

        eq_no_fee = _compute_equity_curve(prices, positions, _make_config(taker_fee_bps=0.0))
        eq_fee = _compute_equity_curve(prices, positions, _make_config(taker_fee_bps=20.0))

        # With fees, final equity should be lower than without fees
        assert eq_fee[-1] < eq_no_fee[-1]

    def test_zero_position(self) -> None:
        """Zero position -> equity stays at initial."""
        prices = np.array([100.0, 105.0, 90.0, 110.0], dtype=np.float64)
        positions = np.zeros(4, dtype=np.float64)
        config = _make_config()

        equity = _compute_equity_curve(prices, positions, config)

        np.testing.assert_allclose(equity, config.initial_equity)

    def test_short_arrays(self) -> None:
        """Single-element arrays return initial equity."""
        prices = np.array([100.0], dtype=np.float64)
        positions = np.array([1.0], dtype=np.float64)
        config = _make_config()

        equity = _compute_equity_curve(prices, positions, config)

        assert equity.shape == (1,)
        assert equity[0] == config.initial_equity


# ---------------------------------------------------------------------------
# _apply_latency_to_positions
# ---------------------------------------------------------------------------
class TestApplyLatencyToPositions:
    def test_delays_positions(self) -> None:
        """Position change at tick 5 should arrive later, delayed by submit_steps."""
        n = 50
        data = _make_structured_array(n, local_ts_step_ns=2_000_000)
        desired = np.zeros(n, dtype=np.float64)
        desired[5:] = 1.0  # want to go long at tick 5

        config = _make_config(
            submit_ack_latency_ms=36.0,
            live_uplift_factor=1.0,
            local_decision_pipeline_latency_us=0,
        )
        executed = _apply_latency_to_positions(data, desired, config)

        # At tick 5, position should still be 0 (order hasn't arrived)
        assert executed[5] == 0.0
        # Eventually the position should become 1.0
        assert executed[-1] == 1.0
        # submit_steps = ceil(36_000_000 / 2_000_000) = 18
        expected_arrival = 5 + 18
        assert executed[expected_arrival] == 1.0
        assert executed[expected_arrival - 1] == 0.0

    def test_cancel_uses_cancel_latency(self) -> None:
        """Closing a position uses cancel latency, not submit latency."""
        n = 80
        data = _make_structured_array(n, local_ts_step_ns=1_000_000)
        desired = np.zeros(n, dtype=np.float64)
        desired[5:40] = 1.0  # long from tick 5 to 39, then close

        config = _make_config(
            submit_ack_latency_ms=10.0,
            cancel_ack_latency_ms=20.0,
            live_uplift_factor=1.0,
            local_decision_pipeline_latency_us=0,
        )
        executed = _apply_latency_to_positions(data, desired, config)

        # submit_steps = ceil(10ms / 1ms) = 10
        # cancel_steps = ceil(20ms / 1ms) = 20
        submit_arrival = 5 + 10
        assert executed[submit_arrival] == 1.0

        # Position close requested at tick 40 -> arrives at tick 40 + 20 = 60
        cancel_arrival = 40 + 20
        assert executed[cancel_arrival] == 0.0
        # One tick before cancel arrives, still holding
        assert executed[cancel_arrival - 1] == 1.0

    def test_single_element(self) -> None:
        """Single element returns as-is."""
        data = _make_structured_array(1)
        desired = np.array([1.0], dtype=np.float64)
        config = _make_config()

        result = _apply_latency_to_positions(data, desired, config)
        assert result.shape == (1,)
        assert result[0] == 1.0


# ---------------------------------------------------------------------------
# _estimate_step_ns
# ---------------------------------------------------------------------------
class TestEstimateStepNs:
    def test_with_local_ts(self) -> None:
        """Structured array with local_ts -> correct median step."""
        data = _make_structured_array(100, local_ts_step_ns=2_000_000)
        step = _estimate_step_ns(data)
        assert step == 2_000_000

    def test_with_exch_ts(self) -> None:
        """Falls back to exch_ts when local_ts not present."""
        dt = np.dtype([("exch_ts", np.int64), ("price", np.float64)])
        arr = np.zeros(50, dtype=dt)
        arr["exch_ts"] = np.arange(50, dtype=np.int64) * 5_000_000

        step = _estimate_step_ns(arr)
        assert step == 5_000_000

    def test_fallback_no_timestamps(self) -> None:
        """No timestamp fields -> fallback to 1_000_000 (1ms)."""
        dt = np.dtype([("price", np.float64), ("volume", np.float64)])
        arr = np.zeros(50, dtype=dt)

        step = _estimate_step_ns(arr)
        assert step == 1_000_000

    def test_unstructured_array(self) -> None:
        """Plain float array (no field names) -> fallback."""
        arr = np.zeros(50, dtype=np.float64)
        step = _estimate_step_ns(arr)
        assert step == 1_000_000


# ---------------------------------------------------------------------------
# _run_adapter_slice integration (mocked adapter)
# ---------------------------------------------------------------------------
class TestRunAdapterSliceEquity:
    def test_nonzero_sharpe_with_signals(self, tmp_path: object) -> None:
        """When signals are non-zero, the signal-based equity should not be flat."""
        from research.backtest.hft_native_runner import _run_adapter_slice

        # Create a fake NPZ with structured data (trending prices)
        n = 200
        dt = np.dtype([
            ("ev", np.uint64), ("exch_ts", np.int64), ("local_ts", np.int64),
            ("px", np.float64), ("qty", np.float64),
        ])
        data = np.zeros(n, dtype=dt)
        data["local_ts"] = np.arange(n, dtype=np.int64) * 2_000_000
        data["px"] = np.linspace(100.0, 110.0, n)
        data["qty"] = 1.0
        npz_path = str(tmp_path / "test.npz")  # type: ignore[operator]
        np.savez_compressed(npz_path, data=data)

        # Build a mock alpha
        mock_alpha = MagicMock()
        mock_alpha.manifest.alpha_id = "test_alpha"

        # Build signal log: alternating positive signals -> non-zero positions
        signal_log = [
            (i * 2_000_000, 0.5 if i % 20 < 10 else -0.5, 100.0 + i * 0.05)
            for i in range(n)
        ]

        config = _make_config(
            submit_ack_latency_ms=4.0,
            cancel_ack_latency_ms=6.0,
            modify_ack_latency_ms=5.0,
            live_uplift_factor=1.0,
            local_decision_pipeline_latency_us=0,
            signal_threshold=0.3,
            max_position=5,
            taker_fee_bps=0.0,
        )

        # Mock the adapter to avoid needing real hftbacktest
        mock_adapter = MagicMock()
        mock_adapter.equity_values = np.ones(n, dtype=np.float64) * config.initial_equity
        mock_adapter.run = MagicMock()

        mock_bridge_instance = MagicMock()
        mock_bridge_instance.signal_log = signal_log
        mock_bridge_instance.reset = MagicMock()

        with (
            patch("research.backtest.hft_native_runner.HftBacktestAdapter", return_value=mock_adapter),
            patch("research.backtest.hft_native_runner.AlphaStrategyBridge", return_value=mock_bridge_instance),
            patch("research.backtest.hft_native_runner._ADAPTER_AVAILABLE", True),
        ):
            equity, signals, mid_prices, positions = _run_adapter_slice(
                mock_alpha, npz_path, config,
            )

        # Equity should NOT be flat (signals produce positions, prices trend up)
        assert equity.size > 1
        equity_range = float(np.max(equity) - np.min(equity))
        assert equity_range > 0.0, "Equity curve is flat — signal-based PnL not computed"
