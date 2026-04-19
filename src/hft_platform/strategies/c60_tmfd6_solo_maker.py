"""C60 TMFD6 Solo R47-minimal Maker (inst RT) — live-runtime BaseStrategy wrapper.

Post-PROMOTE shadow-scaffold (R1 T8, 2026-04-19). Wraps the research artifact
at ``research.alphas.c60_tmfd6_r47_minimal_inst_rt.impl.C60Alpha`` for the
live runtime without mutating the research-side implementation.

**SHADOW_SCAFFOLDED_2026-04-19; enabled=false pending user confirmation**

R47-minimal variant on TMFD6 at inst RT:
  - spread_threshold_pts = 5 (cost-gate; TMF inst RT 1.5 pt; retail ref 4 pt)
  - max_pos = 2 canonical (T5 fresh CK-direct best of {1, 2, 3})
  - inventory_skew_tenths = 2 (0.2 ticks per contract, R47 best practice)
  - D4 QI skew layer RETAINED (qi_skew_threshold=0.10, widen_ticks=1) —
    deployed TMFD6 config; NOT |pos|-modulated (avoids C22-class meta-kill).
  - Other signal layers (PE / Queue / MFG) DISABLED — TMFD6 R47-minimal
    deployed baseline.

Cost citation (BINDING):
  `outputs/team_artifacts/alpha-research/shared-context.yaml#cost_model.TMF`
  tier: institutional_estimate, confirmed: false
  → `requires_broker_confirmation_before_live: true` (see RELEASE_GATE.md)

Shadow-scaffold defaults (enabled=false in strategies.yaml):
  - Live prices are scaled int **x10000** (platform convention). Research
    harness operated on **x1_000_000** CK prices. This wrapper does no unit
    translation on the price itself — it reads L1 from the runtime
    ``StrategyContext`` (which already serves x10000) and uses those
    integers directly.
  - Event dispatch uses ``on_stats(LOBStatsEvent)`` (hot path). The research
    ``on_tick(TickData)`` protocol is not replicated live.
  - ``queue_share`` is an informational parameter kept for
    research-live parity of the strategies.yaml block; live broker decides
    fills, not a simulated queue model.

R5+R6 physics carry (same as C33 pattern):
  - TMF point_value = 10 NTD/pt (NOT 200 like TXF). Single-instrument;
    no cross-instrument hedge leg → R5 hedge-qty rule N/A.
  - No incremental cost beyond R47 TMFD6 base (1.5 pt inst RT per full cycle).
"""

from __future__ import annotations

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import RiskFeedback, Side
from hft_platform.core.timebase import now_ns as _now_ns
from hft_platform.events import BidAskEvent, GapEvent, LOBStatsEvent, TickEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("strategy.c60_tmfd6_solo_maker")

_PRICE_SCALE = 10_000  # live runtime convention (CK is x1_000_000 — not used here)
_LOG_INTERVAL = 500


class C60TmfD6SoloMakerMinimal(BaseStrategy):
    """Live-runtime wrapper for C60 TMFD6 R47-minimal maker under inst RT.

    Research artifact: ``research/alphas/c60_tmfd6_r47_minimal_inst_rt/``
    Parity: parameter names/defaults mirror ``C60Params`` from the research
    impl exactly except defaults are the T8 canonical (spread=5, max_pos=2,
    skew=2 tenths, D4 QI active, D1/D2/D3 off).
    """

    __slots__ = (
        # parameters
        "_max_pos",
        "_spread_threshold_pts",
        "_inventory_skew_tenths",
        "_qi_skew_threshold",
        "_qi_skew_widen_ticks",
        "_enable_qi_layer",
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
        "_qi_widen_events",
        "_quotes_posted",
        "_last_log_ts_ns",
    )

    def __init__(
        self,
        strategy_id: str = "c60_tmfd6_solo_maker",
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

        # Canonical T8 defaults: mp=2, spread=5, skew_tenths=2, QI active.
        self._max_pos: int = _as_int(kwargs.get("max_pos", 2), 2)
        self._spread_threshold_pts: int = _as_int(
            kwargs.get("spread_threshold_pts", 5), 5
        )
        self._inventory_skew_tenths: int = _as_int(
            kwargs.get("inventory_skew_tenths", 2), 2
        )
        self._qi_skew_threshold: float = _as_float(
            kwargs.get("qi_skew_threshold", 0.10), 0.10
        )
        self._qi_skew_widen_ticks: int = _as_int(
            kwargs.get("qi_skew_widen_ticks", 1), 1
        )
        self._enable_qi_layer: bool = bool(
            kwargs.get("enable_qi_layer", True)
        )
        self._shadow_mode: bool = bool(kwargs.get("shadow_mode", False))
        self._queue_share_info: float = _as_float(
            kwargs.get("queue_share", 0.05), 0.05
        )
        variant = kwargs.get("variant", "R47-minimal")
        self._variant_label: str = (
            str(variant) if variant is not None else "R47-minimal"
        )

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
        self._qi_widen_events = 0
        self._quotes_posted = 0
        self._last_log_ts_ns: int = 0

        logger.info(
            "c60_initialized",
            strategy_id=strategy_id,
            max_pos=self._max_pos,
            spread_threshold_pts=self._spread_threshold_pts,
            inventory_skew_tenths=self._inventory_skew_tenths,
            qi_skew_threshold=self._qi_skew_threshold,
            qi_skew_widen_ticks=self._qi_skew_widen_ticks,
            enable_qi_layer=self._enable_qi_layer,
            shadow_mode=self._shadow_mode,
            queue_share_info=self._queue_share_info,
            variant=self._variant_label,
            symbols=sorted(self._symbols_set),
        )

    # ---- Hot-path event handlers ------------------------------------------

    def on_tick(self, event: TickEvent) -> None:  # pragma: no cover
        # Trade events are not drivers in R47-minimal (signal layers off).
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

        # L2 D4 QI skew (top-of-book imbalance; NOT |pos|-modulated).
        widen_bid_ticks, widen_ask_ticks = self._compute_qi_skew(event)
        if widen_bid_ticks or widen_ask_ticks:
            self._qi_widen_events += 1

        # Fixed inventory skew (R47: 0.X ticks/contract; 1 tick = _PRICE_SCALE)
        skew = (pos * self._inventory_skew_tenths * _PRICE_SCALE) // 10
        bid_quote = best_bid - skew - widen_bid_ticks * _PRICE_SCALE
        ask_quote = best_ask - skew + widen_ask_ticks * _PRICE_SCALE

        bid_moved = bid_quote != self._last_bid.get(symbol, -1)
        ask_moved = ask_quote != self._last_ask.get(symbol, -1)

        # Shadow-mode: still emit OrderIntents — system routes through
        # HFT_ORDER_SHADOW_MODE=1 gate, which suppresses broker dispatch.

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
                "c60_stats",
                symbol=symbol,
                pos=pos,
                pending_buy=pending_b,
                pending_sell=pending_s,
                spread_pts=event.spread_scaled // _PRICE_SCALE,
                quotes=self._quotes_posted,
                spread_blk=self._spread_blocked,
                qi_widen=self._qi_widen_events,
                now_ns=_now_ns(),
            )

    def _compute_qi_skew(self, event: LOBStatsEvent) -> tuple[int, int]:
        """L2 D4 QI skew: widen the side that is "light" in queue imbalance.

        Positive imbalance => bid-heavy => widen ASK (pull up 1 tick).
        Negative imbalance => ask-heavy => widen BID (pull down 1 tick).
        Within threshold [-qi_skew_threshold, +qi_skew_threshold] => neutral.

        Uses the pre-computed ``event.imbalance`` (LOBStatsEvent contract).
        Imbalance convention: (bid_depth - ask_depth) / (bid_depth + ask_depth).
        """
        if not self._enable_qi_layer:
            return 0, 0
        imbalance = event.imbalance
        if imbalance is None:
            return 0, 0
        threshold = self._qi_skew_threshold
        widen = self._qi_skew_widen_ticks
        if imbalance > threshold:
            return 0, widen
        if imbalance < -threshold:
            return widen, 0
        return 0, 0

    # ---- Fill / order / gap / risk feedback --------------------------------

    def on_fill(self, event: FillEvent) -> None:
        symbol = event.symbol
        if event.side == Side.BUY:
            self._local_pos[symbol] = self._local_pos.get(symbol, 0) + event.qty
            self._pending_buy[symbol] = max(
                0, self._pending_buy.get(symbol, 0) - event.qty
            )
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
            self._pending_buy[symbol] = max(
                0, self._pending_buy.get(symbol, 0) - 1
            )
            self._last_bid.pop(symbol, None)
        elif feedback.side == Side.SELL:
            self._pending_sell[symbol] = max(
                0, self._pending_sell.get(symbol, 0) - 1
            )
            self._last_ask.pop(symbol, None)

    def on_gap(self, event: GapEvent) -> None:
        """Clear all transient state on bus overflow."""
        logger.warning(
            "c60_gap_reset",
            missed_events=getattr(event, "missed_count", 0),
        )
        self._pending_buy.clear()
        self._pending_sell.clear()
        self._last_bid.clear()
        self._last_ask.clear()
