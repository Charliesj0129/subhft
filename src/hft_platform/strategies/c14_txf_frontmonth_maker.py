"""C14 TXF Front-Month Maker — live-runtime BaseStrategy wrapper.

Post-PROMOTE scaffold (R6 T8, 2026-04-17). Wraps
``research.alphas.c14_txf_frontmonth_native_maker.impl.TxfFrontMonthMaker``
for the live runtime without mutating the research-side implementation.

Key differences from the research artifact:
  - Live prices are scaled int **x10000** (platform convention). The research
    harness operated on **x1_000_000** CK prices. This wrapper does no unit
    translation on the price itself — the wrapper reads L1 from the runtime
    StrategyContext (which already serves x10000) and uses those integers
    directly. We do NOT import or run ``TxfFrontMonthMaker`` here; we
    re-implement the quote decision inline against the runtime event model
    (LOBStatsEvent / BidAskEvent) using the same parameters and semantics.
  - Event dispatch uses ``on_stats(LOBStatsEvent)`` (hot path), not the
    research ``on_tick(TickData)`` protocol.
  - Front-month rotation in production requires broker-side contract
    resolution (symbol aliases like ``TXFR1``). The strategy here is
    parametrised by an explicit symbol list and quotes on whichever symbol
    currently appears in incoming events. A proper production rotator that
    programmatically activates the front-month contract is listed as a
    post-shadow engineering item in ``RELEASE_GATE.md``.

Shadow-only defaults:
  - ``p_front_shadow_flag`` is recorded but has no effect on live quoting
    (fill model is not a live concept — the broker decides fills). It is
    kept here so the same ``C14Params`` block used in the research
    scorecard can be echoed into ``strategies.yaml`` verbatim, reducing
    research-live parity drift.
"""

from __future__ import annotations

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import RiskFeedback, Side
from hft_platform.core.timebase import now_ns as _now_ns
from hft_platform.events import BidAskEvent, GapEvent, LOBStatsEvent, TickEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("strategy.c14_txf_frontmonth_maker")

_PRICE_SCALE = 10_000  # live runtime convention
_LOG_INTERVAL = 500


class C14TxfFrontMonthMakerStrategy(BaseStrategy):
    """Live-runtime wrapper for C14 TXF front-month maker.

    Research artifact: research/alphas/c14_txf_frontmonth_native_maker/
    Parity: parameter names/defaults mirror ``C14Params`` exactly.
    """

    __slots__ = (
        # parameters
        "_max_pos",
        "_spread_threshold_pts",
        "_inventory_skew_tenths",
        "_shadow_mode",
        "_symbols_set",
        # runtime state
        "_local_pos",
        "_pending_buy",
        "_pending_sell",
        "_last_bid",
        "_last_ask",
        # counters
        "_stats_count",
        "_spread_blocked",
        "_quotes_posted",
        "_last_log_ts_ns",
    )

    def __init__(self, strategy_id: str = "c14_txf_frontmonth_maker", **kwargs) -> None:
        super().__init__(strategy_id, **kwargs)
        self._max_pos: int = int(kwargs.get("max_pos", 3))
        self._spread_threshold_pts: int = int(kwargs.get("spread_threshold_pts", 3))
        self._inventory_skew_tenths: int = int(kwargs.get("inventory_skew_tenths", 2))
        self._shadow_mode: bool = bool(kwargs.get("shadow_mode", True))
        syms = kwargs.get("subscribe_symbols") or []
        self._symbols_set: frozenset[str] = frozenset(syms)

        self._local_pos: dict[str, int] = {}
        self._pending_buy: dict[str, int] = {}
        self._pending_sell: dict[str, int] = {}
        self._last_bid: dict[str, int] = {}
        self._last_ask: dict[str, int] = {}

        self._stats_count = 0
        self._spread_blocked = 0
        self._quotes_posted = 0
        self._last_log_ts_ns: int = 0

        logger.info(
            "c14_initialized",
            strategy_id=strategy_id,
            max_pos=self._max_pos,
            spread_threshold_pts=self._spread_threshold_pts,
            shadow_mode=self._shadow_mode,
            symbols=sorted(self._symbols_set),
        )

    # ---- Hot-path event handlers ------------------------------------------

    def on_tick(self, event: TickEvent) -> None:  # pragma: no cover — trades aren't quote driver
        # Trade events are not needed for quoting in the minimal-layer
        # R47 configuration (all signal layers disabled). Present only so
        # the BaseStrategy dispatch contract is satisfied.
        return

    def on_book_update(self, event: BidAskEvent) -> None:  # pragma: no cover
        # LOBStatsEvent is richer and always arrives after BidAskEvent in
        # the canonical runtime pipeline; defer all decision logic to
        # on_stats() to match the research artifact's on_tick semantics.
        return

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Main quoting decision — on every LOB stats update."""
        symbol = event.symbol
        self._stats_count += 1

        # Basic validity guards — match research strategy defensive behaviour.
        if (
            event.mid_price_x2 is None
            or event.spread_scaled is None
            or event.mid_price_x2 <= 0
            or event.spread_scaled <= 0
            or event.best_bid <= 0
            or event.best_ask <= 0
        ):
            return

        # L1 spread gate (cost viability). Uses x10000 units.
        spread_threshold_scaled = self._spread_threshold_pts * _PRICE_SCALE
        if event.spread_scaled < spread_threshold_scaled:
            self._spread_blocked += 1
            return

        best_bid = event.best_bid
        best_ask = event.best_ask
        pos = self._local_pos.get(symbol, 0)
        pending_b = self._pending_buy.get(symbol, 0)
        pending_s = self._pending_sell.get(symbol, 0)

        # Fixed inventory skew (R47: 0.2 ticks/contract; 1 tick = _PRICE_SCALE)
        skew = (pos * self._inventory_skew_tenths * _PRICE_SCALE) // 10
        bid_quote = best_bid - skew
        ask_quote = best_ask - skew

        # Price-movement gate prevents ROD stack-up.
        bid_moved = bid_quote != self._last_bid.get(symbol, -1)
        ask_moved = ask_quote != self._last_ask.get(symbol, -1)

        # Shadow-mode: still emit OrderIntents — the system routes them
        # through HFT_ORDER_SHADOW_MODE=1 gate, which suppresses dispatch
        # at the broker boundary. We do NOT skip intent emission here
        # because the gate exists precisely so execution-path code paths
        # get exercised end-to-end during shadow.

        if bid_moved and (pos + pending_b) < self._max_pos:
            self.buy(symbol, bid_quote, 1)
            self._pending_buy[symbol] = pending_b + 1
            self._last_bid[symbol] = bid_quote
            self._quotes_posted += 1
        if ask_moved and (pos - pending_s) > -self._max_pos:
            self.sell(symbol, ask_quote, 1)
            self._pending_sell[symbol] = pending_s + 1
            self._last_ask[symbol] = ask_quote
            self._quotes_posted += 1

        # Periodic structured log (not per-tick).
        if self._stats_count % _LOG_INTERVAL == 1:
            logger.info(
                "c14_stats",
                symbol=symbol,
                pos=pos,
                pending_buy=pending_b,
                pending_sell=pending_s,
                spread_pts=event.spread_scaled // _PRICE_SCALE,
                quotes=self._quotes_posted,
                spread_blk=self._spread_blocked,
                now_ns=_now_ns(),
            )

    # ---- Fill / order / gap / risk feedback --------------------------------

    def on_fill(self, event: FillEvent) -> None:
        symbol = event.symbol
        if event.side == Side.BUY:
            self._local_pos[symbol] = self._local_pos.get(symbol, 0) + event.qty
            self._pending_buy[symbol] = max(
                0, self._pending_buy.get(symbol, 0) - event.qty
            )
            # Allow requote at the next bid level after a fill
            self._last_bid.pop(symbol, None)
        elif event.side == Side.SELL:
            self._local_pos[symbol] = self._local_pos.get(symbol, 0) - event.qty
            self._pending_sell[symbol] = max(
                0, self._pending_sell.get(symbol, 0) - event.qty
            )
            self._last_ask.pop(symbol, None)

    def on_risk_feedback(self, feedback: RiskFeedback) -> None:
        """Release pending counters on risk rejection (prevents deadlock)."""
        symbol = feedback.symbol
        if feedback.side == Side.BUY:
            self._pending_buy[symbol] = max(0, self._pending_buy.get(symbol, 0) - 1)
            self._last_bid.pop(symbol, None)
        elif feedback.side == Side.SELL:
            self._pending_sell[symbol] = max(0, self._pending_sell.get(symbol, 0) - 1)
            self._last_ask.pop(symbol, None)

    def on_gap(self, event: GapEvent) -> None:
        """Clear all transient state on bus overflow — fills/cancels may be lost."""
        logger.warning("c14_gap_reset", missed_events=getattr(event, "missed_count", 0))
        self._pending_buy.clear()
        self._pending_sell.clear()
        self._last_bid.clear()
        self._last_ask.clear()
