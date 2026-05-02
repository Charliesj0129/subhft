"""C30 — TXFD6 passive maker with TMFD6 delta hedge (cross-instrument MM pair).

Mechanism:
  - Quote TXFD6 passively using an R47-style spread gate (default
    spread_threshold_pts=5, max_pos in TXF contracts = hedge_inv_trigger_pts / 1).
  - Accumulate TXF inventory via passive fills.
  - When |TXF_inventory_pts| >= ``hedge_inv_trigger_pts`` (default 20 pts of
    TXF notional), take one offsetting TMFD6 leg at the far-side top-of-book
    to neutralize delta. 1 TXF = 20 TMF by notional (200 NTD/pt vs 10 NTD/pt).
  - No directional signal on either leg — H3 differentiator vs the killed
    TX-TMF leadlag class (R26/R28).

Interfaces:
  - ``TxfTmfPairMaker`` is the pair-aware strategy object. It is *not* a
    drop-in ``research.backtest.maker_engine.MakerStrategy`` because the
    MakerEngine targets a single instrument; a dedicated pair-aware backtest
    harness (T5) drives both streams and routes events through the
    ``on_txf_tick`` / ``on_tmf_tick`` methods.
  - ``C30Alpha`` conforms to ``research.registry.schemas.AlphaProtocol``.

Cost model (cited from memory/feedback_taifex_fee_structure.md, user-confirmed
2026-04-18):
  - TXF RT = 3.0 pt retail (maker leg amortized as 1.5 pt per entry fill).
  - TMF RT = 4.0 pt retail (full RT per hedge taker event).

Research-module float exception (Rule 11 of 25-architecture-governance): this
file is offline / CLI-invoked research code. Prices arriving from CK use the
scaled-integer convention (``scale`` field on ``TickData``; default 1e6 for
CK, 1e4 in the live platform). All price arithmetic inside the strategy
operates on these scaled integers; floats appear only in cycle statistics and
MTM reporting.

Precision Law (CLAUDE.md #4): scaled-integer math for all live-path decisions.
Timestamps are treated as monotonic ns supplied by the caller; the strategy
never calls ``datetime.now()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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

logger = get_logger("alpha.c30_txf_maker_tmf_hedge_pair")


# ----------------------------------------------------------------------------
# Parameters
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class C30Params:
    """Tuning parameters for the TXF-maker + TMF-hedge pair.

    Attributes
    ----------
    spread_threshold_pts
        Minimum TXFD6 spread (in points) to quote. Default 5 is consistent
        with the R47 convention; DA T2 WARN #1 forbids widening below this
        without fresh OOS TXFD6 regime evidence.
    txf_max_pos_contracts
        TXF contract cap before the maker suppresses the adverse side.
        Default 20 is set to equal the default hedge trigger so that
        inventory accumulation is not cut off prematurely by the max_pos
        gate; the hedge mechanism is the binding risk control.
    hedge_inv_trigger_pts
        Absolute TXF inventory **in TXF contracts** at which a TMF hedge
        is issued. Despite the "_pts" suffix (historical naming), the unit
        is TXF contracts — 1 TXF = 1 unit. Default 20 matches the
        Researcher's T1 counterfactual and the DA APPROVE basis; T5
        brackets [10, 20, 40] per DA WARN #5.
    tmf_hedge_ratio
        Number of TMF contracts per TXF contract by notional. 200/10 = 20.
    assumed_queue_share_pct
        Upper-bound fraction of best-price trades the strategy is assumed
        to catch (1 = 1%). Kept as state for downstream backtest reporting
        per DA WARN #3; the strategy itself does not filter fills here.
    inventory_skew_tenths
        Fixed inventory skew on TXF quotes: 0.1 tick per contract per unit.
        Default 2 = 0.2 ticks/contract (R47 best practice).
    tmf_taker_slippage_pts
        Conservative slippage estimate added to every TMF hedge execution.
        Default 0 means "trust the far-side top-of-book"; the backtest can
        override this per DA WARN #4 (hedge slippage modelling).
    """

    spread_threshold_pts: int = 5
    txf_max_pos_contracts: int = 20
    hedge_inv_trigger_pts: int = 20
    tmf_hedge_ratio: int = 20
    assumed_queue_share_pct: float = 2.0
    inventory_skew_tenths: int = 2
    tmf_taker_slippage_pts: int = 0


# ----------------------------------------------------------------------------
# Hedge action — emitted by TXF leg when inventory crosses trigger
# ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HedgeOrder:
    """Delta-hedge taker order on TMFD6.

    ``side`` follows the same convention as ``PostQuote``: "buy" means the
    strategy lifts the TMF ask (to short the TXF-long), "sell" means the
    strategy hits the TMF bid (to long the TXF-short).

    ``qty`` is the number of TMF contracts (positive integer).

    ``trigger_txf_pos_pts`` reports the absolute TXF inventory (in pts) at
    which this hedge was triggered; used for backtest partition analysis.
    """

    side: str
    qty: int
    trigger_txf_pos_pts: int


# ----------------------------------------------------------------------------
# TXF maker leg — minimal R47-like implementation (spread gate + skew +
# price-movement gate). No feature dependencies; feature-layer state from
# R47 (_PEState / _QueueState / _MFGState) is intentionally omitted so C30
# parks at "R47 minimal" defaults (all layers off) per DA T2 H3 ruling that
# C30's edge source is spread capture, not layered signals.
# ----------------------------------------------------------------------------


class _TxfMakerLeg:
    """Passive R47-minimal maker on TXFD6 with max_pos cap."""

    __slots__ = (
        "_params",
        "_position",
        "_last_bid",
        "_last_ask",
        "_tick_count",
        "_spread_blocked",
        "_quotes_posted",
    )

    def __init__(self, params: C30Params) -> None:
        self._params = params
        self._position = 0
        self._last_bid: int | None = None
        self._last_ask: int | None = None
        self._tick_count = 0
        self._spread_blocked = 0
        self._quotes_posted = 0

    @property
    def position(self) -> int:
        return self._position

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

        actions: list[PostQuote | CancelQuote | Hold] = []
        pos = self._position
        max_pos = params.txf_max_pos_contracts

        # Inventory skew (scaled-integer, R47 best practice)
        skew = (pos * params.inventory_skew_tenths * scale) // 10
        bid_quote = tick.bid_price - skew
        ask_quote = tick.ask_price - skew

        bid_moved = bid_quote != self._last_bid
        ask_moved = ask_quote != self._last_ask

        if pos < max_pos and bid_moved:
            actions.append(PostQuote(side="buy", price=bid_quote, qty=1))
            self._last_bid = bid_quote
            self._quotes_posted += 1
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
            logger.warning("c30_txf_unknown_fill_side", side=side)

    def apply_hedge_offset(self, delta_contracts: int) -> None:
        """Apply a TXF-side inventory reset after a TMF hedge pushes the
        hedge-equivalent back into delta-neutral territory.

        In the pair mechanism, a TMF hedge neutralizes TXF delta but does
        NOT change the TXF position itself (TXF contracts are still held).
        This method intentionally does nothing; it exists so the pair
        orchestrator can record the hedge event without touching private
        state. Kept for symmetry with a future netting variant.
        """
        del delta_contracts  # no-op: TMF hedge offsets delta, not TXF pos

    def on_gap(self) -> None:
        self._last_bid = None
        self._last_ask = None


# ----------------------------------------------------------------------------
# TMF hedge leg — taker on inventory threshold. Uses top-of-book + a
# configurable slippage constant for realistic T5 modelling.
# ----------------------------------------------------------------------------


class _TmfHedgeLeg:
    """Delta hedge taker on TMFD6 driven by TXF inventory threshold."""

    __slots__ = (
        "_params",
        "_position",
        "_last_bid",
        "_last_ask",
        "_hedge_events",
    )

    def __init__(self, params: C30Params) -> None:
        self._params = params
        self._position = 0
        self._last_bid: int = 0
        self._last_ask: int = 0
        self._hedge_events = 0

    @property
    def position(self) -> int:
        return self._position

    @property
    def hedge_events(self) -> int:
        return self._hedge_events

    def on_tmf_book(self, tick: TickData) -> None:
        if tick.is_trade:
            return
        if tick.bid_price > 0:
            self._last_bid = tick.bid_price
        if tick.ask_price > 0:
            self._last_ask = tick.ask_price

    def compute_hedge(
        self, txf_position_contracts: int, txf_point_value_ntd: int
    ) -> HedgeOrder | None:
        """If TXF inventory crosses trigger, emit a HedgeOrder.

        ``hedge_inv_trigger_pts`` is interpreted as TXF contracts (despite
        the legacy "_pts" suffix in the name; see C30Params docstring).
        Trigger fires when ``|txf_position_contracts| >= hedge_inv_trigger_pts``.

        The hedge direction is opposite the TXF delta:
          txf_pos > 0 (long) → sell TMF
          txf_pos < 0 (short) → buy TMF

        Returns None if the threshold is not crossed.
        """
        del txf_point_value_ntd  # retained for backwards-compat of the signature
        if txf_position_contracts == 0:
            return None
        params = self._params
        abs_pos_contracts = abs(txf_position_contracts)
        if abs_pos_contracts < params.hedge_inv_trigger_pts:
            return None
        qty = abs_pos_contracts * params.tmf_hedge_ratio
        if txf_position_contracts > 0:
            side = "sell"
        else:
            side = "buy"
        return HedgeOrder(
            side=side, qty=qty, trigger_txf_pos_pts=abs_pos_contracts
        )

    def on_hedge_fill(
        self, side: str, fill_price: int, qty: int
    ) -> int:
        """Update TMF position after a hedge executed. Returns signed qty
        applied to TMF position (positive for buy, negative for sell)."""
        if side == "buy":
            delta = qty
        elif side == "sell":
            delta = -qty
        else:
            logger.warning("c30_tmf_unknown_hedge_side", side=side)
            return 0
        self._position += delta
        self._hedge_events += 1
        return delta

    def hedge_execution_price(self, side: str) -> int:
        """Return the scaled-integer execution price for a hedge taker
        order, including the configured slippage constant. The strategy
        assumes the hedge lifts the ask (for buy) or hits the bid (for
        sell); slippage widens adversely.
        """
        params = self._params
        scale = 1_000_000  # CK scale; backtest engine can adjust
        slip = params.tmf_taker_slippage_pts * scale
        if side == "buy":
            return self._last_ask + slip if self._last_ask > 0 else 0
        if side == "sell":
            return self._last_bid - slip if self._last_bid > 0 else 0
        return 0

    def on_gap(self) -> None:
        self._last_bid = 0
        self._last_ask = 0


# ----------------------------------------------------------------------------
# Pair orchestrator — drives TXF maker + TMF hedge together
# ----------------------------------------------------------------------------


@dataclass(slots=True)
class PairStepResult:
    """Result of a single pair-step (TXF or TMF tick).

    ``maker_actions`` contains quote placements for the TXF leg. ``hedge``
    is populated only when a hedge is emitted on this step. The caller
    (backtest engine) is responsible for filling ``hedge`` at TMF far-side
    top-of-book before the next step.
    """

    maker_actions: list[PostQuote | CancelQuote | Hold] = field(default_factory=list)
    hedge: HedgeOrder | None = None


class TxfTmfPairMaker:
    """Pair-aware orchestrator for TXF-maker + TMF-hedge.

    Drives two single-leg state machines and issues hedges when the TXF
    inventory crosses the configured trigger. The harness that wraps this
    class is responsible for:

      1. Feeding TXF bid/ask/trade events via ``on_txf_tick``.
      2. Feeding TMF bid/ask events via ``on_tmf_tick``.
      3. Filling returned ``PostQuote`` actions under its chosen queue model.
      4. Filling any returned ``HedgeOrder`` at the TMF far-side TOB.
      5. Calling ``on_txf_fill`` and ``on_tmf_fill`` with the fill events.

    This class carries NO broker or CK I/O. It is pure, deterministic,
    time-monotonic behaviour keyed off the ``exch_ts`` of inputs.
    """

    __slots__ = (
        "_params",
        "_txf_leg",
        "_tmf_leg",
        "_txf_symbol",
        "_tmf_symbol",
        "_txf_point_value_ntd",
    )

    def __init__(
        self,
        params: C30Params | None = None,
        txf_symbol: str = "TXFD6",
        tmf_symbol: str = "TMFD6",
        txf_point_value_ntd: int = 200,
    ) -> None:
        self._params = params or C30Params()
        self._txf_leg = _TxfMakerLeg(self._params)
        self._tmf_leg = _TmfHedgeLeg(self._params)
        self._txf_symbol = txf_symbol
        self._tmf_symbol = tmf_symbol
        self._txf_point_value_ntd = txf_point_value_ntd

    # ---- Event entry points ------------------------------------------------

    def on_txf_tick(self, tick: TickData) -> PairStepResult:
        actions = self._txf_leg.on_tick(tick)
        hedge = self._tmf_leg.compute_hedge(
            self._txf_leg.position, self._txf_point_value_ntd
        )
        return PairStepResult(maker_actions=actions, hedge=hedge)

    def on_tmf_tick(self, tick: TickData) -> PairStepResult:
        self._tmf_leg.on_tmf_book(tick)
        # Recheck hedge trigger on TMF tick too — inventory may have been
        # accumulated since last TXF event but hedge execution price lives
        # on the TMF stream.
        hedge = self._tmf_leg.compute_hedge(
            self._txf_leg.position, self._txf_point_value_ntd
        )
        return PairStepResult(maker_actions=[], hedge=hedge)

    # ---- Fill callbacks ----------------------------------------------------

    def on_txf_fill(self, side: str, price: int, mid_price: float) -> None:
        self._txf_leg.on_fill(side, price, mid_price)

    def on_tmf_fill(
        self, side: str, fill_price: int, qty: int
    ) -> int:
        return self._tmf_leg.on_hedge_fill(side, fill_price, qty)

    def hedge_execution_price(self, side: str) -> int:
        return self._tmf_leg.hedge_execution_price(side)

    # ---- Observability -----------------------------------------------------

    @property
    def txf_position(self) -> int:
        return self._txf_leg.position

    @property
    def tmf_position(self) -> int:
        return self._tmf_leg.position

    @property
    def hedge_events(self) -> int:
        return self._tmf_leg.hedge_events

    @property
    def txf_symbol(self) -> str:
        return self._txf_symbol

    @property
    def tmf_symbol(self) -> str:
        return self._tmf_symbol

    @property
    def params(self) -> C30Params:
        return self._params

    def on_gap(self) -> None:
        self._txf_leg.on_gap()
        self._tmf_leg.on_gap()

    def reset(self) -> None:
        self._txf_leg = _TxfMakerLeg(self._params)
        self._tmf_leg = _TmfHedgeLeg(self._params)


# ----------------------------------------------------------------------------
# AlphaProtocol shim (registry smoke path)
# ----------------------------------------------------------------------------


class C30Alpha:
    """AlphaProtocol wrapper around TxfTmfPairMaker."""

    __slots__ = ("_maker", "_manifest", "_last_signal")

    def __init__(
        self,
        params: C30Params | None = None,
        txf_symbol: str = "TXFD6",
        tmf_symbol: str = "TMFD6",
    ) -> None:
        self._maker = TxfTmfPairMaker(
            params=params,
            txf_symbol=txf_symbol,
            tmf_symbol=tmf_symbol,
        )
        self._last_signal = 0.0
        self._manifest = AlphaManifest(
            alpha_id="c30_txf_maker_tmf_hedge_pair",
            hypothesis=(
                "TXFD6 passive R47-minimal maker earns half-spread on a 4-pt "
                "median-spread regime at RT 3 pt; accumulated TXF delta is "
                "neutralized via a TMFD6 taker hedge at the 20-pt inventory "
                "trigger (1 TXF = 20 TMF by notional, 200 NTD/pt vs 10 NTD/pt). "
                "Mechanism is operationally distinct from TX-TMF leadlag "
                "(R26/R28 KILL) because no TXF->TMF directional prediction is "
                "used; edge source is TXF half-spread capture minus drift "
                "minus TMF hedge cost."
            ),
            formula=(
                "TxfMaker(spread>=5 pts, max_pos=3) + HedgeIfCrossed("
                "|TXF_inv| >= 20 pts, side = -sign(TXF_inv), qty = "
                "|TXF_contracts| * 20 TMF)."
            ),
            paper_refs=(
                "r47_maker_strategy",
                "r47_structural_properties",
                "feedback_taifex_fee_structure",
                "memory/backtest_method_reliability",
                "c14_txf_frontmonth_native_maker",
                "2015_Cartea_Jaimungal_optimal_execution",
                "2008_Avellaneda_Stoikov_HFT_LOB",
            ),
            data_fields=(
                "txf_bid_px",
                "txf_ask_px",
                "txf_bid_qty",
                "txf_ask_qty",
                "txf_trade_price",
                "txf_trade_volume",
                "tmf_bid_px",
                "tmf_ask_px",
                "tmf_bid_qty",
                "tmf_ask_qty",
                "mid_price",
                "spread_pts",
            ),
            complexity="O(1) per tick",
            status=AlphaStatus.PROTOTYPE,
            tier=AlphaTier.TIER_1,
            rust_module=None,
            latency_profile="shioaji_sim_p95_v2026-03-04",
            roles_used=(),
            skills_used=("hft-backtester",),
            feature_set_version=None,
            strategy_type="maker",
            instrument="TXFD6+TMFD6",
        )

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    @property
    def maker(self) -> TxfTmfPairMaker:
        return self._maker

    def update(self, *args: object, **kwargs: object) -> float:
        return self._last_signal

    def reset(self) -> None:
        self._maker.reset()
        self._last_signal = 0.0

    def get_signal(self) -> float:
        return self._last_signal


__all__ = [
    "C30Alpha",
    "C30Params",
    "HedgeOrder",
    "PairStepResult",
    "TxfTmfPairMaker",
]
