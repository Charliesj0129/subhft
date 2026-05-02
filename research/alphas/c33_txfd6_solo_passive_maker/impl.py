"""C33 — TXFD6 solo passive maker (R47-minimal variant).

Applies the R47 MakerStrategy framework to TXFD6 with its native cost/spread
structure (RT=3pt retail, 200 NTD/pt). Signal layers (PE / Queue / MFG / QI)
are DISABLED by default per the R7 T1 counterfactual: R47-full-QI (calibrated
on TMFD6) underperforms R47-minimal 4:1 on TXFD6. TXFD6 requires its own
layer calibration; C33 ships R47-minimal only.

Interfaces:
  - ``TxfD6SoloMaker`` conforms to
    ``research.backtest.maker_engine.MakerStrategy``.
  - ``C33Alpha`` conforms to ``research.registry.schemas.AlphaProtocol``.

H2 framework note (DA T2 adjudication):
  Approved under MAKER FULL-CYCLE framework (Avellaneda-Stoikov §4,
  Cartea-Jaimungal-Penalva §3-4). Per-cycle gross = 2 × half_spread; the full
  cycle pays 1 × RT. At TXFD6 OOS this gives `2 × 1.71 - 3 = +0.42 pt/cycle`
  margin. The framework is CONDITIONAL on close-side fills being MAKER — if
  >=20% of cycles close via TAKER flatten, the framework collapses to strict
  single-fill (gross 1.71 pt < RT 3 pt = FAIL).

Research-module float exception (Rule 11 of 25-architecture-governance):
this file is offline / CLI-invoked research code. Prices arriving from CK
use scaled-integer convention (default scale 1e6). All strategy-decision
arithmetic is integer; only MTM reporting uses floats.

Precision Law (CLAUDE.md #4): scaled-integer math for all quote decisions.
Timestamps are monotonic ns supplied by the caller; the strategy never calls
``datetime.now()``.

Cost citation: `memory/feedback_taifex_fee_structure.md` (TXF RT=3pt,
user-confirmed 2026-04-18). C33 adds no cross-instrument cost (single leg).
"""

from __future__ import annotations

from dataclasses import dataclass

from structlog import get_logger

from research.backtest.maker_engine import (
    CancelQuote,
    Hold,
    PostQuote,
    TickData,
)
from research.registry.schemas import (
    AlphaManifest,
    AlphaStatus,
    AlphaTier,
)

logger = get_logger("alpha.c33_txfd6_solo_passive_maker")

# TXFD6 instrument constants (not mutable by config).
_TXF_POINT_VALUE_NTD = 200  # NTD per pt on TXF (vs TMF 10 NTD/pt)
_TXF_RT_COST_PTS = 3.0       # retail RT, cited from memo

# Signal-layer method names we intentionally never call. Test asserts this.
_DISABLED_SIGNAL_LAYERS = (
    "_update_pe",
    "_update_queue",
    "_update_mfg",
    "_update_qi",
    "compute_pe_gate",
    "compute_queue_suppress",
    "compute_mfg_skew",
    "compute_qi_skew",
)


# ----------------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class C33Params:
    """Tuning parameters for C33 (R47-minimal on TXFD6).

    spread_threshold_pts
        Minimum TXFD6 spread (in pts) to quote. Default 5 = R47 convention;
        2×half_spread (2×2.5=5) matches RT 3pt + 2pt margin.
    max_pos
        TXF contract cap. T5 sweeps {1, 3, 5}. Default 1 (conservative, matches
        deployed R47 TMFD6).
    inventory_skew_tenths
        Fixed inventory skew — 0.X ticks per contract per unit. Default 2 = 0.2
        ticks/contract (R47 best practice).
    enable_pe_layer / enable_queue_layer / enable_mfg_layer / enable_qi_layer
        R47 signal layers. All default False — R47-minimal (R7 counterfactual:
        TMFD6-calibrated layers underperform 4:1 on TXFD6).
    """

    spread_threshold_pts: int = 5
    max_pos: int = 1
    inventory_skew_tenths: int = 2
    enable_pe_layer: bool = False
    enable_queue_layer: bool = False
    enable_mfg_layer: bool = False
    enable_qi_layer: bool = False


# ----------------------------------------------------------------------------
# TxfD6SoloMaker — MakerEngine-compatible strategy
# ----------------------------------------------------------------------------


class TxfD6SoloMaker:
    """R47-minimal maker for TXFD6 day session, MakerEngine-compatible.

    API mirrors the C14/C17 TxfFrontMonthMaker / TmfFrontMonthMaker classes:
      - ``on_tick(TickData)`` → list[PostQuote | CancelQuote | Hold]
      - ``on_fill(side, price, mid)`` — updates local pos
      - ``on_gap()`` — clears transient quote state
    """

    __slots__ = (
        "_params",
        "_position",
        "_last_bid",
        "_last_ask",
        "_tick_count",
        "_spread_blocked",
        "_max_pos_blocked",
        "_quotes_posted",
        "_active_symbol",
    )

    def __init__(
        self,
        params: C33Params | None = None,
        active_symbol: str = "TXFD6",
    ) -> None:
        self._params = params or C33Params()
        self._position = 0
        self._last_bid: int | None = None
        self._last_ask: int | None = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._max_pos_blocked = 0
        self._quotes_posted = 0
        self._active_symbol = active_symbol

    # ---- Observability ---------------------------------------------------

    @property
    def position(self) -> int:
        return self._position

    @property
    def params(self) -> C33Params:
        return self._params

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def spread_blocked(self) -> int:
        return self._spread_blocked

    @property
    def max_pos_blocked(self) -> int:
        return self._max_pos_blocked

    @property
    def quotes_posted(self) -> int:
        return self._quotes_posted

    @property
    def active_symbol(self) -> str:
        return self._active_symbol

    # ---- MakerStrategy protocol -----------------------------------------

    def on_tick(
        self, tick: TickData
    ) -> list[PostQuote | CancelQuote | Hold]:
        self._tick_count += 1
        if tick.is_trade:
            return [Hold()]
        return self._on_bidask(tick)

    def _on_bidask(
        self, tick: TickData
    ) -> list[PostQuote | CancelQuote | Hold]:
        params = self._params
        if tick.bid_price <= 0 or tick.ask_price <= 0:
            return [Hold()]
        if tick.ask_price <= tick.bid_price:
            return [Hold()]
        scale = tick.scale
        spread_raw = tick.ask_price - tick.bid_price
        spread_pts = spread_raw // scale
        if spread_pts < params.spread_threshold_pts:
            self._spread_blocked += 1
            return [Hold()]

        # R47-minimal: signal-layer gates are intentionally NOT evaluated.
        # (See _DISABLED_SIGNAL_LAYERS — enforced by test; attribute absence.)

        actions: list[PostQuote | CancelQuote | Hold] = []
        pos = self._position
        max_pos = params.max_pos

        # Inventory skew (scaled-integer, R47 best practice).
        skew = (pos * params.inventory_skew_tenths * scale) // 10
        bid_quote = tick.bid_price - skew
        ask_quote = tick.ask_price - skew

        bid_moved = bid_quote != self._last_bid
        ask_moved = ask_quote != self._last_ask

        # Buy side: only if inventory not at +max_pos cap.
        if pos < max_pos and bid_moved:
            actions.append(PostQuote(side="buy", price=bid_quote, qty=1))
            self._last_bid = bid_quote
            self._quotes_posted += 1
        elif pos >= max_pos:
            self._max_pos_blocked += 1
        # Sell side: only if inventory not at -max_pos cap.
        if pos > -max_pos and ask_moved:
            actions.append(PostQuote(side="sell", price=ask_quote, qty=1))
            self._last_ask = ask_quote
            self._quotes_posted += 1

        return actions or [Hold()]

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        if side == "buy":
            self._position += 1
            self._last_bid = None
        elif side == "sell":
            self._position -= 1
            self._last_ask = None
        else:
            logger.warning("c33_unknown_fill_side", side=side)

    def on_gap(self) -> None:
        self._last_bid = None
        self._last_ask = None

    def reset(self) -> None:
        self._position = 0
        self._last_bid = None
        self._last_ask = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._max_pos_blocked = 0
        self._quotes_posted = 0


# ----------------------------------------------------------------------------
# AlphaProtocol shim
# ----------------------------------------------------------------------------


class C33Alpha:
    """AlphaProtocol wrapper for registry smoke-path."""

    __slots__ = ("_maker", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C33Params | None = None,
        active_symbol: str = "TXFD6",
    ) -> None:
        self._maker = TxfD6SoloMaker(params=params, active_symbol=active_symbol)
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c33_txfd6_solo_passive_maker",
            hypothesis=(
                "R47-minimal (spread-gate + inventory-skew; signal layers "
                "PE/Queue/MFG/QI DISABLED) applied to TXFD6 day session "
                "captures +0.21 pt per fill under MAKER FULL-CYCLE framework "
                "(cycle gross 3.42 pt > RT 3 pt = +0.42 pt margin). "
                "R47-full-QI (calibrated on TMFD6) underperforms R47-minimal "
                "4:1 on TXFD6 — layer transfer is rejected by R7 T1 "
                "counterfactual. H2 framework validity is CONDITIONAL on T5 "
                "demonstrating close-side fills are MAKER for >=80% of cycles."
            ),
            formula=(
                "R47-minimal at spread_threshold_pts=5, max_pos∈{1,3,5} "
                "(T5 swept), inventory_skew_tenths=2. Signal layers DISABLED. "
                "Post at best bid/ask, maintain inventory within max_pos, "
                "flatten at EOD."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "r47_structural_properties",
                "feedback_taifex_fee_structure",
                "memory/backtest_method_reliability",
                "2008_Avellaneda_Stoikov_HFT_LOB",
                "2015_Cartea_Jaimungal_Penalva_MM_econ",
                "2010_Cont_Stoikov_Talreja_queue_fill_prob",
            ),
            data_fields=(
                "bid_price",
                "ask_price",
                "bid_qty",
                "ask_qty",
                "trade_price",
                "trade_volume",
                "trade_direction",
                "mid_price",
                "spread_pts",
            ),
            complexity="O(1)",
            status=AlphaStatus.PROTOTYPE,
            tier=AlphaTier.TIER_1,
            rust_module=None,
            latency_profile="shioaji_sim_p95_v2026-03-04",
            roles_used=(),
            skills_used=("hft-backtester",),
            feature_set_version=None,
            strategy_type="maker",
            instrument="TXFD6",
        )

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def maker(self) -> TxfD6SoloMaker:
        return self._maker

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._maker.reset()
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C33Alpha",
    "C33Params",
    "TxfD6SoloMaker",
    "_DISABLED_SIGNAL_LAYERS",
    "_TXF_POINT_VALUE_NTD",
    "_TXF_RT_COST_PTS",
]
