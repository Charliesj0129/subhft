"""C33 TXFD6 Solo Passive Maker — live-runtime BaseStrategy wrapper.

Post-PROMOTE scaffold (R7 T8, 2026-04-18). Wraps the research artifact at
``research.alphas.c33_txfd6_solo_passive_maker.impl.C33Alpha`` for the live
runtime without mutating the research-side implementation.

R47-minimal variant on TXFD6:
  - spread_threshold_pts = 5 (cost-gate; TXF RT 3 pt)
  - max_pos = 1 for the explicit exception-live rollout on THESHOW
  - inventory_skew_tenths = 2 (0.2 ticks per contract)
  - Signal layers (PE / Queue / MFG / QI) DISABLED — TMFD6-calibrated
    thresholds do NOT transfer (R7 T1 counterfactual: R47-full-QI
    underperforms R47-minimal 4:1 on TXFD6).

Exception-live defaults (enabled=true in strategies.yaml):
  - Live prices are scaled int **x10000** (platform convention). The
    research harness operated on **x1_000_000** CK prices. This wrapper
    does no unit translation on the price itself — it reads L1 from the
    runtime StrategyContext (which already serves x10000) and uses those
    integers directly.
  - Event dispatch uses ``on_stats(LOBStatsEvent)`` (hot path). The
    research ``on_tick(TickData)`` protocol is not replicated live.
  - ``queue_share`` is an informational parameter: the live
    broker decides fills, not a simulated queue model. It is retained to
    keep the strategies.yaml block echoable from the T5 scorecard config
    verbatim, reducing research-live parity drift.

R5+R6 physics carry:
  - TXF point_value = 200 NTD/pt (NOT 10 like TMFD6). Verified in
    research/alphas/c33_txfd6_solo_passive_maker/test_impl.py.
  - No cross-instrument hedge leg → R5 hedge-qty rule N/A.
  - No incremental cost beyond R47 TXFD6 base (3 pt RT per full cycle).
"""

from __future__ import annotations

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import RiskFeedback, Side
from hft_platform.core.timebase import now_ns as _now_ns
from hft_platform.events import BidAskEvent, GapEvent, LOBStatsEvent, TickEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("strategy.c33_txfd6_solo_maker")

_PRICE_SCALE = 10_000  # live runtime convention (CK is x1_000_000 — not used here)
_LOG_INTERVAL = 500


class C33TxfD6SoloMaker(BaseStrategy):
    """Live-runtime wrapper for C33 TXFD6 solo passive maker.

    Research artifact: research/alphas/c33_txfd6_solo_passive_maker/
    Parity: parameter names/defaults mirror ``C33Params`` from the research
    impl exactly except for the explicit live cap override
    (spread_threshold_pts=5, max_pos=1, inventory_skew_tenths=2,
    signal-layer flags all False).
    """

    __slots__ = (
        # parameters
        "_max_pos",
        "_spread_threshold_pts",
        "_inventory_skew_tenths",
        "_shadow_mode",
        "_queue_share_info",
        "_variant_label",
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

    def __init__(
        self,
        strategy_id: str = "c33_txfd6_solo_maker",
        **kwargs: object,
    ) -> None:
        super().__init__(strategy_id, **kwargs)

        def _as_int(value: object, default: int) -> int:
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float, str)):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return default
            return default

        def _as_float(value: object, default: float) -> float:
            if isinstance(value, (int, float, str)):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return default
            return default

        self._max_pos: int = _as_int(kwargs.get("max_pos", 1), 1)
        self._spread_threshold_pts: int = _as_int(kwargs.get("spread_threshold_pts", 5), 5)
        self._inventory_skew_tenths: int = _as_int(kwargs.get("inventory_skew_tenths", 2), 2)
        self._shadow_mode: bool = bool(kwargs.get("shadow_mode", False))
        # queue_share is informational (live broker decides fills); kept for
        # research-live parity of the strategies.yaml block.
        self._queue_share_info: float = _as_float(kwargs.get("queue_share", 0.05), 0.05)
        variant = kwargs.get("variant", "R47-minimal")
        self._variant_label: str = str(variant) if variant is not None else "R47-minimal"

        syms_raw = kwargs.get("subscribe_symbols") or kwargs.get("symbols") or []
        if isinstance(syms_raw, str):
            syms_list: list[str] = [syms_raw]
        elif isinstance(syms_raw, (list, tuple, frozenset, set)):
            syms_list = [str(s) for s in syms_raw]
        else:
            syms_list = []
        self._symbols_set: frozenset[str] = frozenset(syms_list)

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
            "c33_initialized",
            strategy_id=strategy_id,
            max_pos=self._max_pos,
            spread_threshold_pts=self._spread_threshold_pts,
            inventory_skew_tenths=self._inventory_skew_tenths,
            shadow_mode=self._shadow_mode,
            queue_share_info=self._queue_share_info,
            variant=self._variant_label,
            symbols=sorted(self._symbols_set),
        )

    # ---- Hot-path event handlers ------------------------------------------

    def on_tick(self, event: TickEvent) -> None:  # pragma: no cover
        # Trade events are not drivers in R47-minimal (all signal layers off).
        return

    def on_book_update(self, event: BidAskEvent) -> None:  # pragma: no cover
        # LOBStatsEvent is richer and arrives after BidAskEvent; all decision
        # logic is in on_stats().
        return

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Main quoting decision — on every LOB stats update."""
        symbol = event.symbol
        self._stats_count += 1

        if (
            event.mid_price_x2 is None
            or event.spread_scaled is None
            or event.mid_price_x2 <= 0
            or event.spread_scaled <= 0
            or event.best_bid <= 0
            or event.best_ask <= 0
        ):
            return

        # L1 spread gate (cost viability). scaled-int units, x10000.
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

        bid_moved = bid_quote != self._last_bid.get(symbol, -1)
        ask_moved = ask_quote != self._last_ask.get(symbol, -1)

        # Shadow-mode: still emit OrderIntents — the system routes them
        # through HFT_ORDER_SHADOW_MODE=1 gate, which suppresses dispatch at
        # the broker boundary. This exercises the execution-path code paths
        # end-to-end during shadow.

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

        if self._stats_count % _LOG_INTERVAL == 1:
            logger.info(
                "c33_stats",
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
            self._pending_buy[symbol] = max(0, self._pending_buy.get(symbol, 0) - event.qty)
            self._last_bid.pop(symbol, None)
        elif event.side == Side.SELL:
            self._local_pos[symbol] = self._local_pos.get(symbol, 0) - event.qty
            self._pending_sell[symbol] = max(0, self._pending_sell.get(symbol, 0) - event.qty)
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
        """Clear all transient state on bus overflow."""
        logger.warning(
            "c33_gap_reset",
            missed_events=getattr(event, "missed_count", 0),
        )
        self._pending_buy.clear()
        self._pending_sell.clear()
        self._last_bid.clear()
        self._last_ask.clear()
