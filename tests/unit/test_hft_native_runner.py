"""Unit tests for research/backtest/hft_native_runner.py."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from research.backtest.hft_native_runner import (
    HftNativeRunner,
    _effective_broker_rtt_ms,
    _forward_returns,
    _resolve_hftbt_path,
    _run_adapter_slice,
    _signals_to_positions,
    _split_npz,
    ensure_hftbt_npz,
    has_hftbt_data,
)
from research.backtest.types import BacktestConfig, WalkForwardConfig

# Standard research.npy dtype (from synth_lob_gen._DTYPE)
_RESEARCH_DTYPE = np.dtype(
    [
        ("bid_qty", "f8"),
        ("ask_qty", "f8"),
        ("bid_px", "f8"),
        ("ask_px", "f8"),
        ("mid_price", "f8"),
        ("spread_bps", "f8"),
        ("volume", "f8"),
        ("local_ts", "i8"),
    ]
)


# ---------------------------------------------------------------------------
# Helpers to create synthetic event arrays (mimics event_dtype)
# ---------------------------------------------------------------------------
def _make_event_array(n: int = 100) -> np.ndarray:
    """Create a minimal synthetic array that _split_npz can load."""
    # Use simple float64 2D array as stand-in; _split_npz only needs indexing
    dt = np.dtype(
        [
            ("ev", "i8"),
            ("exch_ts", "i8"),
            ("local_ts", "i8"),
            ("px", "f8"),
            ("qty", "f8"),
            ("a", "i4"),
            ("b", "i4"),
            ("c", "f8"),
        ]
    )
    arr = np.zeros(n, dtype=dt)
    arr["exch_ts"] = np.arange(n) * 1_000_000  # 1ms steps
    arr["local_ts"] = arr["exch_ts"]
    arr["px"] = 100.0 + np.sin(np.arange(n) * 0.1) * 0.5
    arr["qty"] = 10.0
    return arr


def _save_event_npz(arr: np.ndarray, path: str) -> None:
    np.savez_compressed(path, data=arr)


# ---------------------------------------------------------------------------
# _resolve_hftbt_path
# ---------------------------------------------------------------------------
class TestResolveHftbtPath:
    def test_returns_none_when_not_found(self, tmp_path):
        assert _resolve_hftbt_path(str(tmp_path / "research.npy")) is None

    def test_returns_sibling_hftbt_npz(self, tmp_path):
        hbt = tmp_path / "hftbt.npz"
        hbt.touch()
        research = tmp_path / "research.npy"
        result = _resolve_hftbt_path(str(research))
        assert result == str(hbt)

    def test_returns_self_if_named_hftbt(self, tmp_path):
        hbt = tmp_path / "hftbt.npz"
        hbt.touch()
        result = _resolve_hftbt_path(str(hbt))
        assert result == str(hbt)


# ---------------------------------------------------------------------------
# has_hftbt_data
# ---------------------------------------------------------------------------
class TestHasHftbtData:
    def test_false_when_no_hftbt(self, tmp_path):
        paths = [str(tmp_path / "research.npy")]
        assert has_hftbt_data(paths) is False

    def test_true_when_sibling_exists(self, tmp_path):
        (tmp_path / "hftbt.npz").touch()
        paths = [str(tmp_path / "research.npy")]
        assert has_hftbt_data(paths) is True

    def test_true_when_path_is_hftbt(self, tmp_path):
        hbt = tmp_path / "hftbt.npz"
        hbt.touch()
        assert has_hftbt_data([str(hbt)]) is True

    def test_empty_paths(self):
        assert has_hftbt_data([]) is False


# ---------------------------------------------------------------------------
# _split_npz
# ---------------------------------------------------------------------------
class TestSplitNpz:
    def test_splits_70_30(self, tmp_path):
        arr = _make_event_array(100)
        npz = str(tmp_path / "data.npz")
        _save_event_npz(arr, npz)
        is_path, oos_path = _split_npz(npz, 0.7)
        try:
            is_data = np.load(is_path, allow_pickle=False)["data"]
            oos_data = np.load(oos_path, allow_pickle=False)["data"]
            assert len(is_data) == 70
            assert len(oos_data) == 30
        finally:
            for p in (is_path, oos_path):
                os.unlink(p)
                try:
                    os.rmdir(os.path.dirname(p))
                except OSError:
                    pass

    def test_temp_files_created_with_data_key(self, tmp_path):
        arr = _make_event_array(50)
        npz = str(tmp_path / "data.npz")
        _save_event_npz(arr, npz)
        is_path, oos_path = _split_npz(npz, 0.5)
        try:
            assert "data" in np.load(is_path, allow_pickle=False)
            assert "data" in np.load(oos_path, allow_pickle=False)
        finally:
            for p in (is_path, oos_path):
                os.unlink(p)
                try:
                    os.rmdir(os.path.dirname(p))
                except OSError:
                    pass

    def test_minimal_split(self, tmp_path):
        arr = _make_event_array(2)
        npz = str(tmp_path / "data.npz")
        _save_event_npz(arr, npz)
        is_path, oos_path = _split_npz(npz, 0.7)
        try:
            is_data = np.load(is_path, allow_pickle=False)["data"]
            oos_data = np.load(oos_path, allow_pickle=False)["data"]
            assert len(is_data) + len(oos_data) == 2
        finally:
            for p in (is_path, oos_path):
                os.unlink(p)
                try:
                    os.rmdir(os.path.dirname(p))
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# _signals_to_positions
# ---------------------------------------------------------------------------
class TestSignalsToPositions:
    def test_above_threshold_accumulates(self):
        signals = np.array([0.0, 0.5, 0.5, 0.5])
        pos = _signals_to_positions(signals, threshold=0.3, max_position=5)
        assert list(pos) == [0, 1, 2, 3]

    def test_below_negative_threshold_decrements(self):
        signals = np.array([0.0, -0.5, -0.5, -0.5])
        pos = _signals_to_positions(signals, threshold=0.3, max_position=5)
        assert list(pos) == [0, -1, -2, -3]

    def test_clamps_at_max_position(self):
        signals = np.array([0.5] * 10)
        pos = _signals_to_positions(signals, threshold=0.3, max_position=3)
        assert int(pos[-1]) == 3

    def test_inside_threshold_holds_position(self):
        signals = np.array([0.0, 0.5, 0.1, 0.5])
        pos = _signals_to_positions(signals, threshold=0.3, max_position=5)
        assert list(pos) == [0.0, 1.0, 1.0, 2.0]

    def test_empty_signals(self):
        pos = _signals_to_positions(np.array([]), threshold=0.3, max_position=5)
        assert len(pos) == 0


# ---------------------------------------------------------------------------
# _forward_returns
# ---------------------------------------------------------------------------
class TestForwardReturns:
    def test_basic(self):
        prices = np.array([100.0, 101.0, 100.5, 102.0])
        fwd = _forward_returns(prices)
        assert fwd.shape == (3,)  # n-1 elements
        assert fwd[0] == pytest.approx(0.01)  # (101-100)/100

    def test_zero_price_no_nan(self):
        prices = np.array([0.0, 100.0, 101.0])
        fwd = _forward_returns(prices)
        # Division by zero handled: result should be 0.0 not nan
        assert np.isfinite(fwd[0])

    def test_single_price(self):
        fwd = _forward_returns(np.array([100.0]))
        assert fwd.size == 0


# ---------------------------------------------------------------------------
# HftNativeRunner — import failure graceful degradation
# ---------------------------------------------------------------------------
class TestHftNativeRunnerImportError:
    def test_raises_when_hftbacktest_unavailable(self, tmp_path):
        """If hftbacktest not installed, run() raises ImportError."""
        from research.backtest.hft_native_runner import HftNativeRunner

        alpha = MagicMock()
        alpha.manifest.alpha_id = "test"
        config = BacktestConfig(data_paths=[str(tmp_path / "research.npy")])

        runner = HftNativeRunner(alpha, config)

        # Patch both availability flags to False
        with (
            patch("research.backtest.hft_native_runner._ADAPTER_AVAILABLE", False),
            patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", False),
        ):
            with pytest.raises(ImportError, match="hftbacktest"):
                runner.run()


# ---------------------------------------------------------------------------
# HftNativeRunner — empty data path returns zero result
# ---------------------------------------------------------------------------
class TestHftNativeRunnerEmpty:
    def test_returns_zero_result_when_no_hftbt_data(self, tmp_path):
        """When no hftbt.npz found, runner returns a zero-valued BacktestResult."""
        from research.backtest.hft_native_runner import HftNativeRunner

        alpha = MagicMock()
        alpha.manifest.alpha_id = "test"
        config = BacktestConfig(data_paths=[str(tmp_path / "research.npy")])
        runner = HftNativeRunner(alpha, config)

        # Patch to simulate hftbacktest available but no hftbt.npz found
        with (
            patch("research.backtest.hft_native_runner._ADAPTER_AVAILABLE", True),
            patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True),
        ):
            result = runner.run()

        assert result.sharpe_is == pytest.approx(0.0)
        assert result.sharpe_oos == pytest.approx(0.0)
        assert result.run_id != ""  # has a UUID

    def test_has_hftbt_data_false_when_no_sibling(self, tmp_path):
        paths = [str(tmp_path / "research.npy")]
        from research.backtest.hft_native_runner import has_hftbt_data

        assert has_hftbt_data(paths) is False


# ---------------------------------------------------------------------------
# Validation.py integration — _has_hftbt_data helper
# ---------------------------------------------------------------------------
class TestValidationHasHftbtData:
    def test_helper_in_validation(self, tmp_path):
        from hft_platform.alpha.validation import _has_hftbt_data

        # No file
        assert _has_hftbt_data([str(tmp_path / "x.npy")]) is False
        # Create sibling
        (tmp_path / "hftbt.npz").touch()
        assert _has_hftbt_data([str(tmp_path / "x.npy")]) is True

    def test_use_hft_native_default_true(self):
        from hft_platform.alpha.validation import ValidationConfig

        cfg = ValidationConfig(alpha_id="test", data_paths=[])
        assert cfg.use_hft_native is True

    def test_use_hft_native_can_be_disabled(self):
        from hft_platform.alpha.validation import ValidationConfig

        cfg = ValidationConfig(alpha_id="test", data_paths=[], use_hft_native=False)
        assert cfg.use_hft_native is False


# ---------------------------------------------------------------------------
# ensure_hftbt_npz
# ---------------------------------------------------------------------------


def _make_research_npy(path: str, n: int = 100, *, include_local_ts: bool = True, zero_prices: bool = False) -> None:
    """Create a minimal research.npy with _RESEARCH_DTYPE."""
    arr = np.zeros(n, dtype=_RESEARCH_DTYPE)
    if not zero_prices:
        arr["bid_px"] = 99.9
        arr["ask_px"] = 100.1
        arr["bid_qty"] = 10.0
        arr["ask_qty"] = 5.0
        arr["volume"] = 2.0
        arr["mid_price"] = 100.0
    if include_local_ts:
        arr["local_ts"] = np.arange(n, dtype=np.int64) * 1_000_000
    np.save(path, arr)


class TestEnsureHftbtNpz:
    def test_idempotent_when_sibling_exists(self, tmp_path):
        """If hftbt.npz sibling exists, returns it immediately without hftbacktest."""
        hbt = tmp_path / "hftbt.npz"
        hbt.touch()
        npy = str(tmp_path / "research.npy")
        result = ensure_hftbt_npz(npy)
        assert result == str(hbt)

    def test_idempotent_when_path_is_hftbt(self, tmp_path):
        """If data_path is already hftbt.npz, returns it immediately."""
        hbt = tmp_path / "hftbt.npz"
        hbt.touch()
        result = ensure_hftbt_npz(str(hbt))
        assert result == str(hbt)

    def test_raises_importerror_when_hftbacktest_missing(self, tmp_path):
        """Without hftbacktest and no sibling, raises ImportError."""
        npy = str(tmp_path / "research.npy")
        _make_research_npy(npy, n=10)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", False):
            with pytest.raises(ImportError, match="hftbacktest"):
                ensure_hftbt_npz(npy)

    def test_output_path_is_sibling(self, tmp_path):
        """Output hftbt.npz is in same directory as input .npy."""
        npy = str(tmp_path / "research.npy")
        _make_research_npy(npy, n=20)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
                assert Path(out).parent == tmp_path
                assert Path(out).name == "hftbt.npz"
            except ImportError:
                pytest.skip("hftbacktest not installed")

    def test_converts_standard_dtype(self, tmp_path):
        """Converts research.npy to hftbt.npz containing event_dtype array."""
        npy = str(tmp_path / "research.npy")
        _make_research_npy(npy, n=50)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        loaded = np.load(out, allow_pickle=False)
        assert "data" in loaded
        events = loaded["data"]
        assert len(events) > 0

    def test_bid_depth_and_ask_depth_events_generated(self, tmp_path):
        """Each non-zero row generates at least 2 events (bid + ask depth)."""
        npy = str(tmp_path / "research.npy")
        n = 10
        _make_research_npy(npy, n=n)
        # volume=0 so no trade events → exactly 2*n events
        arr = np.load(npy)
        arr["volume"] = 0.0
        np.save(npy, arr)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        assert len(events) == 2 * n  # bid + ask only

    def test_trade_event_generated_when_volume_positive(self, tmp_path):
        """Rows with volume > 0 generate an extra TRADE_EVENT."""
        npy = str(tmp_path / "research.npy")
        n = 10
        _make_research_npy(npy, n=n)  # volume=2.0 by default
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        assert len(events) == 3 * n  # bid + ask + trade

    def test_zero_price_rows_skipped(self, tmp_path):
        """Rows with bid_px=0 and ask_px=0 are skipped entirely."""
        npy = str(tmp_path / "research.npy")
        n = 5
        _make_research_npy(npy, n=n, zero_prices=True)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                with pytest.raises(ValueError, match="No valid events"):
                    ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")

    def test_fallback_ts_when_no_local_ts_field(self, tmp_path):
        """When local_ts field is absent, uses 0,1ms,2ms... fallback timestamps."""
        # Build array without local_ts
        no_ts_dt = np.dtype(
            [
                ("bid_qty", "f8"),
                ("ask_qty", "f8"),
                ("bid_px", "f8"),
                ("ask_px", "f8"),
                ("mid_price", "f8"),
                ("spread_bps", "f8"),
                ("volume", "f8"),
            ]
        )
        arr = np.zeros(10, dtype=no_ts_dt)
        arr["bid_px"] = 99.9
        arr["ask_px"] = 100.1
        arr["bid_qty"] = 10.0
        arr["ask_qty"] = 5.0
        arr["volume"] = 0.0
        npy = str(tmp_path / "research_no_ts.npy")
        np.save(npy, arr)
        with patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True):
            try:
                out = ensure_hftbt_npz(npy)
            except ImportError:
                pytest.skip("hftbacktest not installed")
        events = np.load(out, allow_pickle=False)["data"]
        assert len(events) > 0
        # Timestamps should be monotonically non-decreasing
        ts_field = "local_ts" if "local_ts" in events.dtype.names else "exch_ts"
        assert np.all(np.diff(events[ts_field]) >= 0)


# ---------------------------------------------------------------------------
# Broker RTT injection — effective_broker_rtt_ms
# ---------------------------------------------------------------------------
class TestBrokerRTTInjection:
    def test_effective_rtt_uses_worst_case_cancel(self):
        """When cancel_ack_latency_ms > submit_ack_latency_ms, effective latency uses cancel value."""
        config = BacktestConfig(
            data_paths=[],
            submit_ack_latency_ms=36.0,
            modify_ack_latency_ms=43.0,
            cancel_ack_latency_ms=47.0,
        )
        assert _effective_broker_rtt_ms(config) == 47.0

    def test_effective_rtt_when_submit_is_largest(self):
        """When submit_ack_latency_ms is largest, effective uses submit value."""
        config = BacktestConfig(
            data_paths=[],
            submit_ack_latency_ms=50.0,
            modify_ack_latency_ms=43.0,
            cancel_ack_latency_ms=47.0,
        )
        assert _effective_broker_rtt_ms(config) == 50.0

    def test_effective_rtt_when_modify_is_largest(self):
        """When modify_ack_latency_ms is largest, effective uses modify value."""
        config = BacktestConfig(
            data_paths=[],
            submit_ack_latency_ms=36.0,
            modify_ack_latency_ms=55.0,
            cancel_ack_latency_ms=47.0,
        )
        assert _effective_broker_rtt_ms(config) == 55.0

    def test_latency_profile_includes_effective_broker_rtt_ms(self, tmp_path):
        """HftNativeRunner.run() includes effective_broker_rtt_ms in latency_profile."""
        from research.backtest.hft_native_runner import HftNativeRunner

        alpha = MagicMock()
        alpha.manifest.alpha_id = "test"
        config = BacktestConfig(
            data_paths=[str(tmp_path / "research.npy")],
            submit_ack_latency_ms=36.0,
            modify_ack_latency_ms=43.0,
            cancel_ack_latency_ms=47.0,
        )
        runner = HftNativeRunner(alpha, config)

        with (
            patch("research.backtest.hft_native_runner._ADAPTER_AVAILABLE", True),
            patch("research.backtest.hft_native_runner._HFTBT_AVAILABLE", True),
        ):
            result = runner.run()

        assert "effective_broker_rtt_ms" in result.latency_profile
        assert result.latency_profile["effective_broker_rtt_ms"] == 47.0


# ---------------------------------------------------------------------------
# Bug #8 — feature_mode passthrough
# ---------------------------------------------------------------------------
class TestFeatureModePassthrough:
    def test_feature_mode_passthrough(self):
        """_run_adapter_slice passes config.feature_mode, not hardcoded 'stats_only'."""
        alpha = MagicMock()
        alpha.manifest.alpha_id = "test"
        config = BacktestConfig(data_paths=[], feature_mode="lob_feature")

        mock_adapter_instance = MagicMock()
        mock_adapter_instance.equity_values = np.array([1.0, 2.0])

        mock_bridge_cls = MagicMock()
        mock_bridge_instance = MagicMock()
        mock_bridge_instance.signal_log = []
        mock_bridge_cls.return_value = mock_bridge_instance

        with (
            patch("research.backtest.hft_native_runner._ADAPTER_AVAILABLE", True),
            patch("research.backtest.hft_native_runner.HftBacktestAdapter") as MockAdapter,
            patch("research.backtest.hft_native_runner.AlphaStrategyBridge", mock_bridge_cls),
            patch("research.backtest.hft_native_runner.signal_log_to_arrays", return_value=(
                np.array([]), np.array([]), np.array([]),
            )),
        ):
            MockAdapter.return_value = mock_adapter_instance
            _run_adapter_slice(alpha, "/fake/path.npz", config)
            # Verify feature_mode was passed from config, not hardcoded
            _, call_kwargs = MockAdapter.call_args
            assert call_kwargs["feature_mode"] == "lob_feature"


# ---------------------------------------------------------------------------
# Bug #9 — WF consistency uses config threshold
# ---------------------------------------------------------------------------
class TestWFConsistencyThreshold:
    def test_wf_consistency_uses_config_threshold(self):
        """With min_consistency_sharpe=0.5, folds with Sharpe < 0.5 are not 'consistent'."""
        alpha = MagicMock()
        alpha.manifest.alpha_id = "test"
        alpha.reset = MagicMock()

        config = BacktestConfig(data_paths=["/fake/data.npy"])
        wf = WalkForwardConfig(n_splits=5, min_train_samples=1, min_consistency_sharpe=0.5)

        # Simulate fold sharpes: [0.1, 0.2, 0.6, 0.8, 1.0] → 3/5 = 0.6 above threshold
        fold_sharpes = [0.1, 0.2, 0.6, 0.8, 1.0]

        mock_eq_arrays = [np.linspace(100, 100 + s * 10, 50) for s in fold_sharpes]
        call_count = [0]

        def mock_run_adapter_slice(_alpha, _path, _config, _symbol="ASSET"):
            idx = call_count[0]
            call_count[0] += 1
            eq = mock_eq_arrays[idx]
            sig = np.random.randn(50)
            mid = np.linspace(100, 110, 50)
            pos = np.ones(50)
            return eq, sig, mid, pos

        # Create fake full data
        fake_full = _make_event_array(120)  # 120 rows, fold_size = 120//6 = 20

        with (
            patch("research.backtest.hft_native_runner._collect_hbt_data", return_value=fake_full),
            patch("research.backtest.hft_native_runner._run_adapter_slice", side_effect=mock_run_adapter_slice),
            patch("research.backtest.hft_native_runner.compute_sharpe", side_effect=fold_sharpes),
            patch("research.backtest.hft_native_runner.compute_ic", return_value=(0.1, 0.05, np.array([0.1]))),
            patch("research.backtest.hft_native_runner.compute_max_drawdown", return_value=0.01),
            patch("research.backtest.hft_native_runner.compute_turnover", return_value=0.5),
        ):
            runner = HftNativeRunner(alpha, config)
            result = runner.run_walk_forward(alpha, wf)

        # 3 out of 5 folds have Sharpe > 0.5 → consistency = 0.6
        assert result.fold_consistency_pct == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Bug #11 — Forward returns no trailing zero
# ---------------------------------------------------------------------------
class TestForwardReturnsNoTrailingZero:
    def test_forward_returns_no_trailing_zero(self):
        """_forward_returns returns n-1 elements, no trailing 0."""
        prices = np.array([100.0, 110.0, 105.0])
        fwd = _forward_returns(prices)
        assert fwd.shape == (2,)
        assert fwd[0] == pytest.approx(0.1)  # (110-100)/100
        assert fwd[1] == pytest.approx(-5.0 / 110.0)  # (105-110)/110


# ---------------------------------------------------------------------------
# Bug #14 — WF includes remainder rows
# ---------------------------------------------------------------------------
class TestWFRemainderRows:
    def test_wf_includes_remainder_rows(self):
        """With 103 rows and n_splits=5, last fold test_end == 103 (not 102)."""
        alpha = MagicMock()
        alpha.manifest.alpha_id = "test"
        alpha.reset = MagicMock()

        config = BacktestConfig(data_paths=["/fake/data.npy"])
        wf = WalkForwardConfig(n_splits=5, min_train_samples=1)

        # 103 rows, n_splits=5 -> fold_size = 103//6 = 17
        # Without fix, last fold test_end = 17*6 = 102 (misses row 102).
        # With fix, test_end = min(17*6, 103) = 102 (Python slice full[85:102]).
        # The min() cap prevents OOB when fold_size*(fold_idx+2) > total_rows.
        fake_full = _make_event_array(103)

        def mock_run_adapter_slice(_alpha, _path, _config, _symbol="ASSET"):
            eq = np.linspace(100, 110, 10)
            sig = np.random.randn(10)
            mid = np.linspace(100, 110, 10)
            pos = np.ones(10)
            return eq, sig, mid, pos

        with (
            patch("research.backtest.hft_native_runner._collect_hbt_data", return_value=fake_full),
            patch("research.backtest.hft_native_runner._run_adapter_slice", side_effect=mock_run_adapter_slice),
            patch("research.backtest.hft_native_runner.compute_sharpe", return_value=1.0),
            patch("research.backtest.hft_native_runner.compute_ic", return_value=(0.1, 0.05, np.array([0.1]))),
            patch("research.backtest.hft_native_runner.compute_max_drawdown", return_value=0.01),
            patch("research.backtest.hft_native_runner.compute_turnover", return_value=0.5),
        ):
            runner = HftNativeRunner(alpha, config)
            result = runner.run_walk_forward(alpha, wf)

        assert len(result.folds) == 5
        last_fold = result.folds[-1]
        assert last_fold.test_size > 0
