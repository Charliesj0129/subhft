"""C63 — TXFD6 R47-minimal with tight spread threshold under institutional RT.

Variant of the C33 PROMOTE (TXFD6 R47-minimal, R7-prior). Single lever
changed: `spread_threshold_pts` lowered from 5 to 3. Rationale: at inst
TXF RT 1.5 pt (shared-context.yaml#cost_model.TXF, halves retail 3 pt),
the 3 pt spread band becomes profitable half-spread. Expands the
tradeable-tick universe while keeping the C33 mechanism (mp=3, qf=5%,
R47-minimal signal layers OFF, non-|pos|-gated) intact.

R14-prior guard (structural-break vs session-variant):
The lowered threshold must be a STRUCTURAL change justified by the new
cost regime — NOT a session-adaptive / dynamic threshold. Parameter is
fixed-int; no TOD/session modulation. Passes R14-prior rule.

Cost citation (MANDATORY per shared-context.yaml#cost_model.notes):
  `shared-context.yaml#cost_model.TXF`
    rt_cost_ntd: 300, rt_cost_pts: 1.5, point_value_ntd: 200
    tier: institutional_estimate
    confirmed: false
  Any PROMOTE of C63 MUST carry the flag
  `requires_broker_confirmation_before_live: true`.

Parameters (canonical, per candidate pool C63):
  spread_threshold_pts = 3  (LOWERED from C33's 5; inst RT 1.5 justifies)
  max_pos              = 3  (canonical; same as C33 research operating point)
  queue_share          = 0.05 (informational; same as C33)
  inventory_skew_tenths= 2  (0.2 ticks/contract fixed, same as C33)
  Signal layers        : all four D1/D2/D3/D4 DISABLED (R47-minimal; C33
                         precedent — TXFD6 is R47-minimal with no QI layer,
                         unlike TMFD6 which carries D4).
  NON-|pos|-gated      : avoids C22-class meta-kill by construction
                         (no skew is |pos|-thresholded; inventory skew is
                         LINEAR in pos, not a |pos| gate).

Interfaces:
  - ``TxfD6R47TightSpreadMaker`` conforms to
    ``research.backtest.maker_engine.MakerStrategy``.
  - ``C63Alpha`` conforms to ``research.registry.schemas.AlphaProtocol``.

Research-module float exception (Rule 11 of 25-architecture-governance):
offline/CLI research code. Prices arriving from CK use scaled-integer
convention (default scale 1e6; TXFD6 CK x1_000_000). All quote-decision
arithmetic is integer.

Precision Law (CLAUDE.md #4): scaled-integer math for all quote decisions.
Timestamps are monotonic ns supplied by the caller; strategy never calls
``datetime.now()`` / ``time.time()``.
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

logger = get_logger("alpha.c63_txfd6_r47_tight_spread")

# TXFD6 instrument constants (immutable per contract specs).
_TXF_POINT_VALUE_NTD = 200              # NTD per pt on TXF (vs TMF 10)
_TXF_INST_RT_COST_PTS = 1.5             # shared-context.yaml#cost_model.TXF (inst est.)
_TXF_RETAIL_RT_COST_PTS = 3.0           # retail reference (memory/feedback_taifex_fee_structure)

# All four R47 signal layers are hard-disabled (R47-minimal, C33 precedent).
# C63 keeps C33's discipline — no D4 QI on TXFD6 (TMFD6-calibrated thresholds
# do NOT transfer per R7 T1 counterfactual).
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
class C63Params:
    """Tuning parameters for C63 (TXFD6 R47-minimal with tighter spread).

    spread_threshold_pts
        Minimum TXFD6 spread (in pts) to quote. Default 3 (LOWERED from
        C33's 5). Rationale: inst TXF RT 1.5 pt halves retail 3 pt RT; the
        3 pt band is now profitable half-spread. Fixed; NOT session-adaptive
        (R14-prior structural-break guard).
    max_pos
        TXF contract cap. Canonical 3 (same as C33 research operating
        point). T5 scorecard splits {1, 3, 5} on request.
    inventory_skew_tenths
        Fixed inventory skew — 0.X ticks per contract per unit position.
        Default 2 = 0.2 ticks/contract. Linear in pos — NOT a C22-class
        |pos|-gate.
    enable_pe_layer / enable_queue_layer / enable_mfg_layer / enable_qi_layer
        All four R47 signal layers. All default False — R47-minimal
        (C33 precedent; R7 T1 counterfactual: layers do not transfer on
        TXFD6 without calibration).
    """

    spread_threshold_pts: int = 3
    max_pos: int = 3
    inventory_skew_tenths: int = 2
    enable_pe_layer: bool = False
    enable_queue_layer: bool = False
    enable_mfg_layer: bool = False
    enable_qi_layer: bool = False


# ----------------------------------------------------------------------------
# TxfD6R47TightSpreadMaker — MakerEngine-compatible strategy
# ----------------------------------------------------------------------------


class TxfD6R47TightSpreadMaker:
    """R47-minimal maker for TXFD6 day session with tightened spread gate.

    API mirrors C33 TxfD6SoloMaker / C60 TmfD6SoloMakerMinimal:
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
        "_quotes_posted",
        "_active_symbol",
    )

    def __init__(
        self,
        params: C63Params | None = None,
        active_symbol: str = "TXFD6",
    ) -> None:
        self._params = params or C63Params()
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
    def params(self) -> C63Params:
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

        # Inventory skew (scaled-integer, R47 best practice; LINEAR in pos).
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
            logger.warning("c63_unknown_fill_side", side=side)

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


class C63Alpha:
    """AlphaProtocol wrapper for registry smoke-path."""

    __slots__ = ("_maker", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C63Params | None = None,
        active_symbol: str = "TXFD6",
    ) -> None:
        self._maker = TxfD6R47TightSpreadMaker(
            params=params, active_symbol=active_symbol
        )
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c63_txfd6_r47_tight_spread",
            hypothesis=(
                "R47-minimal on TXFD6 with spread_threshold_pts=3 (lowered "
                "from C33's 5) under institutional-tier TXF RT 1.5 pt. "
                "At inst RT 1.5 pt (half of retail 3 pt), the 3 pt spread "
                "band becomes profitable half-spread (gross 3 pt - RT 1.5 "
                "pt = +1.5 pt margin per full cycle). Captures more cycles "
                "in compressed-regime TXFD6 sessions while preserving "
                "non-|pos|-gated discipline. Mechanism transfer from C33 "
                "PROMOTE (R7-prior) with a single-lever change — fixed "
                "threshold, NOT session-adaptive (R14-prior guard)."
            ),
            formula=(
                "R47-minimal at spread_threshold_pts=3, max_pos=3, "
                "inventory_skew_tenths=2. All four signal layers "
                "(PE/Queue/MFG/QI) DISABLED. Post at best bid/ask, maintain "
                "inventory within max_pos, flatten at EOD."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "r47_structural_properties",
                "r47_tmfd6_economics",
                "c33_txfd6_solo_passive_maker",
                "shared-context_2026-04-19_cost_model",
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
    def maker(self) -> TxfD6R47TightSpreadMaker:
        return self._maker

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._maker.reset()
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C63Alpha",
    "C63Params",
    "TxfD6R47TightSpreadMaker",
    "_DISABLED_SIGNAL_LAYERS",
    "_TXF_POINT_VALUE_NTD",
    "_TXF_INST_RT_COST_PTS",
    "_TXF_RETAIL_RT_COST_PTS",
]
