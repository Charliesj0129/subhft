"""Tests for feature staleness detection (last_update_ns + TTL).

Covers:
- FeatureEngine.last_update_ns returns None before any update
- FeatureEngine.last_update_ns returns a timestamp after process_lob_stats
- FeatureEngine.last_update_ns cleared on reset_symbol and reset_all
- StrategyContext.is_feature_stale returns True for never-updated symbol
- StrategyContext.is_feature_stale returns False for freshly-updated symbol
- StrategyContext.is_feature_stale returns True after time gap exceeds max_age_ns
- Staleness counter is incremented when stale detected
"""

import time
from unittest.mock import MagicMock

from hft_platform.events import LOBStatsEvent
from hft_platform.feature.engine import FeatureEngine
from hft_platform.strategy.base import StrategyContext


def _stats(symbol: str = "2330", ts: int = 1, bid: int = 1000000, ask: int = 1001000, bq: int = 10, aq: int = 20):
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.0,
        best_bid=bid,
        best_ask=ask,
        bid_depth=bq,
        ask_depth=aq,
    )


# --- FeatureEngine.last_update_ns tests ---


def test_last_update_ns_returns_none_before_any_update():
    eng = FeatureEngine()
    assert eng.last_update_ns("2330") is None


def test_last_update_ns_returns_timestamp_after_update():
    eng = FeatureEngine()
    before = time.time_ns()
    eng.process_lob_stats(_stats())
    after = time.time_ns()
    ts = eng.last_update_ns("2330")
    assert ts is not None
    assert before <= ts <= after


def test_last_update_ns_updated_on_each_call():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats(ts=1))
    ts1 = eng.last_update_ns("2330")
    assert ts1 is not None
    eng.process_lob_stats(_stats(ts=2))
    ts2 = eng.last_update_ns("2330")
    assert ts2 is not None
    assert ts2 >= ts1


def test_last_update_ns_per_symbol():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats(symbol="2330"))
    eng.process_lob_stats(_stats(symbol="2317"))
    assert eng.last_update_ns("2330") is not None
    assert eng.last_update_ns("2317") is not None
    assert eng.last_update_ns("9999") is None


def test_last_update_ns_cleared_on_reset_symbol():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats(symbol="2330"))
    assert eng.last_update_ns("2330") is not None
    eng.reset_symbol("2330")
    assert eng.last_update_ns("2330") is None


def test_last_update_ns_cleared_on_reset_all():
    eng = FeatureEngine()
    eng.process_lob_stats(_stats(symbol="2330"))
    eng.process_lob_stats(_stats(symbol="2317"))
    eng.reset_all()
    assert eng.last_update_ns("2330") is None
    assert eng.last_update_ns("2317") is None


# --- StrategyContext.is_feature_stale tests ---


def _make_ctx(*, staleness_source=None, staleness_counter=None):
    return StrategyContext(
        positions={},
        strategy_id="test",
        intent_factory=MagicMock(),
        price_scaler=MagicMock(),
        feature_staleness_source=staleness_source,
        staleness_counter=staleness_counter,
    )


def test_is_feature_stale_returns_true_when_no_source():
    ctx = _make_ctx()
    assert ctx.is_feature_stale("2330", max_age_ns=1_000_000_000) is True


def test_is_feature_stale_returns_true_when_never_updated():
    ctx = _make_ctx(staleness_source=lambda sym: None)
    assert ctx.is_feature_stale("2330", max_age_ns=1_000_000_000) is True


def test_is_feature_stale_returns_false_when_fresh():
    # Source returns current time -> age ~0 -> not stale
    ctx = _make_ctx(staleness_source=lambda sym: time.time_ns())
    assert ctx.is_feature_stale("2330", max_age_ns=1_000_000_000) is False


def test_is_feature_stale_returns_true_when_expired():
    # Source returns a timestamp 2 seconds in the past; max_age = 1 second
    old_ts = time.time_ns() - 2_000_000_000
    ctx = _make_ctx(staleness_source=lambda sym: old_ts)
    assert ctx.is_feature_stale("2330", max_age_ns=1_000_000_000) is True


def test_is_feature_stale_increments_counter_on_stale():
    counter = MagicMock()
    ctx = _make_ctx(staleness_source=lambda sym: None, staleness_counter=counter)
    ctx.is_feature_stale("2330", max_age_ns=1_000_000_000)
    counter.inc.assert_called_once()


def test_is_feature_stale_does_not_increment_counter_when_fresh():
    counter = MagicMock()
    ctx = _make_ctx(staleness_source=lambda sym: time.time_ns(), staleness_counter=counter)
    ctx.is_feature_stale("2330", max_age_ns=1_000_000_000)
    counter.inc.assert_not_called()


# --- Integration: FeatureEngine + StrategyContext wired together ---


def test_staleness_integration_with_feature_engine():
    eng = FeatureEngine()
    counter = MagicMock()

    ctx = _make_ctx(staleness_source=eng.last_update_ns, staleness_counter=counter)

    # Never updated -> stale
    assert ctx.is_feature_stale("2330", max_age_ns=1_000_000_000) is True
    assert counter.inc.call_count == 1

    # Update feature engine -> fresh
    eng.process_lob_stats(_stats())
    assert ctx.is_feature_stale("2330", max_age_ns=1_000_000_000) is False
    assert counter.inc.call_count == 1  # no new increment

    # With a very small TTL -> stale (time has passed since update)
    assert ctx.is_feature_stale("2330", max_age_ns=0) is True
    assert counter.inc.call_count == 2
