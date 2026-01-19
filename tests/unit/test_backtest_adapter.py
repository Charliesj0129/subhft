import sys
import types
from unittest.mock import MagicMock

from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.strategy.base import BaseStrategy


class _Depth:
    best_bid = 10000
    best_ask = 10010


class _Hbt:
    def __init__(self, *args, **kwargs):
        self._ran = False
        self.current_timestamp = 123
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
        self.submitted.append(("buy", asset_id, order_id, price, qty, tif, order_type))

    def submit_sell_order(self, asset_id, order_id, price, qty, tif, order_type):
        self.submitted.append(("sell", asset_id, order_id, price, qty, tif, order_type))

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


class _SimpleStrategy(BaseStrategy):
    def on_stats(self, event):
        self.buy(event.symbol, event.best_bid, 1)


def test_backtest_adapter_submits_scaled_order(monkeypatch):
    monkeypatch.setattr(hbt_adapter, "HFTBACKTEST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(hbt_adapter, "HashMapMarketDepthBacktest", _Hbt, raising=False)
    monkeypatch.setattr(hbt_adapter, "BacktestAsset", _BacktestAsset, raising=False)
    monkeypatch.setattr(hbt_adapter, "LinearAsset", _Noop, raising=False)
    monkeypatch.setattr(hbt_adapter, "ConstantLatency", _Noop, raising=False)
    monkeypatch.setattr(hbt_adapter, "PowerProbQueueModel", _Noop, raising=False)
    monkeypatch.setattr(hbt_adapter, "IOC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "ROD", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "Limit", object(), raising=False)

    strategy = _SimpleStrategy("demo")
    adapter = hbt_adapter.HftBacktestAdapter(strategy=strategy, asset_symbol="AAA", data_path="dummy", price_scale=100)

    result = adapter.run()
    assert result is True
    assert adapter.hbt.submitted
    kind, _asset_id, order_id, price, qty, _tif, _order_type = adapter.hbt.submitted[0]
    assert kind == "buy"
    assert order_id == 1
    assert price == 100.0
    assert qty == 1


def test_strategy_hbt_adapter_uses_strategy_class(monkeypatch):
    dummy_mod = types.ModuleType("dummy_mod")

    class DummyStrategy(BaseStrategy):
        pass

    dummy_mod.DummyStrategy = DummyStrategy
    monkeypatch.setitem(sys.modules, "dummy_mod", dummy_mod)

    stub_adapter = MagicMock()
    stub_adapter.run.return_value = True
    monkeypatch.setattr(hbt_adapter, "HftBacktestAdapter", lambda *args, **kwargs: stub_adapter, raising=False)

    adapter = hbt_adapter.StrategyHbtAdapter(
        data_path="dummy",
        strategy_module="dummy_mod",
        strategy_class="DummyStrategy",
        strategy_id="demo",
        symbol="AAA",
    )

    assert adapter.run() is True
    assert adapter.adapter is stub_adapter
