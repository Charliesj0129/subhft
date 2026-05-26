"""Integration parity: full HftBacktestAdapter feature path vs shared Python path.

Drives the *real* ``HftBacktestAdapter`` feed loop (feature_mode="lob_feature") over
a deterministic depth sequence using a stubbed hftbacktest backend, captures the
emitted ``FeatureUpdateEvent`` per feed, and asserts the promoted feature family
matches ``run_python_engine`` over the identical L1 book sequence.

This exercises ``_feed_loop.run_feed`` -> ``_hbt_utils.build_lob_event`` ->
``LOBEngine`` -> ``FeatureEngine.process_lob_update``, i.e. the genuine
hftbacktest feature seam — without needing the native hftbacktest library.
"""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.events import FeatureUpdateEvent
from hft_platform.feature.parity import (
    FrameResult,
    LobInputFrame,
    compare_paths,
    run_python_engine,
)
from hft_platform.feature.registry import default_feature_registry
from hft_platform.strategy.base import BaseStrategy

pytestmark = pytest.mark.integration

SYMBOL = "AAA"
PRICE_SCALE = 10_000


class _Depth:
    def __init__(self, best_bid: float, best_ask: float, bid_qty: int, ask_qty: int) -> None:
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.best_bid_qty = bid_qty
        self.best_ask_qty = ask_qty


class _SequenceHbt:
    """Stub hftbacktest backend that walks a fixed list of (depth, ts) frames."""

    def __init__(self, depths: list[_Depth], timestamps: list[int]) -> None:
        self._depths = depths
        self._timestamps = timestamps
        self._idx = -1

    def wait_next_feed(self, *_args, **_kwargs) -> int:
        if self._idx + 1 >= len(self._depths):
            return 1  # done
        self._idx += 1
        return 2  # feed available (modern)

    @property
    def current_timestamp(self) -> int:
        return self._timestamps[self._idx]

    def depth(self, *_args, **_kwargs) -> _Depth:
        return self._depths[self._idx]

    def position(self, *_args, **_kwargs) -> int:
        return 0

    def close(self) -> bool:
        return True


class _BacktestAsset:
    def data(self, *_a, **_k):
        return self

    def linear_asset(self, *_a, **_k):
        return self

    def constant_order_latency(self, *_a, **_k):
        return self

    def power_prob_queue_model(self, *_a, **_k):
        return self

    def int_order_id_converter(self):
        return self


class _Noop:
    def __init__(self, *args, **kwargs):
        pass


class _RecordingStrategy(BaseStrategy):
    """Capture every FeatureUpdateEvent the adapter dispatches."""

    def __init__(self, strategy_id: str, **kwargs):
        super().__init__(strategy_id, **kwargs)
        self.captured: list[FrameResult] = []

    def handle_event(self, ctx, event):  # noqa: ANN001
        if isinstance(event, FeatureUpdateEvent):
            self.captured.append(
                FrameResult(
                    symbol=event.symbol,
                    timestamp=int(event.ts),
                    feature_ids=tuple(event.feature_ids),
                    values=tuple(event.values),
                    warmup_ready_mask=int(event.warmup_ready_mask),
                )
            )
        return []


def _patch_hftbacktest(monkeypatch, hbt: _SequenceHbt) -> None:
    monkeypatch.setattr(hbt_adapter, "HFTBACKTEST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(hbt_adapter, "HashMapMarketDepthBacktest", lambda *a, **k: hbt, raising=False)
    monkeypatch.setattr(hbt_adapter, "BacktestAsset", _BacktestAsset, raising=False)
    monkeypatch.setattr(hbt_adapter, "LinearAsset", _Noop, raising=False)
    monkeypatch.setattr(hbt_adapter, "ConstantLatency", _Noop, raising=False)
    monkeypatch.setattr(hbt_adapter, "PowerProbQueueModel", _Noop, raising=False)
    monkeypatch.setattr(hbt_adapter, "IOC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "GTC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "LIMIT", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "_detect_wait_status_mode", lambda: "modern", raising=False)


def _build_sequence() -> tuple[list[_Depth], list[int], list[LobInputFrame]]:
    """Raw depth frames + the matching L1 LobInputFrames (identical scaled books)."""
    depths: list[_Depth] = []
    timestamps: list[int] = []
    frames: list[LobInputFrame] = []
    ts = 1_000
    for i in range(40):
        raw_bid = 100.0 + (i % 6) * 0.1
        raw_ask = raw_bid + 0.2
        bid_qty = 50 + i
        ask_qty = 40 + (i % 7)
        depths.append(_Depth(raw_bid, raw_ask, bid_qty, ask_qty))
        timestamps.append(ts)
        bb = int(round(raw_bid * PRICE_SCALE))
        ba = int(round(raw_ask * PRICE_SCALE))
        frames.append(
            LobInputFrame(
                SYMBOL,
                ts,
                np.array([[bb, bid_qty]], dtype=np.int64),
                np.array([[ba, ask_qty]], dtype=np.int64),
            )
        )
        ts += 125
    return depths, timestamps, frames


def test_full_adapter_feature_path_matches_shared_python(monkeypatch) -> None:
    if not getattr(hbt_adapter, "HFTBACKTEST_AVAILABLE", False):
        # Stub forces availability; this guard is defensive for import-time failures.
        pass

    depths, timestamps, frames = _build_sequence()
    hbt = _SequenceHbt(depths, timestamps)
    _patch_hftbacktest(monkeypatch, hbt)

    strategy = _RecordingStrategy("parity")
    adapter = hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol=SYMBOL,
        data="dummy",
        price_scale=PRICE_SCALE,
        feature_mode="lob_feature",
        dispatch_feature_events=True,
    )
    adapter.run()

    assert len(strategy.captured) == len(frames), "adapter did not emit one feature event per feed"

    feature_set = default_feature_registry().get_default()
    shared = run_python_engine(frames)

    report = compare_paths(
        {"python_shared": shared, "hftbt_full": strategy.captured},
        feature_set=feature_set,
    )
    report.raise_if_failed()
