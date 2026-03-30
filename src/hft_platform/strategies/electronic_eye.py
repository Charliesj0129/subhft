"""Electronic Eye: automated TXO options market-making with delta-neutral hedging."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from structlog import get_logger

from hft_platform.contracts.strategy import RiskFeedback, Side
from hft_platform.contracts.execution import FillEvent, OrderEvent
from hft_platform.events import BidAskEvent, LOBStatsEvent, TickEvent
from hft_platform.core import timebase
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("strategy.electronic_eye")


class EyeState(enum.IntEnum):
    INIT = 0
    QUOTING = 1
    NARROW = 2
    RESTRICT = 3
    HALT = 4


class Guardian:
    """Risk state machine for the Electronic Eye strategy.

    State transitions:
        INIT -> QUOTING: via activate()
        QUOTING -> NARROW: utilization > warn_pct
        NARROW -> QUOTING: utilization <= warn_pct AND stress OK
        QUOTING/NARROW -> RESTRICT: greeks rejection or stress failure
        RESTRICT -> QUOTING: stress OK AND utilization OK
        ANY -> HALT: on_halt() (terminal, no recovery)
    """

    __slots__ = (
        "state",
        "_warn_pct",
        "_max_worst_pnl",
        "_stress_interval_s",
        "_last_stress_ok",
        "_last_util_ok",
    )

    def __init__(
        self,
        warn_utilization_pct: float = 80.0,
        stress_interval_s: int = 60,
        max_worst_case_pnl_ntd: int = -500_000,
    ) -> None:
        self.state = EyeState.INIT
        self._warn_pct = warn_utilization_pct
        self._max_worst_pnl = max_worst_case_pnl_ntd
        self._stress_interval_s = stress_interval_s
        self._last_stress_ok = True
        self._last_util_ok = True

    def activate(self) -> None:
        """Transition INIT -> QUOTING to begin market-making."""
        if self.state == EyeState.INIT:
            self.state = EyeState.QUOTING
            logger.info("guardian_activated", state="QUOTING")

    def on_utilization(self, pct: float) -> None:
        """Update risk state based on current margin utilization percentage."""
        self._last_util_ok = pct <= self._warn_pct
        self._reevaluate()

    def on_greeks_rejection(self, reason: str) -> None:
        """Transition to RESTRICT when a greeks-limit breach is detected."""
        if self.state in (EyeState.QUOTING, EyeState.NARROW):
            self.state = EyeState.RESTRICT
            logger.warning("guardian_restrict", reason=reason)

    def on_stress_result(self, within_limits: bool, worst_pnl: float) -> None:
        """Update risk state based on the latest stress-test result."""
        self._last_stress_ok = within_limits
        if not within_limits and self.state in (EyeState.QUOTING, EyeState.NARROW):
            self.state = EyeState.RESTRICT
            logger.warning("guardian_restrict_stress", worst_pnl=worst_pnl)
        elif within_limits:
            self._reevaluate()

    def on_halt(self) -> None:
        """Transition to HALT (terminal). Requires manual intervention to recover."""
        if self.state != EyeState.HALT:
            self.state = EyeState.HALT
            logger.error("guardian_halt")

    def allows_new_quotes(self) -> bool:
        """Return True if new quote orders may be submitted."""
        return self.state in (EyeState.QUOTING, EyeState.NARROW)

    def should_flatten(self) -> bool:
        """Return True if all positions must be flattened immediately."""
        return self.state == EyeState.HALT

    def _reevaluate(self) -> None:
        """Internal: recompute state from cached stress and utilization flags."""
        if self.state == EyeState.HALT:
            return
        if self._last_stress_ok and self._last_util_ok:
            if self.state in (EyeState.NARROW, EyeState.RESTRICT):
                self.state = EyeState.QUOTING
                logger.info("guardian_clear", state="QUOTING")
        elif not self._last_util_ok:
            if self.state == EyeState.QUOTING:
                self.state = EyeState.NARROW
                logger.info("guardian_narrow")


@dataclass(slots=True)
class EdgeResult:
    has_bid_edge: bool
    has_ask_edge: bool
    bid_price: float
    ask_price: float


def _compute_edge(
    theo_price: float,
    market_bid: float,
    market_ask: float,
    min_edge_ticks: int,
    tick_size: float,
) -> EdgeResult:
    edge = min_edge_ticks * tick_size
    bid_price = theo_price - edge
    ask_price = theo_price + edge
    return EdgeResult(
        has_bid_edge=bid_price > market_bid,
        has_ask_edge=theo_price > market_ask,
        bid_price=bid_price,
        ask_price=ask_price,
    )


def _scale_to_int(price: float, price_scale: int) -> int:
    return int(round(price * price_scale))


class QuoterState:
    __slots__ = ("_max_per_strike", "_qty")

    def __init__(self, max_contracts_per_strike: int = 5) -> None:
        self._max_per_strike = max_contracts_per_strike
        self._qty: dict[str, int] = {}

    def current_qty(self, symbol: str) -> int:
        return self._qty.get(symbol, 0)

    def can_quote(self, symbol: str, additional: int = 1) -> bool:
        return self.current_qty(symbol) + additional <= self._max_per_strike

    def record_quote(self, symbol: str, qty: int = 1) -> None:
        self._qty[symbol] = self._qty.get(symbol, 0) + qty

    def record_cancel(self, symbol: str, qty: int = 1) -> None:
        self._qty[symbol] = max(0, self._qty.get(symbol, 0) - qty)


class HedgerState:
    __slots__ = ("_threshold", "_cooldown_ns", "_max_qty", "_last_hedge_ns")

    def __init__(
        self,
        delta_threshold_lots: int = 3,
        cooldown_ms: int = 1000,
        max_hedge_qty: int = 10,
    ) -> None:
        self._threshold = delta_threshold_lots
        self._cooldown_ns = cooldown_ms * 1_000_000
        self._max_qty = max_hedge_qty
        self._last_hedge_ns = 0

    def should_hedge(self, hedge_lots: int, now_ns: int) -> bool:
        if abs(hedge_lots) <= self._threshold:
            return False
        if now_ns - self._last_hedge_ns < self._cooldown_ns:
            return False
        return True

    def record_hedge(self, now_ns: int) -> None:
        self._last_hedge_ns = now_ns

    def clamp_qty(self, lots: int) -> int:
        if lots > self._max_qty:
            return self._max_qty
        if lots < -self._max_qty:
            return -self._max_qty
        return lots

    def hedge_direction(self, hedge_lots: int) -> tuple:
        from hft_platform.contracts.strategy import Side  # noqa: PLC0415 — deferred to avoid circular import

        if hedge_lots > 0:
            return Side.SELL, min(abs(hedge_lots), self._max_qty)
        return Side.BUY, min(abs(hedge_lots), self._max_qty)


class ElectronicEye(BaseStrategy):
    """Automated TXO options market-making with delta-neutral hedging."""

    __slots__ = (
        "_quoter_cfg", "_hedger_cfg", "_guardian_cfg", "_publish_cfg",
        "guardian", "_hedger", "_quoter_state", "_last_publish_ns",
    )

    def __init__(self, strategy_id="electronic_eye", quoter=None, hedger=None, guardian=None, publish=None, **kwargs):
        super().__init__(strategy_id=strategy_id, **kwargs)
        self._quoter_cfg = quoter or {}
        self._hedger_cfg = hedger or {}
        self._guardian_cfg = guardian or {}
        self._publish_cfg = publish or {}

        self.guardian = Guardian(
            warn_utilization_pct=self._guardian_cfg.get("warn_utilization_pct", 80),
            stress_interval_s=self._guardian_cfg.get("stress_interval_s", 60),
            max_worst_case_pnl_ntd=self._guardian_cfg.get("max_worst_case_pnl_ntd", -500000),
        )
        self._hedger = HedgerState(
            delta_threshold_lots=self._hedger_cfg.get("delta_threshold_lots", 3),
            cooldown_ms=self._hedger_cfg.get("hedge_cooldown_ms", 1000),
            max_hedge_qty=self._hedger_cfg.get("max_hedge_qty_per_order", 10),
        )
        self._quoter_state = QuoterState(
            max_contracts_per_strike=self._quoter_cfg.get("max_contracts_per_strike", 5),
        )
        self._last_publish_ns = 0

    def on_risk_feedback(self, feedback: RiskFeedback) -> None:
        if feedback.reason_code.startswith("GREEKS_"):
            self.guardian.on_greeks_rejection(feedback.reason_code)
            logger.warning("eye_greeks_rejection", reason=feedback.reason_code, symbol=feedback.symbol, state=self.guardian.state.name)

    def on_book_update(self, event: BidAskEvent) -> None:
        if not self.guardian.allows_new_quotes():
            return
        # Quoter logic stub — wired to VolSurface during shadow deployment

    def on_fill(self, event: FillEvent) -> None:
        # Hedger logic stub — wired to live_adapter during shadow deployment
        pass

    def on_stats(self, event: LOBStatsEvent) -> None:
        now_ns = int(event.ts or timebase.now_ns())
        publish_interval_ns = self._publish_cfg.get("interval_ms", 1000) * 1_000_000
        if now_ns - self._last_publish_ns >= publish_interval_ns:
            self._publish_state(now_ns)
            self._last_publish_ns = now_ns

    def _publish_state(self, now_ns: int) -> None:
        if not self.ctx:
            return
        channel = self._publish_cfg.get("channel", "monitor:portfolio:greeks")
        payload = {
            "ts": now_ns,
            "net_delta_lots": 0.0, "net_gamma_lots": 0.0,
            "net_theta_ntd": 0.0, "net_vega_ntd": 0.0,
            "worst_pnl_ntd": 0.0, "eye_state": self.guardian.state.name,
        }
        self.ctx.publish_state(channel, payload)
