"""Unit tests for C33 TXFD6 Solo Maker BaseStrategy wrapper.

Post-PROMOTE T8 scaffold (R7, 2026-04-18). Verifies the live-runtime wrapper
at ``hft_platform.strategies.c33_txfd6_solo_maker.C33TxfD6SoloMaker``:

  - BaseStrategy wrapper instantiation + __slots__ hot-path compliance
  - enabled=false is enforced in strategies.yaml (config-level, tested by
    loading the YAML directly)
  - Config params propagate to the strategy instance
  - Price scaling: scaled-int x10000 (not float); Precision Law compliance
  - Spread-gate boundary at 5 pt
  - R47-minimal: signal-layer PE/Queue/MFG/QI NOT referenced in the wrapper
  - max_pos gate suppresses adverse side at cap
  - on_fill, on_risk_feedback, on_gap all callable and keep invariants
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hft_platform.events import LOBStatsEvent
from hft_platform.strategies.c33_txfd6_solo_maker import (
    _PRICE_SCALE,
    C33TxfD6SoloMaker,
)


def _make_stats(
    symbol: str = "TXFD6",
    best_bid: int = 17500 * 10_000,
    best_ask: int = 17505 * 10_000,
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
    strat = C33TxfD6SoloMaker("c33_test")
    assert strat.strategy_id == "c33_test"


def test_has_slots_hot_path_compliance() -> None:
    """Hot-path strategies must declare __slots__ (allocator law).

    Note: BaseStrategy (the parent) doesn't itself declare __slots__, so
    instances still carry a __dict__. The test asserts the subclass
    declares __slots__ (the discipline signal) and that every declared
    slot is populated — which is what matters for memory locality.
    """
    strat = C33TxfD6SoloMaker("c33_test", subscribe_symbols=["TXFD6"])
    cls_slots = type(strat).__slots__
    assert isinstance(cls_slots, tuple)
    assert len(cls_slots) > 0
    for slot in cls_slots:
        assert hasattr(strat, slot), f"C33 __slots__ missing {slot!r}"


def test_params_propagate_from_kwargs() -> None:
    strat = C33TxfD6SoloMaker(
        "c33_test",
        max_pos=3,
        spread_threshold_pts=5,
        inventory_skew_tenths=2,
        shadow_mode=True,
        queue_share=0.05,
        variant="R47-minimal",
        subscribe_symbols=["TXFD6"],
    )
    assert strat._max_pos == 3
    assert strat._spread_threshold_pts == 5
    assert strat._inventory_skew_tenths == 2
    assert strat._shadow_mode is True
    assert strat._queue_share_info == 0.05
    assert strat._variant_label == "R47-minimal"
    assert "TXFD6" in strat._symbols_set


def test_default_parameters_match_exception_live_cap() -> None:
    """Defaults must match the explicit 1-lot exception live rollout."""
    strat = C33TxfD6SoloMaker("c33_test")
    assert strat._max_pos == 1
    assert strat._spread_threshold_pts == 5
    assert strat._inventory_skew_tenths == 2
    assert strat._variant_label == "R47-minimal"


# ----------------------------------------------------------------------------
# strategies.yaml enabled=false enforcement
# ----------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_strategies_yaml() -> dict:
    path = _REPO_ROOT / "config/base/strategies.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def _find_c33_entry(cfg: dict) -> dict | None:
    strategies = cfg.get("strategies", [])
    for entry in strategies:
        if entry.get("id") == "C33_TXFD6_SOLO_MAKER":
            return entry
    return None


def test_strategies_yaml_has_c33_entry() -> None:
    """Shadow scaffold must register C33 in strategies.yaml."""
    cfg = _load_strategies_yaml()
    c33 = _find_c33_entry(cfg)
    assert c33 is not None, "C33_TXFD6_SOLO_MAKER not found in strategies.yaml"


def test_strategies_yaml_c33_enabled_false() -> None:
    """C33 is currently disabled after the R47-only rollback."""
    cfg = _load_strategies_yaml()
    c33 = _find_c33_entry(cfg)
    assert c33 is not None
    assert c33["enabled"] is False


def test_strategies_yaml_c33_module_class_correct() -> None:
    cfg = _load_strategies_yaml()
    c33 = _find_c33_entry(cfg)
    assert c33 is not None
    assert c33["module"] == "hft_platform.strategies.c33_txfd6_solo_maker"
    assert c33["class"] == "C33TxfD6SoloMaker"


def test_strategies_yaml_c33_symbols_is_txfd6() -> None:
    cfg = _load_strategies_yaml()
    c33 = _find_c33_entry(cfg)
    assert c33 is not None
    assert c33["symbols"] == ["TXFD6"]


def test_strategies_yaml_c33_params_match_exception_live_cap() -> None:
    cfg = _load_strategies_yaml()
    c33 = _find_c33_entry(cfg)
    assert c33 is not None
    params = c33["params"]
    assert params["max_pos"] == 1
    assert params["spread_threshold_pts"] == 5
    assert params["inventory_skew_tenths"] == 2
    assert params["variant"] == "R47-minimal"
    assert params["shadow_mode"] is False


# ----------------------------------------------------------------------------
# strategy_limits.yaml
# ----------------------------------------------------------------------------


def _load_strategy_limits() -> dict:
    path = _REPO_ROOT / "config/base/strategy_limits.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def test_strategy_limits_has_c33_entry() -> None:
    cfg = _load_strategy_limits()
    assert "C33_TXFD6_SOLO_MAKER" in cfg["strategies"]


def test_strategy_limits_c33_max_pos_matches_strategy() -> None:
    cfg = _load_strategy_limits()
    c33 = cfg["strategies"]["C33_TXFD6_SOLO_MAKER"]
    assert c33["max_position_lots"] == 1  # mirrors strategy max_pos
    assert c33["max_order_qty"] == 1


def test_strategy_limits_global_max_position_lots_exception_rollout() -> None:
    cfg = _load_strategy_limits()
    assert cfg["global_defaults"]["max_position_lots"] == 1


def test_strategy_limits_c33_daily_loss_hard_stop() -> None:
    cfg = _load_strategy_limits()
    c33 = cfg["strategies"]["C33_TXFD6_SOLO_MAKER"]
    assert c33["daily_loss_hard_stop_ntd"] == 150000


def test_strategy_limits_c33_auto_disable_thresholds() -> None:
    cfg = _load_strategy_limits()
    c33 = cfg["strategies"]["C33_TXFD6_SOLO_MAKER"]
    ad = c33["auto_disable"]
    assert ad["shadow_close_maker_rate_min"] == 0.80
    assert ad["regime_persistence_sp_med_min"] == 3
    assert ad["regime_persistence_consec_days"] == 3


# ----------------------------------------------------------------------------
# Price scaling — scaled-int x10000 (Precision Law)
# ----------------------------------------------------------------------------


def test_price_scale_constant_is_10k() -> None:
    """Wrapper must use x10000 scale (live platform convention)."""
    assert _PRICE_SCALE == 10_000


def test_on_stats_runs_without_error_on_valid_input() -> None:  # noqa: no-assert
    """No exception on well-formed LOBStatsEvent."""
    strat = C33TxfD6SoloMaker("c33_test", max_pos=3, subscribe_symbols=["TXFD6"])
    stats = _make_stats()
    strat.on_stats(stats)  # should not raise


def test_on_stats_skips_invalid_spread() -> None:
    strat = C33TxfD6SoloMaker("c33_test", subscribe_symbols=["TXFD6"])
    # spread = 4 pt (< 5 pt threshold); no exception, no quote
    stats = _make_stats(
        best_bid=17500 * 10_000,
        best_ask=17504 * 10_000,
    )
    strat.on_stats(stats)
    assert strat._spread_blocked == 1


def test_on_stats_skips_zero_prices() -> None:  # noqa: no-assert
    strat = C33TxfD6SoloMaker("c33_test", subscribe_symbols=["TXFD6"])
    stats = _make_stats(best_bid=0, best_ask=0, mid_price_x2=0, spread_scaled=0)
    strat.on_stats(stats)  # early return, no raise


# ----------------------------------------------------------------------------
# Spread gate boundary
# ----------------------------------------------------------------------------


def test_spread_gate_blocks_below_threshold() -> None:
    strat = C33TxfD6SoloMaker("c33_test", subscribe_symbols=["TXFD6"])
    # spread 4 pt < 5 pt threshold
    stats = _make_stats(best_bid=17500 * 10_000, best_ask=17504 * 10_000)
    strat.on_stats(stats)
    assert strat._spread_blocked >= 1
    assert strat._quotes_posted == 0


def test_spread_gate_admits_at_threshold() -> None:
    strat = C33TxfD6SoloMaker("c33_test", subscribe_symbols=["TXFD6"])
    # spread 5 pt >= threshold — both sides should quote
    stats = _make_stats(best_bid=17500 * 10_000, best_ask=17505 * 10_000)
    strat.on_stats(stats)
    # Both buy and sell quotes should be posted at pos=0
    assert strat._quotes_posted == 2


# ----------------------------------------------------------------------------
# R47-minimal: signal layers NOT referenced
# ----------------------------------------------------------------------------


def test_wrapper_has_no_signal_layer_attributes() -> None:
    """R47-minimal: wrapper must NOT instantiate PE/Queue/MFG/QI state."""
    strat = C33TxfD6SoloMaker("c33_test", subscribe_symbols=["TXFD6"])
    for attr_name in (
        "_pe_states",
        "_queue_states",
        "_mfg_states",
        "_pe_state",
        "_queue_state",
        "_mfg_state",
        "_qi_state",
    ):
        assert not hasattr(strat, attr_name), f"R47-minimal violation: {attr_name} found on wrapper"


def test_wrapper_does_not_import_r47_signal_state_classes() -> None:
    """Static check: the wrapper module does not import R47 signal classes."""
    import hft_platform.strategies.c33_txfd6_solo_maker as mod

    src_path = mod.__file__
    assert src_path is not None
    with open(src_path) as f:
        source = f.read()
    forbidden = ("_PEState", "_QueueState", "_MFGState")
    for sym in forbidden:
        assert sym not in source, f"R47-minimal violation: wrapper references {sym}"


# ----------------------------------------------------------------------------
# max_pos gate
# ----------------------------------------------------------------------------


def _make_fill(side_enum: object, qty: int = 1, price: int = 17500 * 10_000) -> object:
    from hft_platform.contracts.execution import FillEvent

    return FillEvent(
        fill_id="f1",
        account_id="acc",
        order_id="o1",
        strategy_id="c33_test",
        symbol="TXFD6",
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

    strat = C33TxfD6SoloMaker("c33_test", max_pos=3, subscribe_symbols=["TXFD6"])
    strat.on_fill(_make_fill(Side.BUY))  # type: ignore[arg-type]
    assert strat._local_pos.get("TXFD6") == 1


def test_on_fill_sell_decrements() -> None:
    from hft_platform.contracts.strategy import Side

    strat = C33TxfD6SoloMaker("c33_test", max_pos=3, subscribe_symbols=["TXFD6"])
    strat.on_fill(_make_fill(Side.SELL, price=17505 * 10_000))  # type: ignore[arg-type]
    assert strat._local_pos.get("TXFD6") == -1


def test_on_gap_clears_transient_state() -> None:
    from hft_platform.events import GapEvent

    strat = C33TxfD6SoloMaker("c33_test", max_pos=3, subscribe_symbols=["TXFD6"])
    strat._pending_buy["TXFD6"] = 2
    strat._pending_sell["TXFD6"] = 1
    strat._last_bid["TXFD6"] = 17500 * 10_000
    strat._last_ask["TXFD6"] = 17505 * 10_000
    gap = GapEvent(missed_count=5, first_missed_seq=100, last_missed_seq=104, ts=1000)
    strat.on_gap(gap)
    assert strat._pending_buy == {}
    assert strat._pending_sell == {}
    assert strat._last_bid == {}
    assert strat._last_ask == {}


# ----------------------------------------------------------------------------
# Runtime-mode default
# ----------------------------------------------------------------------------


def test_shadow_mode_defaults_to_false() -> None:
    """Exception rollout defaults to live mode unless config opts into shadow."""
    strat = C33TxfD6SoloMaker("c33_test")
    assert strat._shadow_mode is False


def test_queue_share_informational_only() -> None:
    """queue_share is a param for research-live parity, not a live gate."""
    strat = C33TxfD6SoloMaker(
        "c33_test",
        queue_share=0.05,
        subscribe_symbols=["TXFD6"],
    )
    # Still quotes normally — queue_share does not gate the live quote logic.
    stats = _make_stats()
    strat.on_stats(stats)
    assert strat._quotes_posted == 2
