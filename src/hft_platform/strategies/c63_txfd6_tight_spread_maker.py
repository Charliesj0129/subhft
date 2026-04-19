"""C63 TXFD6 R47-minimal with Tightened Spread — live-runtime BaseStrategy wrapper.

Post-PROMOTE-SUPPLEMENTAL shadow-scaffold (R2-SUPP T8, 2026-04-19). Wraps the
research artifact at ``research.alphas.c63_txfd6_r47_tight_spread.impl.C63Alpha``
for the live runtime without mutating the research-side implementation.

**SHADOW_SCAFFOLDED_2026-04-19; enabled=false pending user confirmation**

R47-minimal variant on TXFD6 at inst RT (tightened spread threshold):
  - spread_threshold_pts = 3 (LOWERED from C33's 5)
  - max_pos = 3 canonical (per fresh CK-direct best; T5 also ran {1,2,3})
  - inventory_skew_tenths = 2 (0.2 ticks per contract, R47 best practice)
  - All four R47 signal layers (PE / Queue / MFG / QI) DISABLED — C33 precedent.

## C63 REPLACES C33 on TXFD6 — MUTUALLY EXCLUSIVE

Both C33_TXFD6_SOLO_MAKER and C63_TXFD6_TIGHT_SPREAD_MAKER target TXFD6 with
the same mechanism (R47-minimal). Co-deployment would double-book positions.
``strategies.yaml`` MUST NEVER have both enabled=true. The canonical plan is
to disable C33 (already `enabled: true` for 1-lot exception rollout) and
replace with C63 at max_pos=3 after shadow clears the hard cost gate.

## HARD COST GATE (BINDING)

If broker-confirmed TXF RT > 2.5 pt, **C63 MUST NOT be deployed**.

Cost-fragility analysis (executor T5):
  - At inst RT 1.5 pt: +114,680 NTD/day (sp=3/mp=3 canonical)
  - At retail RT 3.0 pt: -14,447 NTD/day (sign flip)
  - Break-even RT: ~2.83 pt
  - Hard gate at 2.5 pt preserves safety margin

Cost citation (BINDING):
  ``outputs/team_artifacts/alpha-research/shared-context.yaml#cost_model.TXF``
  tier: institutional_estimate, confirmed: false
  -> ``requires_broker_confirmation_before_live: true`` AND
  -> ``hard_cost_gate_retail_rt_max_pt: 2.5`` (see RELEASE_GATE.md)

Shadow-scaffold defaults:
  - Live prices are scaled int **x10000** (platform convention).
  - Research harness operated on **x1_000_000** CK prices.
  - Event dispatch uses ``on_stats(LOBStatsEvent)`` (hot path).
  - ``queue_share`` is informational for research-live parity.

R5+R6 physics carry:
  - TXF point_value = 200 NTD/pt. Single-instrument; no hedge leg.
  - Precision Law: scaled-int math for all quote decisions.
"""

from __future__ import annotations

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import RiskFeedback, Side
from hft_platform.core.timebase import now_ns as _now_ns
from hft_platform.events import BidAskEvent, GapEvent, LOBStatsEvent, TickEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("strategy.c63_txfd6_tight_spread_maker")

_PRICE_SCALE = 10_000  # live runtime convention (CK is x1_000_000 — not used here)
_LOG_INTERVAL = 500


class C63TxfD6TightSpreadMaker(BaseStrategy):
    """Live-runtime wrapper for C63 TXFD6 R47-minimal with tightened spread.

    Research artifact: ``research/alphas/c63_txfd6_r47_tight_spread/``
    Parity: parameter names/defaults mirror ``C63Params`` from the research
    impl exactly except defaults are the T8 canonical (spread=3, max_pos=3,
    skew=2 tenths, all signal layers off).

    Differences vs C33 wrapper:
      - spread_threshold_pts default 3 (C33: 5)
      - max_pos default 3 (C33 live: 1 for conservative exception rollout)
      - no other changes (both are R47-minimal, all signal layers off)
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
        strategy_id: str = "c63_txfd6_tight_spread_maker",
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

        # Canonical T8 defaults: spread=3 (lowered from C33), mp=3, skew=2.
        self._max_pos: int = _as_int(kwargs.get("max_pos", 3), 3)
        self._spread_threshold_pts: int = _as_int(
            kwargs.get("spread_threshold_pts", 3), 3
        )
        self._inventory_skew_tenths: int = _as_int(
            kwargs.get("inventory_skew_tenths", 2), 2
        )
        self._shadow_mode: bool = bool(kwargs.get("shadow_mode", False))
        self._queue_share_info: float = _as_float(
            kwargs.get("queue_share", 0.05), 0.05
        )
        variant = kwargs.get("variant", "R47-minimal-tight-spread")
        self._variant_label: str = (
            str(variant) if variant is not None else "R47-minimal-tight-spread"
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
        self._quotes_posted = 0
        self._last_log_ts_ns: int = 0

        logger.info(
            "c63_initialized",
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

        # L1 spread gate — C63's signature lever (threshold=3 vs C33's 5).
        spread_threshold_scaled = self._spread_threshold_pts * _PRICE_SCALE
        if event.spread_scaled < spread_threshold_scaled:
            self._spread_blocked += 1
            return

        best_bid = event.best_bid
        best_ask = event.best_ask
        pos = self._local_pos.get(symbol, 0)
        pending_b = self._pending_buy.get(symbol, 0)
        pending_s = self._pending_sell.get(symbol, 0)

        # Linear inventory skew (R47: 0.X ticks/contract; NOT |pos|-gated).
        skew = (pos * self._inventory_skew_tenths * _PRICE_SCALE) // 10
        bid_quote = best_bid - skew
        ask_quote = best_ask - skew

        bid_moved = bid_quote != self._last_bid.get(symbol, -1)
        ask_moved = ask_quote != self._last_ask.get(symbol, -1)

        # Shadow-mode: still emit OrderIntents; system routes through
        # HFT_ORDER_SHADOW_MODE=1 gate to suppress broker dispatch.

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
                "c63_stats",
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
            "c63_gap_reset",
            missed_events=getattr(event, "missed_count", 0),
        )
        self._pending_buy.clear()
        self._pending_sell.clear()
        self._last_bid.clear()
        self._last_ask.clear()
