"""Unit tests for HftBacktestAdapter latency parameter handling.

Tests that modify_latency_us / cancel_latency_us are correctly folded into
the constant_order_latency call via max(place, modify, cancel), and that a
structlog warning is emitted when the latencies differ.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.strategy.base import BaseStrategy

# ---------------------------------------------------------------------------
# Minimal stubs for hftbacktest objects
# ---------------------------------------------------------------------------


class _BacktestAssetStub:
    """Records calls to constant_order_latency for assertions."""

    def __init__(self):
        self.constant_latency_calls: list[tuple] = []

    def data(self, *_a, **_kw):
        return self

    def linear_asset(self, *_a, **_kw):
        return self

    def constant_order_latency(self, entry_ns: int, resp_ns: int):
        self.constant_latency_calls.append((entry_ns, resp_ns))
        return self

    # queue / exchange / misc builder methods
    def power_prob_queue_model(self, *_a, **_kw):
        return self

    def partial_fill_exchange(self, *_a, **_kw):
        return self

    def no_partial_fill_exchange(self, *_a, **_kw):
        return self

    def int_order_id_converter(self):
        return self

    def tick_size(self, *_a, **_kw):
        return self

    def lot_size(self, *_a, **_kw):
        return self


class _HbtStub:
    current_timestamp = 0

    def wait_next_feed(self, *_a, **_kw):
        return 1  # immediately signals end-of-data

    def depth(self, *_a, **_kw):
        m = MagicMock()
        m.best_bid = 10000
        m.best_ask = 10010
        return m

    def position(self, *_a, **_kw):
        return 0

    def close(self):
        return True


class _NoopStrategy(BaseStrategy):
    def on_stats(self, event):
        pass


# ---------------------------------------------------------------------------
# Helper to patch hftbacktest references in the adapter module
# ---------------------------------------------------------------------------


def _make_asset_stub():
    return _BacktestAssetStub()


def _patch_hftbacktest(monkeypatch, asset_stub: _BacktestAssetStub):
    """Patch all hftbacktest symbols used by HftBacktestAdapter.__init__."""
    monkeypatch.setattr(hbt_adapter, "HFTBACKTEST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(hbt_adapter, "BacktestAsset", lambda: asset_stub, raising=False)
    monkeypatch.setattr(hbt_adapter, "HashMapMarketDepthBacktest", lambda _: _HbtStub(), raising=False)
    monkeypatch.setattr(hbt_adapter, "GTC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "IOC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "LIMIT", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "_detect_wait_status_mode", lambda: "modern", raising=False)


def _build_adapter(monkeypatch, *, latency_us=100, modify_latency_us=0, cancel_latency_us=0):
    asset_stub = _make_asset_stub()
    _patch_hftbacktest(monkeypatch, asset_stub)
    strategy = _NoopStrategy("test_strat")
    hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="SYM",
        data_path="dummy",
        latency_us=latency_us,
        modify_latency_us=modify_latency_us,
        cancel_latency_us=cancel_latency_us,
    )
    return asset_stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLatencyPassThrough:
    """When only latency_us is provided, constant_order_latency receives latency_us * 1000."""

    def test_only_place_latency_uses_place_latency(self, monkeypatch):
        asset_stub = _build_adapter(monkeypatch, latency_us=200)
        assert len(asset_stub.constant_latency_calls) == 1
        entry_ns, resp_ns = asset_stub.constant_latency_calls[0]
        assert entry_ns == 200 * 1000
        assert resp_ns == 200 * 1000

    def test_zero_modify_cancel_does_not_change_latency(self, monkeypatch):
        asset_stub = _build_adapter(monkeypatch, latency_us=150, modify_latency_us=0, cancel_latency_us=0)
        entry_ns, resp_ns = asset_stub.constant_latency_calls[0]
        assert entry_ns == 150 * 1000
        assert resp_ns == 150 * 1000


class TestMaxLatencyUsed:
    """When modify or cancel latencies are larger, the max is used."""

    def test_modify_latency_larger_than_place(self, monkeypatch):
        asset_stub = _build_adapter(monkeypatch, latency_us=100, modify_latency_us=300, cancel_latency_us=0)
        entry_ns, resp_ns = asset_stub.constant_latency_calls[0]
        assert entry_ns == 300 * 1000
        assert resp_ns == 300 * 1000

    def test_cancel_latency_larger_than_place(self, monkeypatch):
        asset_stub = _build_adapter(monkeypatch, latency_us=100, modify_latency_us=0, cancel_latency_us=250)
        entry_ns, resp_ns = asset_stub.constant_latency_calls[0]
        assert entry_ns == 250 * 1000
        assert resp_ns == 250 * 1000

    def test_max_of_all_three(self, monkeypatch):
        asset_stub = _build_adapter(monkeypatch, latency_us=100, modify_latency_us=400, cancel_latency_us=350)
        entry_ns, resp_ns = asset_stub.constant_latency_calls[0]
        assert entry_ns == 400 * 1000
        assert resp_ns == 400 * 1000

    def test_place_latency_already_largest(self, monkeypatch):
        """When place latency dominates, effective latency equals place latency."""
        asset_stub = _build_adapter(monkeypatch, latency_us=500, modify_latency_us=200, cancel_latency_us=150)
        entry_ns, resp_ns = asset_stub.constant_latency_calls[0]
        assert entry_ns == 500 * 1000
        assert resp_ns == 500 * 1000


class TestWarningEmitted:
    """A structlog warning must be emitted when latencies differ."""

    def test_warning_when_modify_exceeds_place(self, monkeypatch):
        asset_stub = _make_asset_stub()
        _patch_hftbacktest(monkeypatch, asset_stub)
        strategy = _NoopStrategy("warn_strat")

        with patch.object(hbt_adapter.logger, "warning") as mock_warn:
            hbt_adapter.HftBacktestAdapter(
                strategy=strategy,
                asset_symbol="SYM",
                data_path="dummy",
                latency_us=100,
                modify_latency_us=300,
                cancel_latency_us=0,
            )
        mock_warn.assert_called_once()
        event_name = mock_warn.call_args[0][0]
        assert event_name == "backtest_latency_approximation"
        kwargs = mock_warn.call_args[1]
        assert kwargs["effective_latency_us"] == 300
        assert kwargs["place_latency_us"] == 100

    def test_warning_when_cancel_exceeds_place(self, monkeypatch):
        asset_stub = _make_asset_stub()
        _patch_hftbacktest(monkeypatch, asset_stub)
        strategy = _NoopStrategy("warn_strat2")

        with patch.object(hbt_adapter.logger, "warning") as mock_warn:
            hbt_adapter.HftBacktestAdapter(
                strategy=strategy,
                asset_symbol="SYM",
                data_path="dummy",
                latency_us=100,
                modify_latency_us=0,
                cancel_latency_us=200,
            )
        mock_warn.assert_called_once()
        kwargs = mock_warn.call_args[1]
        assert kwargs["effective_latency_us"] == 200

    def test_no_warning_when_place_already_largest(self, monkeypatch):
        """No warning when place_latency dominates (approximation is exact)."""
        asset_stub = _make_asset_stub()
        _patch_hftbacktest(monkeypatch, asset_stub)
        strategy = _NoopStrategy("no_warn_strat")

        with patch.object(hbt_adapter.logger, "warning") as mock_warn:
            hbt_adapter.HftBacktestAdapter(
                strategy=strategy,
                asset_symbol="SYM",
                data_path="dummy",
                latency_us=500,
                modify_latency_us=200,
                cancel_latency_us=150,
            )
        mock_warn.assert_not_called()

    def test_no_warning_when_modify_cancel_both_zero(self, monkeypatch):
        """No warning when modify/cancel are 0 (feature not used)."""
        asset_stub = _make_asset_stub()
        _patch_hftbacktest(monkeypatch, asset_stub)
        strategy = _NoopStrategy("no_warn_strat2")

        with patch.object(hbt_adapter.logger, "warning") as mock_warn:
            hbt_adapter.HftBacktestAdapter(
                strategy=strategy,
                asset_symbol="SYM",
                data_path="dummy",
                latency_us=100,
                modify_latency_us=0,
                cancel_latency_us=0,
            )
        mock_warn.assert_not_called()

    def test_no_warning_when_all_latencies_equal(self, monkeypatch):
        """No warning when all three latencies are equal."""
        asset_stub = _make_asset_stub()
        _patch_hftbacktest(monkeypatch, asset_stub)
        strategy = _NoopStrategy("equal_lat_strat")

        with patch.object(hbt_adapter.logger, "warning") as mock_warn:
            hbt_adapter.HftBacktestAdapter(
                strategy=strategy,
                asset_symbol="SYM",
                data_path="dummy",
                latency_us=100,
                modify_latency_us=100,
                cancel_latency_us=100,
            )
        mock_warn.assert_not_called()
        assert len(asset_stub.constant_latency_calls) == 1
        assert asset_stub.constant_latency_calls[0] == (100 * 1000, 100 * 1000)
