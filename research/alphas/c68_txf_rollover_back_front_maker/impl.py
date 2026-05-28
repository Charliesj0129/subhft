"""C68 — TXF rollover-week back-to-front passive maker.

R4 T1 CONDITIONAL PROCEED (2026-04-19). Researcher T1 identified the
narrow-spread transition window (TXFD6 Feb 2026-02-23..25 analog: median
spread 12-16 pt, 747-1323 trades/day) as the viable mechanism. The
task-brief "hedge pair" framing is REJECTED: TAKE-leg hedge inverts edge
from +7.8 pt/RT to -0.9 pt/RT. C68 is therefore a SOLO PASSIVE MAKER on
the back-month, calendar-gated to activate only during the 3-day rollover
window.

Mechanism:
  - CALENDAR GATE: activate only during the 3-day window before + including
    the current front-month expiry date. Configured via
    `rollover_window_start_date` and `rollover_window_end_date`.
  - SPREAD GATE: min spread threshold (default 12 pt; TXFD6 Feb analog
    median). Narrow enough to quote profitably at inst RT 1.5 pt per leg.
  - MAX_POS cap: default 1 (data-constrained; 3-day analog sample).
  - R47-minimal: all four signal layers DISABLED (TXFD6 precedent).
  - Inventory skew LINEAR in pos (not |pos|-gated; no C22-class kill).

No cross-instrument hedge leg — the "hedge" in the task brief is a risk-
offset via waiting for opposite-side passive fill. R5-prior hedge-qty
rule N/A.

Cost citation: `shared-context.yaml#cost_model.TXF`
  rt_cost_pts: 1.5 (inst est.; confirmed=false)
  point_value_ntd: 200
Any PROMOTE of C68 MUST carry `requires_broker_confirmation_before_live: true`.

Research-module float exception (Rule 11 of 25-architecture-governance).
Precision Law (CLAUDE.md #4): scaled-int math for all quote decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date

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

logger = get_logger("alpha.c68_txf_rollover_back_front_maker")

# TXF instrument constants (TXF family; back-month contract = same pt value).
_TXF_POINT_VALUE_NTD = 200
_TXF_INST_RT_COST_PTS = 1.5
_TXF_RETAIL_RT_COST_PTS = 3.0

# R47-minimal: all four signal layers disabled (TXFD6 precedent via C33).
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

# Canonical rollover window (N trading days inclusive, ending at expiry).
_ROLLOVER_WINDOW_CANONICAL_DAYS = 3


def is_in_rollover_window(
    session_date: _date,
    rollover_start: _date,
    rollover_end: _date,
) -> bool:
    """Calendar gate: is `session_date` within the inclusive rollover window?

    Args:
        session_date: current session date.
        rollover_start: first day of the rollover window (inclusive).
        rollover_end: last day (expiry of current front; inclusive).
    """
    return rollover_start <= session_date <= rollover_end


# ----------------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class C68Params:
    """Tuning parameters for C68.

    spread_threshold_pts
        Minimum back-month spread (pts) to quote. Default 12 matches the
        TXFD6 Feb analog median (12-16 pt). Fixed, not adaptive (R14-prior
        structural-break guard — the threshold is justified by the known
        rollover-window spread regime, not by session variance).
    max_pos
        Contract cap. Default 1 (data-constrained on the 3-day analog).
        T5 scorecard may sweep {1, 2, 3}.
    inventory_skew_tenths
        Fixed linear inventory skew. Default 2 = 0.2 ticks/contract.
    rollover_window_start_date / rollover_window_end_date
        Inclusive calendar gate. Strategy quotes only when session_date is
        inside this window. Defaults None = no gate (for tests + backtest).
    rollover_window_days
        Documentation-only: canonical 3-day window (matches observed
        TXFD6 Feb 2026-02-23..25 analog).
    enable_pe_layer / enable_queue_layer / enable_mfg_layer / enable_qi_layer
        R47-minimal: all False by default (TXFD6 precedent).
    """

    spread_threshold_pts: int = 12
    max_pos: int = 1
    inventory_skew_tenths: int = 2
    rollover_window_start_date: _date | None = None
    rollover_window_end_date: _date | None = None
    rollover_window_days: int = _ROLLOVER_WINDOW_CANONICAL_DAYS
    enable_pe_layer: bool = False
    enable_queue_layer: bool = False
    enable_mfg_layer: bool = False
    enable_qi_layer: bool = False


# ----------------------------------------------------------------------------
# TxfRolloverBackFrontMaker — MakerEngine-compatible strategy
# ----------------------------------------------------------------------------


class TxfRolloverBackFrontMaker:
    """R47-minimal maker for back-month TXF during rollover window.

    API mirrors C33 / C63:
      - ``on_tick(TickData)`` -> list[PostQuote | CancelQuote | Hold]
      - ``on_fill(side, price, mid)`` - updates local pos
      - ``on_gap()`` - clears transient quote state

    Calendar gate is applied via `current_session_date` setter. If the
    rollover window is configured and current_session_date is outside it,
    `on_tick` returns `[Hold()]` and increments a counter.
    """

    __slots__ = (
        "_params",
        "_position",
        "_last_bid",
        "_last_ask",
        "_tick_count",
        "_spread_blocked",
        "_max_pos_blocked",
        "_rollover_gate_blocked",
        "_quotes_posted",
        "_active_symbol",
        "_current_session_date",
    )

    def __init__(
        self,
        params: C68Params | None = None,
        active_symbol: str = "TXFE6",
    ) -> None:
        self._params = params or C68Params()
        self._position = 0
        self._last_bid: int | None = None
        self._last_ask: int | None = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._max_pos_blocked = 0
        self._rollover_gate_blocked = 0
        self._quotes_posted = 0
        self._active_symbol = active_symbol
        self._current_session_date: _date | None = None

    # ---- Observability ---------------------------------------------------

    @property
    def position(self) -> int:
        return self._position

    @property
    def params(self) -> C68Params:
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
    def rollover_gate_blocked(self) -> int:
        return self._rollover_gate_blocked

    @property
    def quotes_posted(self) -> int:
        return self._quotes_posted

    @property
    def active_symbol(self) -> str:
        return self._active_symbol

    @property
    def current_session_date(self) -> _date | None:
        return self._current_session_date

    def set_session_date(self, session_date: _date) -> None:
        """Advance session calendar gate. Call once per simulated day at
        start-of-day."""
        self._current_session_date = session_date

    def _rollover_gate_pass(self) -> bool:
        """True if current session is inside rollover window (or no gate set).

        If either window bound is None, the gate is OPEN (for tests without
        calendar context). In production/backtest, set both dates.
        """
        p = self._params
        if p.rollover_window_start_date is None or p.rollover_window_end_date is None:
            return True
        if self._current_session_date is None:
            return True
        return is_in_rollover_window(
            self._current_session_date,
            p.rollover_window_start_date,
            p.rollover_window_end_date,
        )

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
        # Calendar gate FIRST — no quoting outside rollover window.
        if not self._rollover_gate_pass():
            self._rollover_gate_blocked += 1
            return [Hold()]

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

        actions: list[PostQuote | CancelQuote | Hold] = []
        pos = self._position
        max_pos = params.max_pos

        # Linear inventory skew (R47 best practice, NOT |pos|-gated).
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
            logger.warning("c68_unknown_fill_side", side=side)

    def on_gap(self) -> None:
        self._last_bid = None
        self._last_ask = None

    def emergency_unwind_required(self) -> bool:
        """True if the rollover window has ended and we still hold inventory.

        Caller (engine or backtest harness) should take appropriate action
        (flatten via taker cross) to avoid holding into the deep-back-month
        phase where spread reverts to 100+ pt.
        """
        if self._position == 0:
            return False
        if self._current_session_date is None:
            return False
        p = self._params
        if p.rollover_window_end_date is None:
            return False
        return self._current_session_date > p.rollover_window_end_date

    def reset(self) -> None:
        self._position = 0
        self._last_bid = None
        self._last_ask = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._max_pos_blocked = 0
        self._rollover_gate_blocked = 0
        self._quotes_posted = 0
        self._current_session_date = None


# ----------------------------------------------------------------------------
# AlphaProtocol shim
# ----------------------------------------------------------------------------


class C68Alpha:
    """AlphaProtocol wrapper for registry smoke-path."""

    __slots__ = ("_maker", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C68Params | None = None,
        active_symbol: str = "TXFE6",
    ) -> None:
        self._maker = TxfRolloverBackFrontMaker(
            params=params, active_symbol=active_symbol
        )
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c68_txf_rollover_back_front_maker",
            hypothesis=(
                "Back-month TXF contract entering rollover (becoming "
                "new-front in 3 days) exhibits a narrow-spread window "
                "(median 12-16 pt, TXFD6 Feb 2026-02-23..25 analog) with "
                "non-trivial trade activity (747-1323 trades/day). C68 "
                "quotes passively on the transitioning back-month during "
                "this window only, via calendar gate + spread gate. Per-trip "
                "gross 14 pt vs combined inst RT 3 pt = +7.8 pt/RT margin "
                "(linear estimate; R1 lesson: 5-8x optimistic vs fresh sim). "
                "Task-brief 'hedge pair' framing REJECTED: TAKE hedge leg "
                "inverts edge to negative. C68 is solo passive maker."
            ),
            formula=(
                "R47-minimal at spread_threshold_pts=12, max_pos=1, "
                "inventory_skew_tenths=2. All four signal layers "
                "(PE/Queue/MFG/QI) DISABLED. Calendar-gated: active only "
                "during configured 3-day rollover window. Post at best "
                "bid/ask; flatten via emergency_unwind_required if window "
                "closes with |pos|>0."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "r47_structural_properties",
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
                "session_date",
            ),
            complexity="O(1)",
            status=AlphaStatus.PROTOTYPE,
            tier=AlphaTier.TIER_1,
            rust_module=None,
            latency_profile="shioaji_sim_p95_v2026-03-04",
            roles_used=("architect", "code-reviewer"),
            skills_used=("hft-backtester",),
            feature_set_version=None,
            strategy_type="maker",
            instrument="TXFE6",
        )

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def maker(self) -> TxfRolloverBackFrontMaker:
        return self._maker

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._maker.reset()
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C68Alpha",
    "C68Params",
    "TxfRolloverBackFrontMaker",
    "is_in_rollover_window",
    "_DISABLED_SIGNAL_LAYERS",
    "_TXF_POINT_VALUE_NTD",
    "_TXF_INST_RT_COST_PTS",
    "_TXF_RETAIL_RT_COST_PTS",
    "_ROLLOVER_WINDOW_CANONICAL_DAYS",
]
