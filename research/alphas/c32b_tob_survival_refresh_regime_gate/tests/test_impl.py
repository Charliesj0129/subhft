"""Unit tests for C32b TOB-survival refresh regime gate.

Patterns per .agent/skills/hft-test-hft/SKILL.md:
  - Scaled-int price assertions (CK scale = 1_000_000; TMFD6 tick = 1 pt)
  - Monotonic time via time.monotonic_ns(), no wall-clock deps
  - Factory fixtures via helper functions
"""

from __future__ import annotations

import time

import pytest

from research.alphas.c32b_tob_survival_refresh_regime_gate.impl import (
    C32bAlpha,
    C32bModulator,
    C32bParams,
    LobRefreshEvent,
    _RollingTobSurvivalTracker,
)
from research.registry.schemas import AlphaProtocol

_CK_SCALE = 1_000_000
_PLATFORM_SCALE = 10_000




def _evt(
    ts_ns: int,
    bid_pts: float,
    ask_pts: float,
    tob_changed: bool = True,
    scale: int = _CK_SCALE,
) -> LobRefreshEvent:
    return LobRefreshEvent(
        exch_ts_ns=ts_ns,
        bid_price=int(bid_pts * scale),
        ask_price=int(ask_pts * scale),
        scale=scale,
        tob_changed=tob_changed,
    )


def _feed_regime_active(
    mod: C32bModulator, base_ts_ns: int = 1_000_000_000_000_000_000
) -> int:
    """Prime the tracker with 5 minutes of TOB events at 250 ms intervals (>200 ms).

    Returns the timestamp of the last event fed (call next event after this).
    """
    ts = base_ts_ns
    minute_ns = 60_000_000_000
    bid = 22_500.0
    ask = 22_505.0
    for _minute in range(5):
        for i in range(22):
            if i % 2 == 0:
                b, a = bid, ask
            else:
                b, a = bid + 1.0, ask + 1.0
            mod.should_delay_refresh(_evt(ts, b, a, tob_changed=True))
            ts += 250_000_000
        bid, ask = 22_500.0, 22_505.0
    ts = base_ts_ns + 5 * minute_ns + 100_000_000
    mod.should_delay_refresh(_evt(ts, bid, ask, tob_changed=True))
    return ts


def _feed_regime_inactive(
    mod: C32bModulator, base_ts_ns: int = 1_000_000_000_000_000_000
) -> int:
    """Prime the tracker with 5 minutes of fast TOB events (<200 ms intervals)."""
    ts = base_ts_ns
    minute_ns = 60_000_000_000
    for _minute in range(5):
        for i in range(40):
            if i % 2 == 0:
                b, a = 22_500.0, 22_505.0
            else:
                b, a = 22_501.0, 22_506.0
            mod.should_delay_refresh(_evt(ts, b, a, tob_changed=True))
            ts += 100_000_000
    ts = base_ts_ns + 5 * minute_ns + 100_000_000
    mod.should_delay_refresh(_evt(ts, 22_500.0, 22_505.0, tob_changed=True))
    return ts




def test_tracker_starts_at_zero() -> None:
    tracker = _RollingTobSurvivalTracker()
    assert tracker.roll_median_ms == 0.0


def test_tracker_ignores_non_tob_events() -> None:
    tracker = _RollingTobSurvivalTracker()
    evt = _evt(ts_ns=1000, bid_pts=22500, ask_pts=22505, tob_changed=False)
    tracker.feed(evt)
    assert tracker.roll_median_ms == 0.0


def test_tracker_needs_min_events_per_minute() -> None:
    """Minutes with fewer than min_events_per_minute samples are skipped."""
    tracker = _RollingTobSurvivalTracker(min_events_per_minute=20)
    base = 1_000_000_000_000_000_000
    for i in range(5):
        tracker.feed(_evt(base + i * 250_000_000, 22500, 22505))
    tracker.feed(_evt(base + 60_000_000_000 + 100_000_000, 22500, 22505))
    assert tracker.roll_median_ms == 0.0




def test_threshold_at_199ms_blocks_delay() -> None:
    """tob_roll5_med = 199 → regime NOT active → no delay."""
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 199.0
    evt = _evt(ts_ns=10_000_000_000, bid_pts=22500, ask_pts=22505, tob_changed=False)
    assert mod.should_delay_refresh(evt) is False


def test_threshold_at_200ms_blocks_delay() -> None:
    """Strict > comparison: 200 ms is NOT active (needs > 200)."""
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 200.0
    evt = _evt(ts_ns=10_000_000_000, bid_pts=22500, ask_pts=22505, tob_changed=False)
    assert mod.should_delay_refresh(evt) is False


def test_threshold_at_201ms_opens_delay() -> None:
    """tob_roll5_med = 201 → regime active → delay returned True."""
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 201.0
    evt = _evt(ts_ns=10_000_000_000, bid_pts=22500, ask_pts=22505, tob_changed=False)
    assert mod.should_delay_refresh(evt) is True




def test_mid_move_releases_hold() -> None:
    """Any |Δmid| > 0.5 tick releases the hold and returns False."""
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 250.0
    ts = 10_000_000_000
    first = _evt(ts, 22500.0, 22505.0, tob_changed=False)
    assert mod.should_delay_refresh(first) is True
    ts += 50_000_000
    moved = _evt(ts, 22501.0, 22506.0, tob_changed=False)
    assert mod.should_delay_refresh(moved) is False
    assert mod.hold_in_progress is False


def test_mid_move_at_exactly_half_tick_holds() -> None:
    """|Δmid| == 0.5 tick (not >) does NOT release (strict inequality)."""
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 250.0
    ts = 10_000_000_000
    first = _evt(ts, 22500.0, 22505.0, tob_changed=False)
    assert mod.should_delay_refresh(first) is True
    ts += 50_000_000
    half_tick = _evt(ts, 22500.0, 22506.0, tob_changed=False)
    assert mod.should_delay_refresh(half_tick) is True


def test_max_delay_hold_expiry_releases() -> None:
    """Hold > 250 ms releases on the next event regardless of mid-stability."""
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 250.0
    ts = 10_000_000_000
    first = _evt(ts, 22500.0, 22505.0, tob_changed=False)
    assert mod.should_delay_refresh(first) is True
    ts += 300_000_000
    later = _evt(ts, 22500.0, 22505.0, tob_changed=False)
    assert mod.should_delay_refresh(later) is False
    assert mod.hold_in_progress is False


def test_max_delay_hold_at_cap_still_holds() -> None:
    """Hold duration exactly == 250 ms is still within cap (strict > comparison)."""
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 250.0
    ts = 10_000_000_000
    first = _evt(ts, 22500.0, 22505.0, tob_changed=False)
    assert mod.should_delay_refresh(first) is True
    ts += 250_000_000
    at_cap = _evt(ts, 22500.0, 22505.0, tob_changed=False)
    assert mod.should_delay_refresh(at_cap) is True


def test_regime_flip_during_hold_releases() -> None:
    """Regime turning inactive while hold in progress releases."""
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 250.0
    ts = 10_000_000_000
    first = _evt(ts, 22500.0, 22505.0, tob_changed=False)
    assert mod.should_delay_refresh(first) is True
    mod._tracker._last_roll_median_ms = 150.0
    ts += 50_000_000
    after_flip = _evt(ts, 22500.0, 22505.0, tob_changed=False)
    assert mod.should_delay_refresh(after_flip) is False
    assert mod.hold_in_progress is False




def test_modulator_has_no_order_methods() -> None:
    """R5 discipline: modulator must NEVER issue place_order / cancel_order.

    Attribute-surface check: no such method exists on the modulator object.
    """
    mod = C32bModulator()
    assert not hasattr(mod, "place_order")
    assert not hasattr(mod, "cancel_order")
    assert not hasattr(mod, "submit")
    assert not hasattr(mod, "send")
    assert callable(mod.should_delay_refresh)


def test_hook_signature_only_returns_bool() -> None:
    """Return type for every path of should_delay_refresh is bool.

    Covers True path, False path, and degenerate-book path.
    """
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 250.0
    assert mod.should_delay_refresh(_evt(1000, 22500, 22505, False)) is True
    mod._tracker._last_roll_median_ms = 100.0
    assert mod.should_delay_refresh(_evt(2000, 22500, 22505, False)) is False
    mod._tracker._last_roll_median_ms = 250.0
    assert mod.should_delay_refresh(_evt(3000, 0, 22505, False)) is False
    assert mod.should_delay_refresh(_evt(4000, 22505, 22500, False)) is False




def test_tracker_primed_active_then_hook_holds() -> None:
    """Feeding real regime-active events primes the tracker > 200 ms and enables delays."""
    mod = C32bModulator()
    last_ts = _feed_regime_active(mod)
    assert mod.tracker.roll_median_ms > 200.0
    ts = last_ts + 1_000_000
    evt = _evt(ts, 22500.0, 22505.0, tob_changed=False)
    assert mod.should_delay_refresh(evt) is True


def test_tracker_primed_inactive_then_hook_releases() -> None:
    """Fast-TOB regime → roll_median < 200 ms → hook never holds."""
    mod = C32bModulator()
    last_ts = _feed_regime_inactive(mod)
    assert mod.tracker.roll_median_ms <= 200.0
    ts = last_ts + 1_000_000
    evt = _evt(ts, 22500.0, 22505.0, tob_changed=False)
    assert mod.should_delay_refresh(evt) is False




def test_monotonic_timestamp_ordering_preserved() -> None:
    mod = C32bModulator()
    t0 = time.monotonic_ns()
    mod.should_delay_refresh(_evt(t0, 22500, 22505, tob_changed=True))
    t1 = time.monotonic_ns()
    assert t1 > t0
    EPOCH_THRESHOLD_NS = 100_000_000_000_000_000
    assert t0 < EPOCH_THRESHOLD_NS
    assert t1 < EPOCH_THRESHOLD_NS




def test_on_gap_clears_hold() -> None:
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 250.0
    assert mod.should_delay_refresh(_evt(1000, 22500, 22505, False)) is True
    assert mod.hold_in_progress is True
    mod.on_gap()
    assert mod.hold_in_progress is False


def test_on_refresh_executed_clears_hold() -> None:
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 250.0
    assert mod.should_delay_refresh(_evt(1000, 22500, 22505, False)) is True
    assert mod.hold_in_progress is True
    mod.on_refresh_executed()
    assert mod.hold_in_progress is False




def test_c32b_alpha_conforms_to_alpha_protocol() -> None:
    alpha = C32bAlpha()
    assert isinstance(alpha, AlphaProtocol)
    assert alpha.manifest.alpha_id == "c32b_tob_survival_refresh_regime_gate_rescue"
    assert alpha.manifest.strategy_type == "maker"
    sig = alpha.update(foo=1)
    assert isinstance(sig, float)
    assert alpha.reset() is None


def test_c32b_manifest_declares_latency_profile() -> None:
    alpha = C32bAlpha()
    assert alpha.manifest.latency_profile is not None
    assert alpha.manifest.latency_profile != ""


def test_c32b_manifest_documents_r47_modulator_role() -> None:
    """Hypothesis must cite R47 modulator role."""
    alpha = C32bAlpha()
    h = alpha.manifest.hypothesis.upper()
    assert "R47" in h
    assert "REFRESH" in h or "QUEUE PRIORITY" in h


def test_c32b_manifest_claims_zero_incremental_cost() -> None:
    """Hypothesis + formula encode modulator / incremental-cost=0 claim."""
    alpha = C32bAlpha()
    h = alpha.manifest.hypothesis.upper()
    assert "INCREMENTAL" in h or "NO NEW ORDERS" in h or "RT COST = 0" in h




def test_default_threshold_is_200ms() -> None:
    """Threshold must default to 200 ms (FROZEN per DA)."""
    assert C32bParams().tob_median_threshold_ms == 200


def test_default_max_delay_hold_is_250ms() -> None:
    assert C32bParams().max_delay_hold_ms == 250




def test_delay_and_release_counters_advance() -> None:
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 250.0
    mod.should_delay_refresh(_evt(1000, 22500, 22505, False))
    mod.should_delay_refresh(_evt(1000 + 50_000_000, 22500, 22505, False))
    assert mod.delay_count == 2
    mod.should_delay_refresh(_evt(1000 + 100_000_000, 22502, 22507, False))
    assert mod.release_count == 1


@pytest.mark.parametrize(
    "scale",
    [_PLATFORM_SCALE, _CK_SCALE],
)
def test_half_tick_threshold_scales_with_input(scale: int) -> None:
    """The 0.5-tick mid-move threshold is derived from the event's scale,
    so the hook behaves identically at platform x10k and CK x1M."""
    mod = C32bModulator()
    mod._tracker._last_roll_median_ms = 250.0
    assert mod.should_delay_refresh(_evt(1000, 22500, 22505, False, scale=scale)) is True
    assert mod.should_delay_refresh(_evt(1500, 22500.5, 22505.7, False, scale=scale)) is False
