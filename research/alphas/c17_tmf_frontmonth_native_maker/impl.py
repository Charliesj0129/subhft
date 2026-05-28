"""C17 — R47 Maker on TMF rolling front-month.

TMF analog of C14 (R6 PROMOTE, 2026-04-17). Strategy code reuses R47's
signal sub-states via composition — **no modifications to R47 or C14**.

Interfaces:
  - ``TmfFrontMonthMaker`` conforms to
    ``research.backtest.maker_engine.MakerStrategy``.
  - ``C17Alpha`` conforms to ``research.registry.schemas.AlphaProtocol``.

SWITCH semantics (per R10-T2 P3 WARN):
  C17 is a SWITCH on the deployed TMFD6 R47 max_pos=1 — these two
  strategies MUST NOT run simultaneously. Running both creates R51-C1b's
  "TMFD6 multi-instrument" failure mode (net −109K NTD, KILLED direction).

  Live deployment (post-shadow) requires: (a) disable the deployed R47
  TMFD6 max_pos=1 entry in strategies.yaml, (b) flatten any existing
  TMFD6 position, (c) enable C17_TMF_FRONTMONTH_MAKER. Documented in
  the shadow playbook accompanying this file.

Defaults mirror C14:
  spread_threshold_pts=3, max_pos=3, signal layers DISABLED (R47
  structural minimal). TMF-specific differences are ONLY in the cost
  model (0.7 tax + 1.3 commission = 4.0 pt RT on TMF vs 0.48 pt RT on
  TXF) and the point value (10 NTD/pt vs 200 NTD/pt). The strategy
  itself is venue-invariant.
"""

from __future__ import annotations

from dataclasses import dataclass

from structlog import get_logger

from research.alphas.r47_maker_pivot.impl import _MFGState, _PEState, _QueueState
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

logger = get_logger("alpha.c17_tmf_frontmonth_native_maker")


# ----------------------------------------------------------------------------
# Strategy parameters
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class C17Params:
    """Tuning parameters for C17 strategy (TMF variant of C14Params).

    spread_threshold_pts: TMF RT cost = 4 pt. Must be > 4 for cost
    viability. Default 5 is consistent with deployed R47 TMFD6 max_pos=1
    (spr>=5). This is STRICTLY HIGHER than C14's 3 — TXF RT 0.48 pt
    allows spread_threshold=3, but TMF's 4 pt RT requires 5+ pt spread to
    produce positive half-spread capture after cost.
    """

    # L1 — spread gate (hard cost-viability floor, TMF RT 4.0 pt)
    spread_threshold_pts: int = 5
    # L2 — R47 signal layers (all disabled; R47 structural minimal)
    pe_danger_threshold: float = 0.0
    pe_window: int = 100
    queue_cancel_threshold: float = 1.0
    queue_ema_alpha: float = 0.05
    mfg_skew_z_threshold: float = 100.0
    mfg_ema_alpha: float = 0.01
    # Inventory — R47 structural optimum max_pos=3
    max_pos: int = 3
    # Fixed inventory skew — 0.2 ticks/contract (R47 best practice)
    inventory_skew_tenths: int = 2


# ----------------------------------------------------------------------------
# MakerStrategy implementation — mirror of C14's TxfFrontMonthMaker
# ----------------------------------------------------------------------------


class TmfFrontMonthMaker:
    """R47 maker logic, TMF front-month aware, MakerEngine-compatible.

    Same rollover API as C14's ``TxfFrontMonthMaker``:
      - ``set_active_symbol(new_symbol)`` — switch contract, clear price memory
      - ``flatten_position()`` — zero local pos, return prior pos
      - ``on_tick(TickData)`` → list[PostQuote|CancelQuote|Hold]
      - ``on_fill(side, price, mid)`` — updates local pos
      - ``on_gap()`` — clears transient quote state
    """

    __slots__ = (
        "_params",
        "_pe_states",
        "_queue_states",
        "_mfg_states",
        "_position",
        "_active_symbol",
        "_last_bid",
        "_last_ask",
        "_tick_count",
        "_spread_blocked",
        "_pe_blocked",
        "_queue_blocked",
        "_quotes_posted",
        "_rollover_events",
    )

    def __init__(
        self,
        params: C17Params | None = None,
        active_symbol: str | None = None,
    ) -> None:
        self._params = params or C17Params()
        self._pe_states: dict[str, _PEState] = {}
        self._queue_states: dict[str, _QueueState] = {}
        self._mfg_states: dict[str, _MFGState] = {}
        self._position = 0
        self._active_symbol: str | None = active_symbol
        self._last_bid: int | None = None
        self._last_ask: int | None = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._pe_blocked = 0
        self._queue_blocked = 0
        self._quotes_posted = 0
        self._rollover_events = 0

    # ---- Rollover API ------------------------------------------------------

    def set_active_symbol(self, new_symbol: str) -> None:
        if self._active_symbol == new_symbol:
            return
        logger.info(
            "c17_rollover",
            outgoing=self._active_symbol,
            incoming=new_symbol,
        )
        self._active_symbol = new_symbol
        self._last_bid = None
        self._last_ask = None
        self._position = 0
        self._rollover_events += 1

    def flatten_position(self) -> int:
        prior = self._position
        self._position = 0
        self._last_bid = None
        self._last_ask = None
        return prior

    @property
    def active_symbol(self) -> str | None:
        return self._active_symbol

    @property
    def position(self) -> int:
        return self._position

    @property
    def rollover_events(self) -> int:
        return self._rollover_events

    # ---- MakerStrategy protocol -------------------------------------------

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

        sym = self._active_symbol or "_unknown_"
        pe = self._get_pe(sym)
        total_qty = tick.bid_qty + tick.ask_qty
        if total_qty > 0:
            qi = (tick.bid_qty - tick.ask_qty) / total_qty
            pe.update(qi)
        if params.pe_danger_threshold > 0.0 and pe.warmed_up:
            if pe.h < params.pe_danger_threshold:
                self._pe_blocked += 1
                return [Hold()]

        qstate = self._get_queue(sym)
        qstate.update(tick.bid_qty, tick.ask_qty)
        suppress_bid = False
        suppress_ask = False
        if params.queue_cancel_threshold < 1.0 and qstate.warmed_up:
            if qstate.p_depl_bid > params.queue_cancel_threshold:
                suppress_bid = True
            if qstate.p_depl_ask > params.queue_cancel_threshold:
                suppress_ask = True
            if suppress_bid or suppress_ask:
                self._queue_blocked += 1

        actions: list[PostQuote | CancelQuote | Hold] = []
        pos = self._position
        max_pos = params.max_pos

        skew = (pos * params.inventory_skew_tenths * scale) // 10
        bid_quote = tick.bid_price - skew
        ask_quote = tick.ask_price - skew

        bid_moved = bid_quote != self._last_bid
        ask_moved = ask_quote != self._last_ask

        if not suppress_bid and pos < max_pos and bid_moved:
            actions.append(PostQuote(side="buy", price=bid_quote, qty=1))
            self._last_bid = bid_quote
            self._quotes_posted += 1
        if not suppress_ask and pos > -max_pos and ask_moved:
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
            logger.warning("c17_unknown_fill_side", side=side)

    def on_gap(self) -> None:
        self._last_bid = None
        self._last_ask = None

    # ---- R47 state getters ------------------------------------------------

    def _get_pe(self, symbol: str) -> _PEState:
        state = self._pe_states.get(symbol)
        if state is None:
            state = _PEState(d=4, window=self._params.pe_window)
            self._pe_states[symbol] = state
        return state

    def _get_queue(self, symbol: str) -> _QueueState:
        state = self._queue_states.get(symbol)
        if state is None:
            state = _QueueState(ema_alpha=self._params.queue_ema_alpha)
            self._queue_states[symbol] = state
        return state

    def _get_mfg(self, symbol: str) -> _MFGState:
        state = self._mfg_states.get(symbol)
        if state is None:
            state = _MFGState(ema_alpha=self._params.mfg_ema_alpha)
            self._mfg_states[symbol] = state
        return state


# ----------------------------------------------------------------------------
# AlphaProtocol shim (mirror of C14Alpha)
# ----------------------------------------------------------------------------


class C17Alpha:
    """AlphaProtocol wrapper around TmfFrontMonthMaker (registry smoke-path)."""

    __slots__ = ("_maker", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C17Params | None = None,
        active_symbol: str | None = None,
    ) -> None:
        self._maker = TmfFrontMonthMaker(params=params, active_symbol=active_symbol)
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c17_tmf_frontmonth_native_maker",
            hypothesis=(
                "R47's structural edge is venue-invariant. Running on TMF "
                "rolling front-month (TMFB6 → TMFC6 → TMFD6) captures the "
                "same edge as C14 on the Mini-TAIEX contract size. "
                "SWITCH semantics: must NOT run concurrently with the "
                "deployed TMFD6 R47 max_pos=1 (R51-C1b kill direction)."
            ),
            formula=(
                "R47 three-layer maker (PE gate / queue cancel / MFG skew, "
                "minimal-layer defaults) driven on whichever TMF contract "
                "is current front-month; per-tick position flattened on "
                "rollover day. spread_threshold_pts=5 (TMF RT=4.0 pt)."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "r47_structural_properties",
                "r47_backtest_data_regression",
                "c14_txf_frontmonth_native_maker",
                "2008_Avellaneda_Stoikov_HFT_LOB",
            ),
            data_fields=(
                "bid_px",
                "ask_px",
                "bid_qty",
                "ask_qty",
                "mid_price",
                "spread_pts",
                "trade_price",
                "trade_volume",
            ),
            complexity="O(1) per tick",
            status=AlphaStatus.PROTOTYPE,
            tier=AlphaTier.TIER_1,
            rust_module=None,
            latency_profile="sim_p95_v2026-02-26",
            roles_used=("architect", "code-reviewer"),
            skills_used=("hft-backtester",),
            feature_set_version=None,
            strategy_type="maker",
            instrument="TMF_frontmonth",
        )

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def maker(self) -> TmfFrontMonthMaker:
        return self._maker

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._maker = TmfFrontMonthMaker(
            params=self._maker._params,  # type: ignore[attr-defined]
            active_symbol=None,
        )
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C17Alpha",
    "C17Params",
    "TmfFrontMonthMaker",
]
