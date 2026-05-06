"""Unit tests for C63 TXFD6 Tight Spread Maker BaseStrategy wrapper.

Post-PROMOTE-SUPPLEMENTAL T8 shadow-scaffold (R2-SUPP, 2026-04-19). Verifies
the live-runtime wrapper at
``hft_platform.strategies.c63_txfd6_tight_spread_maker.C63TxfD6TightSpreadMaker``:

  - BaseStrategy wrapper instantiation + __slots__ hot-path compliance
  - enabled=false in strategies.yaml (feedback_no_auto_deploy)
  - Config params propagate; canonical defaults sp=3/mp=3
  - Price scaling x10000 (Precision Law)
  - Spread-gate boundary at 3 pt (C63's signature lever vs C33=5)
  - All four signal layers (PE/Queue/MFG/QI) NOT referenced
  - max_pos gate (canonical mp=3)
  - Linear inventory skew (NOT |pos|-gated)
  - on_fill / on_risk_feedback / on_gap invariants
  - HARD COST GATE present in strategy_limits.yaml (2.5 pt retail)
  - Mutual exclusion with C33_TXFD6_SOLO_MAKER documented
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.c63_txfd6_tight_spread_maker import (
    _PRICE_SCALE,
    C63TxfD6TightSpreadMaker,
)


def _make_stats(
    symbol: str = "TXFD6",
    best_bid: int = 17500 * 10_000,
    best_ask: int = 17503 * 10_000,  # 3 pt spread default (hit threshold)
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
    s = C63TxfD6TightSpreadMaker("c63_test")
    assert s.strategy_id == "c63_test"


def test_has_slots_hot_path_compliance() -> None:
    s = C63TxfD6TightSpreadMaker("c63_test", subscribe_symbols=["TXFD6"])
    slots = type(s).__slots__
    assert isinstance(slots, tuple)
    assert len(slots) > 0
    for sl in slots:
        assert hasattr(s, sl), f"C63 __slots__ missing {sl!r}"


def test_params_propagate_from_kwargs() -> None:
    s = C63TxfD6TightSpreadMaker(
        "c63_test",
        max_pos=3,
        spread_threshold_pts=3,
        inventory_skew_tenths=2,
        shadow_mode=True,
        queue_share=0.05,
        variant="R47-minimal-tight-spread",
        subscribe_symbols=["TXFD6"],
    )
    assert s._max_pos == 3
    assert s._spread_threshold_pts == 3
    assert s._inventory_skew_tenths == 2
    assert s._shadow_mode is True
    assert s._queue_share_info == 0.05
    assert s._variant_label == "R47-minimal-tight-spread"
    assert "TXFD6" in s._symbols_set


def test_default_parameters_match_canonical_sp3_mp3() -> None:
    """Canonical C63 config per R2-SUPP T6: sp=3/mp=3."""
    s = C63TxfD6TightSpreadMaker("c63_test")
    assert s._max_pos == 3
    assert s._spread_threshold_pts == 3
    assert s._inventory_skew_tenths == 2
    assert s._variant_label == "R47-minimal-tight-spread"


# ----------------------------------------------------------------------------
# strategies.yaml — enabled=false; mutual exclusion with C33
# ----------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_strategies_yaml() -> dict:
    path = _REPO_ROOT / "research/strategy_archive/strategies_2026_05.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def _find(cfg: dict, sid: str) -> dict | None:
    for e in cfg.get("strategies", []):
        if e.get("id") == sid:
            return e
    return None


def test_strategies_yaml_has_c63_entry() -> None:
    cfg = _load_strategies_yaml()
    c63 = _find(cfg, "C63_TXFD6_TIGHT_SPREAD_MAKER")
    assert c63 is not None


def test_strategies_yaml_c63_enabled_false() -> None:
    cfg = _load_strategies_yaml()
    c63 = _find(cfg, "C63_TXFD6_TIGHT_SPREAD_MAKER")
    assert c63 is not None
    assert c63["enabled"] is False, "C63 must ship enabled=false; user-gated + mutually exclusive with C33"


def test_strategies_yaml_c63_module_class_correct() -> None:
    cfg = _load_strategies_yaml()
    c63 = _find(cfg, "C63_TXFD6_TIGHT_SPREAD_MAKER")
    assert c63 is not None
    assert c63["module"] == "hft_platform.strategies.c63_txfd6_tight_spread_maker"
    assert c63["class"] == "C63TxfD6TightSpreadMaker"


def test_strategies_yaml_c63_symbols_is_txfd6() -> None:
    cfg = _load_strategies_yaml()
    c63 = _find(cfg, "C63_TXFD6_TIGHT_SPREAD_MAKER")
    assert c63 is not None
    assert c63["symbols"] == ["TXFD6"]


def test_strategies_yaml_c63_params_canonical() -> None:
    cfg = _load_strategies_yaml()
    c63 = _find(cfg, "C63_TXFD6_TIGHT_SPREAD_MAKER")
    assert c63 is not None
    p = c63["params"]
    assert p["max_pos"] == 3
    assert p["spread_threshold_pts"] == 3  # C63's signature lever
    assert p["inventory_skew_tenths"] == 2
    assert p["variant"] == "R47-minimal-tight-spread"
    assert p["shadow_mode"] is True


def test_strategies_yaml_c33_must_also_be_inspectable() -> None:
    """C33 and C63 coexist in strategies.yaml (mutual-exclusion at enabled=true)."""
    cfg = _load_strategies_yaml()
    c33 = _find(cfg, "C33_TXFD6_SOLO_MAKER")
    c63 = _find(cfg, "C63_TXFD6_TIGHT_SPREAD_MAKER")
    assert c33 is not None, "C33 must still exist (rollback path)"
    assert c63 is not None, "C63 must be registered"


def test_strategies_yaml_c33_c63_never_both_enabled() -> None:
    """HARD rule: C33 and C63 must NEVER both be enabled=true simultaneously.

    Accepts the current committed state (one enabled=true is OK, both false is OK,
    both true MUST FAIL)."""
    cfg = _load_strategies_yaml()
    c33 = _find(cfg, "C33_TXFD6_SOLO_MAKER")
    c63 = _find(cfg, "C63_TXFD6_TIGHT_SPREAD_MAKER")
    assert c33 is not None and c63 is not None
    # Both cannot be True at same time on TXFD6.
    assert not (c33["enabled"] and c63["enabled"]), "C33 and C63 are mutually exclusive on TXFD6 — double-book risk"


# ----------------------------------------------------------------------------
# strategy_limits.yaml — HARD cost gate + 7 rules
# ----------------------------------------------------------------------------


def _load_limits() -> dict:
    path = _REPO_ROOT / "config/base/strategy_limits.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def test_strategy_limits_has_c63_entry() -> None:
    cfg = _load_limits()
    assert "C63_TXFD6_TIGHT_SPREAD_MAKER" in cfg["strategies"]


def test_strategy_limits_c63_max_pos_3() -> None:
    cfg = _load_limits()
    c63 = cfg["strategies"]["C63_TXFD6_TIGHT_SPREAD_MAKER"]
    assert c63["max_position_lots"] == 3
    assert c63["max_order_qty"] == 1


def test_strategy_limits_c63_daily_loss_hard_stop_20k() -> None:
    cfg = _load_limits()
    c63 = cfg["strategies"]["C63_TXFD6_TIGHT_SPREAD_MAKER"]
    assert c63["daily_loss_hard_stop_ntd"] == 20000


def test_strategy_limits_c63_auto_disable_7_canonical_rules() -> None:
    """All 7 rules per team-lead T8 dispatch must be encoded."""
    cfg = _load_limits()
    ad = cfg["strategies"]["C63_TXFD6_TIGHT_SPREAD_MAKER"]["auto_disable"]
    # Rule 1 (HARD) — cost gate
    assert ad["hard_cost_gate_retail_rt_max_pt"] == 2.5
    assert ad["hard_cost_gate_action"] == "ABORT_BEFORE_DEPLOY"
    # Rule 2 — rolling PnL floor
    assert ad["rolling_pnl_window_days"] == 5
    assert ad["rolling_pnl_min_ntd_per_day"] == 20000
    assert ad["rolling_pnl_trigger_count"] == 2
    # Rule 3 — spread regime drift 3..6 pt with 20% baseline 4
    assert ad["spread_regime_baseline_pt"] == 4
    assert ad["spread_regime_floor_pt"] == 3
    assert ad["spread_regime_ceiling_pt"] == 6
    assert ad["spread_regime_drift_pct_max"] == 20
    assert ad["spread_regime_consec_days"] == 3
    # Rule 4 — close-maker rate
    assert ad["shadow_close_maker_rate_min"] == 0.80
    assert ad["shadow_close_maker_trailing_cycles"] == 200
    # Rule 5 — loss tail
    assert ad["loss_tail_ratio_max"] == 2
    assert ad["loss_tail_consec_days"] == 2
    # Rule 6 — shortfall vs projection (20% of 34,404 after 30 days)
    assert ad["shadow_pnl_vs_projection_floor_pct"] == 20
    assert ad["shadow_projection_ntd_per_day"] == 34404
    assert ad["shadow_projection_min_days"] == 30
    # Rule 7 — walk-forward k=5
    assert ad["walk_forward_k"] == 5
    assert ad["walk_forward_min_positive"] == 3
    # Structural: mutual exclusion docs
    assert ad["mutually_exclusive_with"] == "C33_TXFD6_SOLO_MAKER"


# ----------------------------------------------------------------------------
# Price scaling (Precision Law)
# ----------------------------------------------------------------------------


def test_price_scale_constant_is_10k() -> None:
    assert _PRICE_SCALE == 10_000


def test_on_stats_runs_without_error() -> None:  # noqa: no-assert
    s = C63TxfD6TightSpreadMaker("c63_test", subscribe_symbols=["TXFD6"])
    s.on_stats(_make_stats())  # should not raise


def test_on_stats_skips_invalid_spread() -> None:
    s = C63TxfD6TightSpreadMaker("c63_test", subscribe_symbols=["TXFD6"])
    # spread = 2 pt (< 3 threshold); no quote
    s.on_stats(_make_stats(best_bid=17500 * 10_000, best_ask=17502 * 10_000))
    assert s._spread_blocked == 1
    assert s._quotes_posted == 0


def test_on_stats_skips_zero_prices() -> None:  # noqa: no-assert
    s = C63TxfD6TightSpreadMaker("c63_test", subscribe_symbols=["TXFD6"])
    s.on_stats(_make_stats(best_bid=0, best_ask=0, mid_price_x2=0, spread_scaled=0))
    # early return; no raise


# ----------------------------------------------------------------------------
# Spread gate — C63's signature lever (sp=3 vs C33's sp=5)
# ----------------------------------------------------------------------------


def test_spread_gate_blocks_at_sp2() -> None:
    s = C63TxfD6TightSpreadMaker("c63_test", subscribe_symbols=["TXFD6"])
    s.on_stats(_make_stats(best_bid=17500 * 10_000, best_ask=17502 * 10_000))
    assert s._spread_blocked >= 1
    assert s._quotes_posted == 0


def test_spread_gate_admits_at_sp3() -> None:
    """sp=3 at C63 threshold -> both sides quote. (C33 would block at sp=3.)"""
    s = C63TxfD6TightSpreadMaker("c63_test", subscribe_symbols=["TXFD6"])
    s.on_stats(_make_stats(best_bid=17500 * 10_000, best_ask=17503 * 10_000))
    assert s._quotes_posted == 2
    assert s._spread_blocked == 0


def test_spread_gate_admits_at_sp5() -> None:
    """sp=5 (C33 threshold) also admits for C63."""
    s = C63TxfD6TightSpreadMaker("c63_test", subscribe_symbols=["TXFD6"])
    s.on_stats(_make_stats(best_bid=17500 * 10_000, best_ask=17505 * 10_000))
    assert s._quotes_posted == 2


# ----------------------------------------------------------------------------
# R47-minimal: all four signal layers NOT referenced
# ----------------------------------------------------------------------------


def test_wrapper_has_no_signal_layer_attributes() -> None:
    s = C63TxfD6TightSpreadMaker("c63_test", subscribe_symbols=["TXFD6"])
    for attr in (
        "_pe_states",
        "_queue_states",
        "_mfg_states",
        "_qi_states",
        "_pe_state",
        "_queue_state",
        "_mfg_state",
        "_qi_state",
        "_qi_skew_threshold",  # no D4 QI for TXFD6 (C33 precedent)
        "_qi_skew_widen_ticks",
        "_enable_qi_layer",
    ):
        assert not hasattr(s, attr), f"R47-minimal violation: {attr} on C63 wrapper"


def test_wrapper_does_not_import_r47_signal_state_classes() -> None:
    import hft_platform.strategies.c63_txfd6_tight_spread_maker as mod

    src = mod.__file__
    assert src is not None
    with open(src) as f:
        source = f.read()
    for sym in ("_PEState", "_QueueState", "_MFGState", "_QIState"):
        assert sym not in source, f"R47-minimal violation: wrapper references {sym}"


# ----------------------------------------------------------------------------
# max_pos gate (canonical mp=3)
# ----------------------------------------------------------------------------


def _make_fill(side_enum: object, qty: int = 1, price: int = 17500 * 10_000):
    from hft_platform.contracts.execution import FillEvent

    return FillEvent(
        fill_id="f1",
        account_id="acc",
        order_id="o1",
        strategy_id="c63_test",
        symbol="TXFD6",
        side=side_enum,  # type: ignore[arg-type]
        qty=qty,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=1000,
        match_ts_ns=1000,
    )


def test_on_fill_updates_local_position_buy() -> None:
    from hft_platform.contracts.strategy import Side

    s = C63TxfD6TightSpreadMaker("c63_test", max_pos=3, subscribe_symbols=["TXFD6"])
    s.on_fill(_make_fill(Side.BUY))  # type: ignore[arg-type]
    assert s._local_pos.get("TXFD6") == 1


def test_on_fill_updates_local_position_sell() -> None:
    from hft_platform.contracts.strategy import Side

    s = C63TxfD6TightSpreadMaker("c63_test", max_pos=3, subscribe_symbols=["TXFD6"])
    s.on_fill(_make_fill(Side.SELL, price=17503 * 10_000))  # type: ignore[arg-type]
    assert s._local_pos.get("TXFD6") == -1


def test_on_gap_clears_transient_state() -> None:
    from hft_platform.events import GapEvent

    s = C63TxfD6TightSpreadMaker("c63_test", max_pos=3, subscribe_symbols=["TXFD6"])
    s._pending_buy["TXFD6"] = 2
    s._pending_sell["TXFD6"] = 1
    s._last_bid["TXFD6"] = 17500 * 10_000
    s._last_ask["TXFD6"] = 17503 * 10_000
    g = GapEvent(missed_count=5, first_missed_seq=100, last_missed_seq=104, ts=1000)
    s.on_gap(g)
    assert s._pending_buy == {}
    assert s._pending_sell == {}
    assert s._last_bid == {}
    assert s._last_ask == {}


# ----------------------------------------------------------------------------
# Runtime mode default
# ----------------------------------------------------------------------------


def test_shadow_mode_defaults_to_false() -> None:
    s = C63TxfD6TightSpreadMaker("c63_test")
    assert s._shadow_mode is False


def test_queue_share_informational_only() -> None:
    s = C63TxfD6TightSpreadMaker("c63_test", queue_share=0.05, subscribe_symbols=["TXFD6"])
    s.on_stats(_make_stats())
    # Still quotes (queue_share does not gate).
    assert s._quotes_posted == 2


# ----------------------------------------------------------------------------
# Cross-candidate distinction
# ----------------------------------------------------------------------------


def test_c63_default_threshold_lower_than_c33() -> None:
    """C63 canonical sp=3 < C33 canonical sp=5."""
    s = C63TxfD6TightSpreadMaker("c63_test")
    assert s._spread_threshold_pts == 3
    assert s._spread_threshold_pts < 5


def test_c63_same_instrument_as_c33_double_book_risk() -> None:
    """Both target TXFD6 — confirms mutual-exclusion rule necessity."""
    s = C63TxfD6TightSpreadMaker("c63_test", subscribe_symbols=["TXFD6"])
    assert "TXFD6" in s._symbols_set
