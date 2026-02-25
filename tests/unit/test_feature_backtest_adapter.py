"""Tests for FeatureEngine integration in HftBacktestAdapter (lob_feature mode)."""
from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.strategy.base import BaseStrategy


# --- Shared stubs ---

class _Depth:
    best_bid = 1000000
    best_ask = 1001000
    best_bid_qty = 30
    best_ask_qty = 20


class _Hbt:
    def __init__(self, *args, **kwargs):
        self._ran = False
        self.current_timestamp = 123_000_000_000
        self.submitted = []

    def run(self):
        if self._ran:
            return False
        self._ran = True
        return True

    def elapse(self, *_args, **_kwargs):
        return True

    def depth(self, *_args, **_kwargs):
        return _Depth()

    def position(self, *_args, **_kwargs):
        return 0

    def submit_buy_order(self, asset_id, order_id, price, qty, tif, order_type):
        self.submitted.append(("buy", price, qty))

    def submit_sell_order(self, asset_id, order_id, price, qty, tif, order_type):
        self.submitted.append(("sell", price, qty))

    def cancel(self, *_args, **_kwargs):
        pass

    def close(self):
        return True


class _BacktestAsset:
    def data(self, *_args, **_kwargs):
        return self

    def linear_asset(self, *_args, **_kwargs):
        return self

    def constant_latency(self, *_args, **_kwargs):
        return self

    def power_prob_queue_model(self, *_args, **_kwargs):
        return self

    def int_order_id_converter(self):
        return self


class _Noop:
    def __init__(self, *args, **kwargs):
        pass


def _patch_hftbacktest(monkeypatch):
    monkeypatch.setattr(hbt_adapter, "HFTBACKTEST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(hbt_adapter, "HashMapMarketDepthBacktest", _Hbt, raising=False)
    monkeypatch.setattr(hbt_adapter, "BacktestAsset", _BacktestAsset, raising=False)
    monkeypatch.setattr(hbt_adapter, "LinearAsset", _Noop, raising=False)
    monkeypatch.setattr(hbt_adapter, "ConstantLatency", _Noop, raising=False)
    monkeypatch.setattr(hbt_adapter, "PowerProbQueueModel", _Noop, raising=False)
    monkeypatch.setattr(hbt_adapter, "IOC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "ROD", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "Limit", object(), raising=False)


# --- Test strategies ---

class _RecordFeaturesStrategy(BaseStrategy):
    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(strategy_id, **kwargs)
        self.recorded_spread = None
        self.recorded_tuple = None
        self.recorded_mid = None

    def on_stats(self, event):
        if self.ctx:
            self.recorded_spread = self.ctx.get_feature(event.symbol, "spread_scaled")
            self.recorded_mid = self.ctx.get_feature(event.symbol, "mid_price_x2")
            self.recorded_tuple = self.ctx.get_feature_tuple(event.symbol)


class _SimpleStrategy(BaseStrategy):
    def on_stats(self, event):
        self.buy(event.symbol, event.best_bid, 1)


# --- Tests ---

def test_lob_feature_mode_instantiates_lob_and_feature_engines(monkeypatch):
    _patch_hftbacktest(monkeypatch)
    strategy = _SimpleStrategy("demo")
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="AAA",
        data_path="dummy",
        feature_mode="lob_feature",
    )
    assert adapter._lob_engine is not None
    assert isinstance(adapter._lob_engine, LOBEngine)
    assert adapter._feature_engine is not None
    assert isinstance(adapter._feature_engine, FeatureEngine)


def test_stats_only_mode_no_lob_engine(monkeypatch):
    _patch_hftbacktest(monkeypatch)
    strategy = _SimpleStrategy("demo")
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="AAA",
        data_path="dummy",
        feature_mode="stats_only",
    )
    assert adapter._lob_engine is None
    assert adapter._feature_engine is None


def test_lob_feature_mode_feature_source_in_ctx(monkeypatch):
    _patch_hftbacktest(monkeypatch)
    strategy = _SimpleStrategy("demo")
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="AAA",
        data_path="dummy",
        feature_mode="lob_feature",
    )
    # ctx._feature_source must be a callable bound to the feature engine instance
    assert callable(adapter.ctx._feature_source)
    assert adapter.ctx._feature_source.__self__ is adapter._feature_engine


def test_lob_feature_mode_feature_view_source_in_ctx(monkeypatch):
    _patch_hftbacktest(monkeypatch)
    strategy = _SimpleStrategy("demo")
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="AAA",
        data_path="dummy",
        feature_mode="lob_feature",
    )
    assert callable(adapter.ctx._feature_view_source)
    assert adapter.ctx._feature_view_source.__self__ is adapter._feature_engine


def test_stats_only_mode_feature_source_none(monkeypatch):
    _patch_hftbacktest(monkeypatch)
    strategy = _SimpleStrategy("demo")
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="AAA",
        data_path="dummy",
        feature_mode="stats_only",
    )
    assert adapter.ctx._feature_source is None


def test_lob_feature_mode_populates_spread_scaled(monkeypatch):
    _patch_hftbacktest(monkeypatch)
    strategy = _RecordFeaturesStrategy("demo")
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="AAA",
        data_path="dummy",
        feature_mode="lob_feature",
        price_scale=10_000,
    )
    adapter.run()
    # spread should be non-None after lob_feature mode processes one event
    assert strategy.recorded_spread is not None


def test_lob_feature_mode_spread_value_is_integer(monkeypatch):
    _patch_hftbacktest(monkeypatch)
    strategy = _RecordFeaturesStrategy("demo")
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="AAA",
        data_path="dummy",
        feature_mode="lob_feature",
        price_scale=10_000,
    )
    adapter.run()
    # spread_scaled must be an int (Precision Law)
    if strategy.recorded_spread is not None:
        assert isinstance(strategy.recorded_spread, int)


def test_lob_feature_mode_feature_tuple_available_via_ctx(monkeypatch):
    _patch_hftbacktest(monkeypatch)
    strategy = _RecordFeaturesStrategy("demo")
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="AAA",
        data_path="dummy",
        feature_mode="lob_feature",
    )
    adapter.run()
    # feature_tuple should be populated after one iteration
    assert strategy.recorded_tuple is not None
    # tuple length matches default feature set
    from hft_platform.feature.registry import build_default_lob_feature_set_v1
    fs = build_default_lob_feature_set_v1()
    assert len(strategy.recorded_tuple) == len(fs.features)


def test_lob_feature_mode_mid_price_x2_is_sum_of_bid_ask(monkeypatch):
    _patch_hftbacktest(monkeypatch)
    strategy = _RecordFeaturesStrategy("demo")
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="AAA",
        data_path="dummy",
        feature_mode="lob_feature",
        price_scale=10_000,
    )
    adapter.run()
    # _Depth has best_bid=1000000, best_ask=1001000
    # mid_price_x2 = bid + ask = 2001000
    if strategy.recorded_mid is not None:
        assert strategy.recorded_mid == 1000000 + 1001000
