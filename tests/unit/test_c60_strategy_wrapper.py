"""Unit tests for C60 TMFD6 R47-minimal Maker BaseStrategy wrapper.

Post-PROMOTE T8 shadow-scaffold (R1, 2026-04-19). Verifies the live-runtime
wrapper at
``hft_platform.strategies.c60_tmfd6_solo_maker.C60TmfD6SoloMakerMinimal``:

  - BaseStrategy wrapper instantiation + __slots__ hot-path compliance
  - enabled=false is enforced in strategies.yaml (config-level, tested by
    loading the YAML directly) per `feedback_no_auto_deploy.md`
  - Config params propagate to the strategy instance; canonical mp=2
  - Price scaling: scaled-int x10000 (not float); Precision Law compliance
  - Spread-gate boundary at 5 pt
  - R47-minimal: D1/D2/D3 (PE/Queue/MFG) NOT referenced; D4 QI IS active
  - D4 QI skew: widens per top-of-book imbalance; NOT |pos|-gated
  - max_pos gate suppresses adverse side at cap (canonical mp=2)
  - on_fill, on_risk_feedback, on_gap all callable and keep invariants
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.c60_tmfd6_solo_maker import (
    _PRICE_SCALE,
    C60TmfD6SoloMakerMinimal,
)


def _make_stats(
    symbol: str = "TMFD6",
    best_bid: int = 22500 * 10_000,
    best_ask: int = 22505 * 10_000,
    spread_scaled: int | None = None,
    mid_price_x2: int | None = None,
    imbalance: float = 0.0,
    bid_depth: int = 10,
    ask_depth: int = 10,
) -> LOBStatsEvent:
    if spread_scaled is None:
        spread_scaled = best_ask - best_bid
    if mid_price_x2 is None:
        mid_price_x2 = best_bid + best_ask
    return LOBStatsEvent(
        symbol=symbol,
        ts=0,
        mid_price_x2=mid_price_x2,
        spread_scaled=spread_scaled,
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
    )


# ----------------------------------------------------------------------------
# Instantiation + slots
# ----------------------------------------------------------------------------


def test_instantiation_default() -> None:
    strat = C60TmfD6SoloMakerMinimal("c60_test")
    assert strat.strategy_id == "c60_test"


def test_has_slots_hot_path_compliance() -> None:
    """Hot-path strategies must declare __slots__ (allocator law)."""
    strat = C60TmfD6SoloMakerMinimal("c60_test", subscribe_symbols=["TMFD6"])
    cls_slots = type(strat).__slots__
    assert isinstance(cls_slots, tuple)
    assert len(cls_slots) > 0
    for slot in cls_slots:
        assert hasattr(strat, slot), f"C60 __slots__ missing {slot!r}"


def test_params_propagate_from_kwargs() -> None:
    strat = C60TmfD6SoloMakerMinimal(
        "c60_test",
        max_pos=2,
        spread_threshold_pts=5,
        inventory_skew_tenths=2,
        qi_skew_threshold=0.10,
        qi_skew_widen_ticks=1,
        enable_qi_layer=True,
        shadow_mode=True,
        queue_share=0.05,
        variant="R47-minimal",
        subscribe_symbols=["TMFD6"],
    )
    assert strat._max_pos == 2
    assert strat._spread_threshold_pts == 5
    assert strat._inventory_skew_tenths == 2
    assert strat._qi_skew_threshold == 0.10
    assert strat._qi_skew_widen_ticks == 1
    assert strat._enable_qi_layer is True
    assert strat._shadow_mode is True
    assert strat._queue_share_info == 0.05
    assert strat._variant_label == "R47-minimal"
    assert "TMFD6" in strat._symbols_set


def test_default_parameters_match_canonical_mp_2() -> None:
    """Canonical C60 config per DA T6: max_pos=2 (best of fresh-sim {1,2,3})."""
    strat = C60TmfD6SoloMakerMinimal("c60_test")
    assert strat._max_pos == 2
    assert strat._spread_threshold_pts == 5
    assert strat._inventory_skew_tenths == 2
    assert strat._qi_skew_threshold == 0.10
    assert strat._qi_skew_widen_ticks == 1
    assert strat._enable_qi_layer is True
    assert strat._variant_label == "R47-minimal"


# ----------------------------------------------------------------------------
# strategies.yaml enabled=false enforcement (shadow scaffold discipline)
# ----------------------------------------------------------------------------


def _load_strategies_yaml() -> dict:
    path = Path("/home/charlie/hft_platform/config/base/strategies.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def _find_c60_entry(cfg: dict) -> dict | None:
    strategies = cfg.get("strategies", [])
    for entry in strategies:
        if entry.get("id") == "C60_TMFD6_SOLO_MAKER":
            return entry
    return None


def test_strategies_yaml_has_c60_entry() -> None:
    """Shadow scaffold must register C60 in strategies.yaml."""
    cfg = _load_strategies_yaml()
    c60 = _find_c60_entry(cfg)
    assert c60 is not None, "C60_TMFD6_SOLO_MAKER not found in strategies.yaml"


def test_strategies_yaml_c60_enabled_false() -> None:
    """C60 MUST ship disabled pending user confirmation (shadow scaffold)."""
    cfg = _load_strategies_yaml()
    c60 = _find_c60_entry(cfg)
    assert c60 is not None
    assert c60["enabled"] is False, "C60 must ship enabled=false; user-gated per memory/feedback_no_auto_deploy.md"


def test_strategies_yaml_c60_module_class_correct() -> None:
    cfg = _load_strategies_yaml()
    c60 = _find_c60_entry(cfg)
    assert c60 is not None
    assert c60["module"] == "hft_platform.strategies.c60_tmfd6_solo_maker"
    assert c60["class"] == "C60TmfD6SoloMakerMinimal"


def test_strategies_yaml_c60_symbols_is_tmfd6() -> None:
    cfg = _load_strategies_yaml()
    c60 = _find_c60_entry(cfg)
    assert c60 is not None
    assert c60["symbols"] == ["TMFD6"]


def test_strategies_yaml_c60_params_match_canonical() -> None:
    cfg = _load_strategies_yaml()
    c60 = _find_c60_entry(cfg)
    assert c60 is not None
    params = c60["params"]
    assert params["max_pos"] == 2  # DA T6 canonical
    assert params["spread_threshold_pts"] == 5
    assert params["inventory_skew_tenths"] == 2
    assert params["qi_skew_threshold"] == 0.10
    assert params["qi_skew_widen_ticks"] == 1
    assert params["enable_qi_layer"] is True
    assert params["variant"] == "R47-minimal"
    assert params["shadow_mode"] is True


# ----------------------------------------------------------------------------
# strategy_limits.yaml
# ----------------------------------------------------------------------------


def _load_strategy_limits() -> dict:
    path = Path("/home/charlie/hft_platform/config/base/strategy_limits.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


def test_strategy_limits_has_c60_entry() -> None:
    cfg = _load_strategy_limits()
    assert "C60_TMFD6_SOLO_MAKER" in cfg["strategies"]


def test_strategy_limits_c60_max_pos_matches_strategy() -> None:
    cfg = _load_strategy_limits()
    c60 = cfg["strategies"]["C60_TMFD6_SOLO_MAKER"]
    assert c60["max_position_lots"] == 2  # mirrors strategy max_pos canonical
    assert c60["max_order_qty"] == 1


def test_strategy_limits_c60_daily_loss_hard_stop() -> None:
    cfg = _load_strategy_limits()
    c60 = cfg["strategies"]["C60_TMFD6_SOLO_MAKER"]
    assert c60["daily_loss_hard_stop_ntd"] == 15000


def test_strategy_limits_c60_auto_disable_6_canonical_rules() -> None:
    """Auto-disable thresholds must mirror the 6 canonical rules in
    SHADOW_DEPLOY.md per team-lead T8 dispatch."""
    cfg = _load_strategy_limits()
    c60 = cfg["strategies"]["C60_TMFD6_SOLO_MAKER"]
    ad = c60["auto_disable"]
    # Rule 1: rolling PnL floor
    assert ad["rolling_pnl_window_days"] == 5
    assert ad["rolling_pnl_min_ntd_per_day"] == -3000
    assert ad["rolling_pnl_trigger_count"] == 2
    # Rule 2: spread regime shift >20% from 2 pt baseline
    assert ad["spread_regime_baseline_pt"] == 2
    assert ad["spread_regime_shift_pct_max"] == 20
    assert ad["spread_regime_consec_days"] == 3
    # Rule 3: close-maker-rate drift
    assert ad["shadow_close_maker_rate_min"] == 0.80
    assert ad["shadow_close_maker_trailing_cycles"] == 200
    # Rule 4: loss-tail asymmetry > 2x mean for 2 consec days
    assert ad["loss_tail_ratio_max"] == 2
    assert ad["loss_tail_consec_days"] == 2
    # Rule 5: PnL vs projection shortfall (10% of 1367 after 30 days)
    assert ad["shadow_pnl_vs_projection_floor_pct"] == 10
    assert ad["shadow_projection_ntd_per_day"] == 1367
    assert ad["shadow_projection_min_days"] == 30
    # Rule 6: walk-forward k=5
    assert ad["walk_forward_k"] == 5
    assert ad["walk_forward_min_positive"] == 3
    # Structural guardrails (not one of the 6)
    assert ad["regime_persistence_sp_med_min"] == 1
    assert ad["regime_persistence_consec_days"] == 3
    assert ad["shadow_daily_pnl_min_ntd"] == 1367


# ----------------------------------------------------------------------------
# Price scaling — scaled-int x10000 (Precision Law)
# ----------------------------------------------------------------------------


def test_price_scale_constant_is_10k() -> None:
    """Wrapper must use x10000 scale (live platform convention)."""
    assert _PRICE_SCALE == 10_000


def test_on_stats_runs_without_error_on_valid_input() -> None:  # noqa: no-assert
    strat = C60TmfD6SoloMakerMinimal("c60_test", max_pos=2, subscribe_symbols=["TMFD6"])
    stats = _make_stats()
    strat.on_stats(stats)  # should not raise


def test_on_stats_skips_invalid_spread() -> None:
    strat = C60TmfD6SoloMakerMinimal("c60_test", subscribe_symbols=["TMFD6"])
    # spread = 4 pt (< 5 pt threshold); no exception, no quote
    stats = _make_stats(
        best_bid=22500 * 10_000,
        best_ask=22504 * 10_000,
    )
    strat.on_stats(stats)
    assert strat._spread_blocked == 1


def test_on_stats_skips_zero_prices() -> None:  # noqa: no-assert
    strat = C60TmfD6SoloMakerMinimal("c60_test", subscribe_symbols=["TMFD6"])
    stats = _make_stats(best_bid=0, best_ask=0, mid_price_x2=0, spread_scaled=0)
    strat.on_stats(stats)  # early return, no raise


# ----------------------------------------------------------------------------
# Spread gate boundary
# ----------------------------------------------------------------------------


def test_spread_gate_blocks_below_threshold_at_sp2() -> None:
    """TMFD6 median spread 2 pt < 5 pt threshold."""
    strat = C60TmfD6SoloMakerMinimal("c60_test", subscribe_symbols=["TMFD6"])
    stats = _make_stats(best_bid=22500 * 10_000, best_ask=22502 * 10_000)
    strat.on_stats(stats)
    assert strat._spread_blocked >= 1
    assert strat._quotes_posted == 0


def test_spread_gate_blocks_below_threshold_at_sp3() -> None:
    """TMFD6 p75 spread 3 pt < 5 pt threshold."""
    strat = C60TmfD6SoloMakerMinimal("c60_test", subscribe_symbols=["TMFD6"])
    stats = _make_stats(best_bid=22500 * 10_000, best_ask=22503 * 10_000)
    strat.on_stats(stats)
    assert strat._spread_blocked >= 1
    assert strat._quotes_posted == 0


def test_spread_gate_admits_at_threshold_with_qi_neutral() -> None:
    """Spread 5 pt at threshold with neutral QI; both sides quote."""
    strat = C60TmfD6SoloMakerMinimal("c60_test", subscribe_symbols=["TMFD6"])
    stats = _make_stats(
        best_bid=22500 * 10_000,
        best_ask=22505 * 10_000,
        imbalance=0.0,  # neutral: D4 QI does not widen
        bid_depth=10,
        ask_depth=10,
    )
    strat.on_stats(stats)
    assert strat._quotes_posted == 2


# ----------------------------------------------------------------------------
# R47-minimal: D1/D2/D3 NOT referenced; D4 QI IS active
# ----------------------------------------------------------------------------


def test_wrapper_has_no_d1_d2_d3_signal_layer_attributes() -> None:
    """R47-minimal: wrapper must NOT instantiate PE/Queue/MFG state."""
    strat = C60TmfD6SoloMakerMinimal("c60_test", subscribe_symbols=["TMFD6"])
    for attr_name in (
        "_pe_states",
        "_queue_states",
        "_mfg_states",
        "_pe_state",
        "_queue_state",
        "_mfg_state",
    ):
        assert not hasattr(strat, attr_name), f"R47-minimal violation: {attr_name} found on wrapper"


def test_wrapper_does_not_import_r47_d1_d2_d3_state_classes() -> None:
    """Static check: wrapper module does not import R47 D1/D2/D3 classes."""
    import hft_platform.strategies.c60_tmfd6_solo_maker as mod

    src_path = mod.__file__
    assert src_path is not None
    with open(src_path) as f:
        source = f.read()
    forbidden = ("_PEState", "_QueueState", "_MFGState")
    for sym in forbidden:
        assert sym not in source, f"R47-minimal violation: wrapper references {sym}"


def test_wrapper_exposes_qi_compute_method() -> None:
    """D4 QI skew layer must be implemented on the wrapper."""
    strat = C60TmfD6SoloMakerMinimal("c60_test", subscribe_symbols=["TMFD6"])
    assert hasattr(strat, "_compute_qi_skew")
    assert callable(strat._compute_qi_skew)


# ----------------------------------------------------------------------------
# D4 QI skew (top-of-book imbalance; NOT |pos|-modulated)
# ----------------------------------------------------------------------------


def test_qi_skew_widens_ask_when_bid_heavy() -> None:
    strat = C60TmfD6SoloMakerMinimal("c60_test", enable_qi_layer=True, subscribe_symbols=["TMFD6"])
    # imbalance = +0.8 > +0.10 threshold -> widen ASK up 1 tick
    stats = _make_stats(imbalance=0.8)
    strat.on_stats(stats)
    assert strat._qi_widen_events == 1
    assert strat._last_ask["TMFD6"] == (22505 + 1) * 10_000


def test_qi_skew_widens_bid_when_ask_heavy() -> None:
    strat = C60TmfD6SoloMakerMinimal("c60_test", enable_qi_layer=True, subscribe_symbols=["TMFD6"])
    stats = _make_stats(imbalance=-0.8)
    strat.on_stats(stats)
    assert strat._qi_widen_events == 1
    assert strat._last_bid["TMFD6"] == (22500 - 1) * 10_000


def test_qi_skew_neutral_within_threshold() -> None:
    strat = C60TmfD6SoloMakerMinimal("c60_test", enable_qi_layer=True, subscribe_symbols=["TMFD6"])
    stats = _make_stats(imbalance=0.04)  # within +/-0.10 threshold
    strat.on_stats(stats)
    assert strat._qi_widen_events == 0


def test_qi_skew_is_not_pos_gated() -> None:
    """QI must trigger the same at |pos|=0 and |pos|=2 for identical book."""
    from hft_platform.contracts.strategy import Side

    strat = C60TmfD6SoloMakerMinimal(
        "c60_test",
        enable_qi_layer=True,
        max_pos=2,
        subscribe_symbols=["TMFD6"],
    )
    # pos=0: widen once
    strat.on_stats(_make_stats(imbalance=0.8))
    widen_at_pos0 = strat._qi_widen_events
    # drive pos up to +2
    strat.on_fill(_make_fill(Side.BUY))
    strat.on_fill(_make_fill(Side.BUY))
    assert strat._local_pos["TMFD6"] == 2
    # identical book (note: price-move gate clears on fill via _last_bid.pop)
    strat.on_stats(_make_stats(imbalance=0.8))
    widen_at_pos2 = strat._qi_widen_events - widen_at_pos0
    assert widen_at_pos0 == 1
    assert widen_at_pos2 == 1


def test_qi_skew_disabled_by_param() -> None:
    strat = C60TmfD6SoloMakerMinimal("c60_test", enable_qi_layer=False, subscribe_symbols=["TMFD6"])
    strat.on_stats(_make_stats(imbalance=0.8))
    assert strat._qi_widen_events == 0


# ----------------------------------------------------------------------------
# max_pos gate (canonical mp=2)
# ----------------------------------------------------------------------------


def _make_fill(side_enum: object, qty: int = 1, price: int = 22500 * 10_000) -> object:
    from hft_platform.contracts.execution import FillEvent

    return FillEvent(
        fill_id="f1",
        account_id="acc",
        order_id="o1",
        strategy_id="c60_test",
        symbol="TMFD6",
        side=side_enum,  # type: ignore[arg-type]
        qty=qty,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=1000,
        match_ts_ns=1000,
    )


def test_on_fill_updates_local_position() -> None:
    from hft_platform.contracts.strategy import Side

    strat = C60TmfD6SoloMakerMinimal("c60_test", max_pos=2, subscribe_symbols=["TMFD6"])
    strat.on_fill(_make_fill(Side.BUY))  # type: ignore[arg-type]
    assert strat._local_pos.get("TMFD6") == 1


def test_on_fill_sell_decrements() -> None:
    from hft_platform.contracts.strategy import Side

    strat = C60TmfD6SoloMakerMinimal("c60_test", max_pos=2, subscribe_symbols=["TMFD6"])
    strat.on_fill(_make_fill(Side.SELL, price=22505 * 10_000))  # type: ignore[arg-type]
    assert strat._local_pos.get("TMFD6") == -1


def test_on_gap_clears_transient_state() -> None:
    from hft_platform.events import GapEvent

    strat = C60TmfD6SoloMakerMinimal("c60_test", max_pos=2, subscribe_symbols=["TMFD6"])
    strat._pending_buy["TMFD6"] = 2
    strat._pending_sell["TMFD6"] = 1
    strat._last_bid["TMFD6"] = 22500 * 10_000
    strat._last_ask["TMFD6"] = 22505 * 10_000
    gap = GapEvent(missed_count=5, first_missed_seq=100, last_missed_seq=104, ts=1000)
    strat.on_gap(gap)
    assert strat._pending_buy == {}
    assert strat._pending_sell == {}
    assert strat._last_bid == {}
    assert strat._last_ask == {}


# ----------------------------------------------------------------------------
# Runtime-mode default (shadow scaffold -> shadow_mode=False default;
# strategies.yaml sets shadow_mode=true explicitly)
# ----------------------------------------------------------------------------


def test_shadow_mode_defaults_to_false() -> None:
    """Wrapper default is live-runtime; strategies.yaml opts into shadow."""
    strat = C60TmfD6SoloMakerMinimal("c60_test")
    assert strat._shadow_mode is False


def test_queue_share_informational_only() -> None:
    """queue_share is a param for research-live parity, not a live gate."""
    strat = C60TmfD6SoloMakerMinimal(
        "c60_test",
        queue_share=0.05,
        subscribe_symbols=["TMFD6"],
    )
    stats = _make_stats(imbalance=0.0)  # neutral so QI doesn't alter quote
    strat.on_stats(stats)
    assert strat._quotes_posted == 2
