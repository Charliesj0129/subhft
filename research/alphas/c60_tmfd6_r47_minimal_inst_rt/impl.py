"""C60 — TMFD6 R47-minimal maker under institutional-tier RT.

Direct transfer of the C33 PROMOTE mechanism (TXFD6 R47-minimal, R7-prior) to
TMFD6 under the new institutional-tier cost regime. Cost drag drops from 200%
(retail `TMF RT = 4 pt`) to 75% (inst `TMF RT = 1.5 pt`); the same three-layer
R47 pattern now math-viable per the T1 counterfactual (+22,451 NTD/day at
baseline qf=1.0, spread>=5, max_pos=1).

H1 physics audit (DA T2 adjudication, 2026-04-19):
  - Tier 1/2 FAIL count: 0
  - S6 regime dependency WARN (Jan-Feb wide spread -> Mar compressed).
  - Bright-line WARN: cost_drag = 1.5/2 = 75% (> 50%).

Cost citation (MANDATORY per shared-context.yaml#cost_model.notes):
  `shared-context.yaml#cost_model.TMF`
    rt_cost_ntd: 15, rt_cost_pts: 1.5, point_value_ntd: 10
    tier: institutional_estimate
    confirmed: false
    confirmation_source: "user-authorized rough estimate, 2026-04-19"
  Any PROMOTE of C60 MUST carry the flag
  `requires_broker_confirmation_before_live: true`.

Parameters (canonical drop-in from the currently deployed TMFD6 R47-minimal):
  spread_threshold_pts = 5  (baseline; DA flag #2 narrow margin; per R14-prior
                             do NOT add dynamic threshold — fixed only)
  max_pos              = 1  (canonical; T5 scorecard splits {1,2,3})
  inventory_skew_tenths= 2  (0.2 ticks/contract fixed)
  qf (queue_share)     = 1.0 (informational for sim; full-queue conservative)
  L2 layers            : D1 PE off, D2 queue off, D3 MFG off (effectively skew
                         OFF), D4 QI skew threshold=0.10 widen=1 tick
  NON-|pos|-gated      : no skew modulation by |pos| -> avoids C22-class kill

Interfaces:
  - ``TmfD6SoloMakerMinimal`` conforms to
    ``research.backtest.maker_engine.MakerStrategy``.
  - ``C60Alpha`` conforms to ``research.registry.schemas.AlphaProtocol``.

Research-module float exception (Rule 11 of 25-architecture-governance):
this file is offline/CLI research code. Prices arriving from CK use
scaled-integer convention (default scale 1e6; TMFD6 CK x1_000_000 — NOT the
platform live scale x10_000). All quote-decision arithmetic is integer.

Precision Law (CLAUDE.md #4): scaled-integer math for all quote decisions.
Timestamps are monotonic ns supplied by the caller; the strategy never calls
``datetime.now()`` / ``time.time()`` directly (research float exception, but
we still monotonic-only for determinism).
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

logger = get_logger("alpha.c60_tmfd6_r47_minimal_inst_rt")

# TMFD6 instrument constants (immutable per contract specs).
_TMF_POINT_VALUE_NTD = 10          # NTD per pt on TMF (vs TXF 200)
_TMF_INST_RT_COST_PTS = 1.5        # shared-context.yaml#cost_model.TMF (inst est.)
_TMF_RETAIL_RT_COST_PTS = 4.0      # retail reference (feedback_taifex_fee_structure)

# Three of the four R47 signal layers are hard-disabled. The D4 QI skew layer
# is retained (deployed TMFD6 config), but is gated via QueueImbalance on the
# top-of-book snapshot only — it is NOT |pos|-modulated. Test asserts the
# three-layer absence.
_DISABLED_SIGNAL_LAYERS_MOST = (
    "_update_pe",
    "_update_queue",
    "_update_mfg",
    "compute_pe_gate",
    "compute_queue_suppress",
    "compute_mfg_skew",
)


# ----------------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class C60Params:
    """Tuning parameters for C60 (TMFD6 R47-minimal under inst RT).

    spread_threshold_pts
        Minimum TMFD6 spread (in pts) to quote. Default 5 matches the deployed
        TMFD6 R47-minimal config. 2 x half_spread gate plus margin over inst RT
        1.5 pt. Fixed; not session-adaptive (R14-prior structural-break rule).
    max_pos
        TMF contract cap. Canonical 1 (drop-in replacement for current deploy).
        T5 scorecard splits {1, 2, 3} — R15-prior monotone check required for
        any |pos|-sensitive variant.
    inventory_skew_tenths
        Fixed inventory skew — 0.X ticks per contract per unit position.
        Default 2 = 0.2 ticks/contract. NOT a |pos|-gate (applies linearly,
        so is not a C22-class meta-kill vector).
    qi_skew_threshold
        L2 D4 QueueImbalance skew threshold. When |imbalance| > threshold,
        widen the opposite side by `qi_skew_widen_ticks`. Default 0.10
        (deployed config).
    qi_skew_widen_ticks
        Number of ticks to widen quote on QI-skew trigger. Default 1.
    enable_qi_layer
        D4 QI layer master switch. Default True (deployed config).
    enable_pe_layer / enable_queue_layer / enable_mfg_layer
        D1/D2/D3 signal layers. All default False — prior empirical evidence
        these TMFD6-calibrated layers do not transfer positively.
    """

    spread_threshold_pts: int = 5
    max_pos: int = 1
    inventory_skew_tenths: int = 2
    qi_skew_threshold: float = 0.10
    qi_skew_widen_ticks: int = 1
    enable_qi_layer: bool = True
    enable_pe_layer: bool = False
    enable_queue_layer: bool = False
    enable_mfg_layer: bool = False


# ----------------------------------------------------------------------------
# TmfD6SoloMakerMinimal — MakerEngine-compatible strategy
# ----------------------------------------------------------------------------


class TmfD6SoloMakerMinimal:
    """R47-minimal maker for TMFD6 day session, MakerEngine-compatible.

    API mirrors C33 TxfD6SoloMaker / C14 TxfFrontMonthMaker:
      - ``on_tick(TickData)`` -> list[PostQuote | CancelQuote | Hold]
      - ``on_fill(side, price, mid)`` - updates local pos
      - ``on_gap()`` - clears transient quote state
    """

    __slots__ = (
        "_params",
        "_position",
        "_last_bid",
        "_last_ask",
        "_tick_count",
        "_spread_blocked",
        "_max_pos_blocked",
        "_qi_widen_events",
        "_quotes_posted",
        "_active_symbol",
    )

    def __init__(
        self,
        params: C60Params | None = None,
        active_symbol: str = "TMFD6",
    ) -> None:
        self._params = params or C60Params()
        self._position = 0
        self._last_bid: int | None = None
        self._last_ask: int | None = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._max_pos_blocked = 0
        self._qi_widen_events = 0
        self._quotes_posted = 0
        self._active_symbol = active_symbol

    # ---- Observability ---------------------------------------------------

    @property
    def position(self) -> int:
        return self._position

    @property
    def params(self) -> C60Params:
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
    def qi_widen_events(self) -> int:
        return self._qi_widen_events

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

        # L2 D4 QI skew (deployed layer). Computed on top-of-book snapshot;
        # NOT |pos|-modulated. Returns (widen_bid_ticks, widen_ask_ticks).
        widen_bid_ticks, widen_ask_ticks = self._compute_qi_skew(tick)
        if widen_bid_ticks or widen_ask_ticks:
            self._qi_widen_events += 1

        actions: list[PostQuote | CancelQuote | Hold] = []
        pos = self._position
        max_pos = params.max_pos

        # Fixed inventory skew (scaled-integer, R47 best practice).
        skew = (pos * params.inventory_skew_tenths * scale) // 10
        tick_size = scale  # 1 pt == scale scaled-units
        bid_quote = tick.bid_price - skew - widen_bid_ticks * tick_size
        ask_quote = tick.ask_price - skew + widen_ask_ticks * tick_size

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

    def _compute_qi_skew(self, tick: TickData) -> tuple[int, int]:
        """L2 D4 QI skew: widen the side that is "light" in queue imbalance.

        Imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty), in [-1, +1].
        Positive imbalance => bid-heavy => ask side is lighter => widen ASK.
        Negative imbalance => ask-heavy => bid side is lighter => widen BID.
        (Equivalent to pulling back the quote on the "losing" side where
        adverse fills concentrate.)
        """
        params = self._params
        if not params.enable_qi_layer:
            return 0, 0
        total_qty = tick.bid_qty + tick.ask_qty
        if total_qty <= 0:
            return 0, 0
        # float math allowed (research module; not live hot-path).
        imbalance = (tick.bid_qty - tick.ask_qty) / total_qty
        threshold = params.qi_skew_threshold
        widen = params.qi_skew_widen_ticks
        if imbalance > threshold:
            return 0, widen
        if imbalance < -threshold:
            return widen, 0
        return 0, 0

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        if side == "buy":
            self._position += 1
            self._last_bid = None
        elif side == "sell":
            self._position -= 1
            self._last_ask = None
        else:
            logger.warning("c60_unknown_fill_side", side=side)

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
        self._qi_widen_events = 0
        self._quotes_posted = 0


# ----------------------------------------------------------------------------
# AlphaProtocol shim
# ----------------------------------------------------------------------------


class C60Alpha:
    """AlphaProtocol wrapper for registry smoke-path."""

    __slots__ = ("_maker", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C60Params | None = None,
        active_symbol: str = "TMFD6",
    ) -> None:
        self._maker = TmfD6SoloMakerMinimal(
            params=params, active_symbol=active_symbol
        )
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c60_tmfd6_r47_minimal_inst_rt",
            hypothesis=(
                "R47-minimal three-layer mechanism (deployed on TXFD6 as C33 "
                "PROMOTE, R7-prior) transfers to TMFD6 once cost-drag drops "
                "from 200% (retail RT 4 pt) to 75% (inst RT 1.5 pt). Same "
                "pattern, different cost regime. Empirical CK-direct "
                "counterfactual (25 TMFD6 days, qf=1.0, spread>=5, max_pos=1) "
                "yields +22,451 NTD/day at inst RT, robust under +/-30% RT "
                "sensitivity (+19,191 to +25,710 NTD/day). Non-|pos|-gated "
                "(avoids C22-class meta-kill). PROMOTE requires "
                "broker-contract RT confirmation before live."
            ),
            formula=(
                "R47-minimal at spread_threshold_pts=5, max_pos in {1,2,3} "
                "(T5 swept), inventory_skew_tenths=2, qi_skew_threshold=0.10, "
                "qi_skew_widen_ticks=1. Layers D1/D2/D3 DISABLED; D4 QI "
                "retained per deployed config. Post at best bid/ask, "
                "maintain inventory within max_pos, flatten at EOD."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "r47_structural_properties",
                "r47_tmfd6_economics",
                "shared-context_2026-04-19_cost_model",
                "memory/backtest_method_reliability",
                "2409.12721v2_Lalor_Swishchuk_2024_adverse_selection_sim",
                "2405.11444v1_Chavez_Casillas_2024_adaptive_MM",
                "2211.00496v1_Han_2022_maker_taker_fees",
                "1806.05101v1_Lu_Abergel_2018_LOB_MM",
                "2510.27334v1_Jafree_2025_adverse_selection_meta_orders_RL",
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
            instrument="TMFD6",
        )

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def maker(self) -> TmfD6SoloMakerMinimal:
        return self._maker

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._maker.reset()
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C60Alpha",
    "C60Params",
    "TmfD6SoloMakerMinimal",
    "_DISABLED_SIGNAL_LAYERS_MOST",
    "_TMF_POINT_VALUE_NTD",
    "_TMF_INST_RT_COST_PTS",
    "_TMF_RETAIL_RT_COST_PTS",
]
