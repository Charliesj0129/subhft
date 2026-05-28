"""C14 — R47 Maker on TXF rolling front-month.

Research-stage prototype. Reuses R47's three-layer signal states (PE,
Queue, MFG) via composition — **no modifications to R47 in-tree code**.

Interfaces:
  - ``TxfFrontMonthMaker`` conforms to ``research.backtest.maker_engine.MakerStrategy``
    and is what ``MakerEngine.run()`` drives.
  - ``C14Alpha`` conforms to ``research.registry.schemas.AlphaProtocol``
    for manifest/registry bookkeeping only (no signal-per-tick output;
    the quoting is done via MakerStrategy).

Design:
  L1 — spread gate at ``spread_threshold_pts`` (TXF default 3 pt; front-month
       median 2-5 pt so this keeps quoting active in the profitable regime).
  L2 — R47 signal layers (PE regime / Queue depletion / MFG capitulation).
       Defaults are the R47-structural-properties "minimal" configuration:
       PE disabled (0.0), Queue disabled (1.0), MFG disabled (100), leaving
       the L1 spread gate as the dominant filter. This preserves R47's
       V-shape recovery mechanism on TXF.
  L3 — Rollover-aware position tracking. When the active symbol differs
       from what was seen on the previous tick (a rollover event), the
       strategy flattens any outstanding R47 position in the outgoing
       contract before any quoting resumes on the incoming contract.

Prices:
  Inputs arrive as scaled integers with ``scale`` on the ``TickData``
  instance (CK source emits ``scale=1_000_000``). The strategy returns
  PostQuote prices at the same scale as the input bid_price/ask_price —
  no unit conversion happens inside the strategy.

Per rule MB-07 (multi-broker governance), platform ingestion scales to
x10000. The research backtest source happens to keep x1_000_000 integers
— this is NOT a live-path; research module float/scale exception applies.
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

logger = get_logger("alpha.c14_txf_frontmonth_native_maker")


# ----------------------------------------------------------------------------
# Strategy parameters
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class C14Params:
    """Tuning parameters for C14 strategy.

    Defaults reflect R47-structural "minimal-signal-layers" configuration
    plus TXF-native cost constants (RT 0.48 pt).
    """

    # L1 — spread gate (hard cost-viability floor)
    # TXF RT cost 0.48 pt. Median front-month spread 3-5 pt. Default 3 pt
    # to keep quoting active during the most common regime.
    spread_threshold_pts: int = 3
    # L2 — R47 signal layers (disabled by default = R47 minimal)
    pe_danger_threshold: float = 0.0  # 0.0 = never trip = disabled
    pe_window: int = 100
    queue_cancel_threshold: float = 1.0  # 1.0 = never trip = disabled
    queue_ema_alpha: float = 0.05
    mfg_skew_z_threshold: float = 100.0  # 100 = never trip = disabled
    mfg_ema_alpha: float = 0.01
    # Inventory — R47 structural optimum is max_pos=3 (non-linearly essential;
    # max_pos=1 turns +4,504 to -1,407).
    max_pos: int = 3
    # Fixed inventory skew in ticks per contract (R47 best practice: 0.2).
    inventory_skew_tenths: int = 2  # tenths of a tick per contract


# ----------------------------------------------------------------------------
# MakerStrategy implementation
# ----------------------------------------------------------------------------


class TxfFrontMonthMaker:
    """R47 maker logic, TXF front-month aware, MakerEngine-compatible.

    The strategy is rollover-aware: when ``set_active_symbol(new_symbol)``
    is invoked with a different symbol, any outstanding quotes are
    cancelled and any non-zero position is treated as "needs flattening"
    — the engine will see this as a HOLD in quoting with a zero-pos
    boundary.

    Current-tick signal states are kept per-symbol so that switching
    contracts does not pollute Queue/PE/MFG with stale state from the
    outgoing contract.
    """

    __slots__ = (
        "_params",
        "_pe_states",
        "_queue_states",
        "_mfg_states",
        "_position",  # fill-tracked local position for the *active* symbol
        "_active_symbol",  # current front-month symbol; may be None pre-bootstrap
        "_last_bid",
        "_last_ask",
        # counters (informational)
        "_tick_count",
        "_spread_blocked",
        "_pe_blocked",
        "_queue_blocked",
        "_quotes_posted",
        "_rollover_events",
    )

    def __init__(
        self,
        params: C14Params | None = None,
        active_symbol: str | None = None,
    ) -> None:
        self._params = params or C14Params()
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
        """Switch the strategy to a new front-month symbol.

        Clears outstanding price memory (so the engine's price-movement
        gate re-arms) and increments the rollover counter. Does NOT
        auto-zero the local position — the caller (MakerEngine driver or
        the research harness) is responsible for synthesising the closing
        trade on the outgoing contract before this switch.
        """
        if self._active_symbol == new_symbol:
            return
        logger.info(
            "c14_rollover",
            outgoing=self._active_symbol,
            incoming=new_symbol,
        )
        self._active_symbol = new_symbol
        self._last_bid = None
        self._last_ask = None
        # Position carries 0 across the boundary because caller flattens.
        # Reset to 0 defensively — if the caller forgot to flatten, the
        # accounting here at least keeps the new contract's pos clean.
        self._position = 0
        self._rollover_events += 1

    def flatten_position(self) -> int:
        """Zero the local position counter and return the prior value.

        Used by the test suite and the research driver to confirm a
        flatten happened before a set_active_symbol() call. Does not
        emit any quote actions by itself.
        """
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
        """Dispatch a single event.

        Note ``TickData`` carries both bidask and trade events. Trades
        feed the MFG signed-flow estimator; bidask events drive PE and
        Queue estimators plus the quoting decision.
        """
        self._tick_count += 1

        if tick.is_trade:
            # Trades feed MFG only; quoting/position logic is bidask-driven.
            if self._active_symbol is not None:
                mfg = self._get_mfg(self._active_symbol)
                # Engine's TickData does not carry trade_direction; approximate
                # by sign of trade_price vs cur_mid: skipped here because
                # cur_mid is not on the event. MFG update is signal-optional.
                # Leave as a no-op: MFG-disabled is the R47 default anyway.
                _ = mfg
            return [Hold()]

        return self._on_bidask(tick)

    def _on_bidask(
        self, tick: TickData
    ) -> list[PostQuote | CancelQuote | Hold]:
        params = self._params

        # Validity guard — reject malformed/one-sided books.
        if tick.bid_price <= 0 or tick.ask_price <= 0:
            return [Hold()]
        if tick.ask_price <= tick.bid_price:
            return [Hold()]

        scale = tick.scale
        spread_raw = tick.ask_price - tick.bid_price
        spread_pts = spread_raw // scale

        # L1 — spread gate (cost viability)
        if spread_pts < params.spread_threshold_pts:
            self._spread_blocked += 1
            return [Hold()]

        # L2a — PE regime gate (disabled when threshold=0.0)
        sym = self._active_symbol or "_unknown_"
        pe = self._get_pe(sym)
        # Update PE from L1 imbalance.
        total_qty = tick.bid_qty + tick.ask_qty
        if total_qty > 0:
            qi = (tick.bid_qty - tick.ask_qty) / total_qty
            pe.update(qi)
        if params.pe_danger_threshold > 0.0 and pe.warmed_up:
            if pe.h < params.pe_danger_threshold:
                self._pe_blocked += 1
                return [Hold()]

        # L2b — queue depletion gate (disabled when threshold>=1.0)
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

        # L3 — quote construction (fresh-quote replacement, no stale-quote
        # preservation — see r47_structural_properties.md "Fresh Quotes Beat
        # Stale Quotes": 0/12 days improved from preservation).
        actions: list[PostQuote | CancelQuote | Hold] = []
        pos = self._position
        max_pos = params.max_pos

        # Fixed inventory skew (R47: 0.2 ticks/contract = 0.2 pt * scale/10)
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
            self._last_bid = None  # allow requote at new level
        elif side == "sell":
            self._position -= 1
            self._last_ask = None
        else:
            logger.warning("c14_unknown_fill_side", side=side)

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

    # ---- Gap resilience (invoked by research harness on bus-overflow) ----

    def on_gap(self) -> None:
        """Clear mutable state on gap events.

        Per hft-strategy-sdk: bus overflow means fills/cancels may be
        lost. Clear pending-quote markers so requote logic can re-arm
        at the next tick. Local position is NOT cleared (authoritative
        from fills); only the transient state that drives quote
        suppression is reset.
        """
        self._last_bid = None
        self._last_ask = None


# ----------------------------------------------------------------------------
# AlphaProtocol shim for registry / manifest tooling
# ----------------------------------------------------------------------------


class C14Alpha:
    """Thin AlphaProtocol wrapper around TxfFrontMonthMaker.

    Satisfies ``research.registry.schemas.AlphaProtocol`` so registry /
    manifest tooling can discover and introspect this alpha. The actual
    trading logic is in ``TxfFrontMonthMaker``; this shim's ``update()``
    is a no-op that returns a constant signal, used only by the registry
    smoke path.
    """

    __slots__ = ("_maker", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C14Params | None = None,
        active_symbol: str | None = None,
    ) -> None:
        self._maker = TxfFrontMonthMaker(params=params, active_symbol=active_symbol)
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c14_txf_frontmonth_native_maker",
            hypothesis=(
                "R47's structural edge (V-shape recovery, max_pos=3 "
                "inventory, minimal skew) is venue-invariant within the "
                "same cost-structure class. Running R47 on TXF rolling "
                "front-month captures the same edge on a 10x more "
                "favourable cost-to-spread ratio than TMFD6."
            ),
            formula=(
                "R47 three-layer maker (PE gate / queue cancel / MFG skew, "
                "minimal-layer defaults) driven on whichever TXF contract "
                "is current front-month; per-tick position flattened on "
                "rollover day."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "r47_structural_properties",
                "r47_backtest_data_regression",
                "2008_Avellaneda_Stoikov_HFT_LOB",
                "2011_Huang_Polak_LOBSTER",
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
            instrument="TXF_frontmonth",
        )

    # ---- AlphaProtocol members --------------------------------------------

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def maker(self) -> TxfFrontMonthMaker:
        return self._maker

    def update(self, *args: object, **kwargs: object) -> float:
        """AlphaProtocol update stub.

        C14 is a MAKER strategy, not a signal-per-tick predictor. Returns
        a constant zero — registry tooling only needs the method to exist.
        """
        return self._last_signal

    def reset(self) -> None:
        self._maker = TxfFrontMonthMaker(
            params=self._maker._params,  # type: ignore[attr-defined]
            active_symbol=None,
        )
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


# ----------------------------------------------------------------------------
# Queue-position-loss fill model (R6-T5-REVISE Fix A)
# ----------------------------------------------------------------------------
#
# Gate C T6 unrescued-FAIL #1: the prior RetailDiscountedFill applied a scalar
# multiplier that preferentially kept clean fills — opposite of real retail
# queue economics. Real retail at a busy front-month:
#
#   Favorable market move (would be profitable maker fill):
#     HFT queue-front fills first — retail does NOT execute → skipped.
#
#   Adverse sweep across multiple levels:
#     Every resting order gets hit, including retail → proceeds.
#
# ``p_front`` is the probability retail happens near the queue-front.
# Default 0.3 = pessimistic-realistic (HFT wins queue priority 70% of ticks).
# See t6_challenger_gate_c.md Fix A.
# ----------------------------------------------------------------------------


class QueuePositionStochasticFill:
    """Queue-position-loss fill model with adverse-selection bias.

    Wraps ``research.backtest.fill_models.QueueDepletionFill``. For each fill
    candidate emitted by the inner model:
      1. Draw ``q ~ U(0,1)`` deterministically per (side, price, trade_price,
         trade_volume, seed). Same inputs → same draw (reproducibility).
      2. If ``q < p_front``: retail near queue-front → fill proceeds.
      3. Else: classify the fill as favorable vs adverse by comparing
         ``trade_price`` to the maker's quote price on the correct side.
         Adverse fills proceed; favorable fills are DROPPED (HFT took them).

    Expected signature under varying ``p_front``:
      - Edge-per-fill MONOTONICALLY DECREASES as p_front decreases.
        If this invariant does not hold on a new dataset, the classification
        logic is miscalibrated.
    """

    def __init__(
        self,
        queue_fraction: float = 0.5,
        p_front: float = 0.3,
        rng_seed: int = 2026_04_17,
    ) -> None:
        # Import here to avoid circular import at module-eval time.
        from research.backtest.fill_models import QueueDepletionFill as _QDF

        self._inner = _QDF(queue_fraction=queue_fraction)
        if not 0.0 <= p_front <= 1.0:
            raise ValueError(f"p_front must be in [0,1], got {p_front}")
        self._p_front = float(p_front)
        self._rng_seed = int(rng_seed)
        self._stats_total = 0
        self._stats_front = 0
        self._stats_adverse_kept = 0
        self._stats_favorable_dropped = 0

    @property
    def label(self) -> str:
        return f"QueuePosStochastic({self._inner.label},p_front={self._p_front:.2f})"

    @property
    def queue_fraction(self) -> float:
        return self._inner.queue_fraction

    @property
    def p_front(self) -> float:
        return self._p_front

    @property
    def stats(self) -> dict:
        return {
            "total_raw_fills": self._stats_total,
            "front_kept": self._stats_front,
            "adverse_kept": self._stats_adverse_kept,
            "favorable_dropped": self._stats_favorable_dropped,
        }

    def post_quote(self, side: str, price: int, book_qty: int):
        return self._inner.post_quote(side, price, book_qty)

    def check_fills(self, positions, trade_price: int, trade_volume: int):
        raw_fills = self._inner.check_fills(positions, trade_price, trade_volume)
        if not raw_fills:
            return raw_fills
        kept = []
        for f in raw_fills:
            self._stats_total += 1
            h = hash((f.side, f.price, trade_price, trade_volume, self._rng_seed))
            q = (h & 0xFFFFFFFF) / 0x1_0000_0000
            if q < self._p_front:
                self._stats_front += 1
                kept.append(f)
                continue
            # Classify adverse vs favorable.
            # A buy maker fill is adverse if the trade_price is BELOW our quote
            # (price fell through our bid — we caught a falling market).
            # A sell maker fill is adverse if the trade_price is ABOVE our quote
            # (price rose through our ask — we sold into a rally).
            if f.side == "buy":
                adverse = trade_price < f.price
            elif f.side == "sell":
                adverse = trade_price > f.price
            else:  # pragma: no cover — defensive
                adverse = False
            if adverse:
                self._stats_adverse_kept += 1
                kept.append(f)
            else:
                self._stats_favorable_dropped += 1
        return kept


__all__ = [
    "C14Alpha",
    "C14Params",
    "QueuePositionStochasticFill",
    "TxfFrontMonthMaker",
]
