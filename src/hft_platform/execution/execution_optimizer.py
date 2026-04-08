"""Execution optimizer: limit vs market order decision based on LOB state.

Empirical basis: Albers et al. 2025 — fill probability modeled from
Q_near (queue depth on our side), Q_opp (queue depth on opposite side),
and L1 imbalance.

Decision logic:
1. Spread >= spread_threshold_pts: limit order saves >= 1 tick (1 pt = 10 NTD on TMFD6).
2. fill_score = Q_opp / max(Q_near, 1) > fill_score_threshold: favorable queue dynamics.
3. Urgent flag or disabled: always MARKET.
4. Timeout: pending limit cancelled after limit_timeout_ns.

Usage::

    opt = ExecutionOptimizer(spread_threshold_pts=2, fill_score_threshold=1.5)

    order_type = opt.decide(
        spread_pts=3, near_depth=10, opp_depth=20,
        imbalance_ppm=150_000, side=+1, ts_ns=now_ns,
    )
    if order_type == OrderType.LIMIT:
        # place limit order, then monitor timeout
        if opt.check_timeout(later_ns):
            # cancel limit, switch to market
            opt.on_cancel()
    else:
        # place market order immediately
        pass

    # On fill callback:
    opt.on_fill()
"""

from __future__ import annotations

from enum import IntEnum

import structlog

from hft_platform.execution.regime_classifier import Regime

logger = structlog.get_logger(__name__)


class OrderType(IntEnum):
    """Order execution type."""

    MARKET = 0
    LIMIT = 1


class _OptimizerState(IntEnum):
    IDLE = 0
    PENDING_LIMIT = 1


class ExecutionOptimizer:
    """Decide limit vs market order based on LOB state.

    Parameters
    ----------
    spread_threshold_pts : int
        Minimum spread (in price points) to consider limit order.  Default: 2.
    fill_score_threshold : float | int
        Minimum Q_opp / Q_near ratio to use limit.  Default: 1.5.
        Internally stored as scaled integer x1000 for Precision Law compliance.
    limit_timeout_ns : int
        Max wait for limit fill before fallback to market.
        Default: 3_000_000_000 (3s).
    enabled : bool
        If False, always returns MARKET.
    """

    __slots__ = (
        "_spread_threshold_pts",
        "_fill_score_threshold_x1000",
        "_limit_timeout_ns",
        "_enabled",
        "_states",
        "_pending_sides",
        "_pending_start_times",
    )

    def __init__(
        self,
        spread_threshold_pts: int = 2,
        fill_score_threshold: float = 1.5,
        limit_timeout_ns: int = 3_000_000_000,
        enabled: bool = True,
    ) -> None:
        self._spread_threshold_pts: int = spread_threshold_pts
        # Store as scaled integer x1000 for integer-only arithmetic on hot path
        self._fill_score_threshold_x1000: int = int(fill_score_threshold * 1000)
        self._limit_timeout_ns: int = limit_timeout_ns
        self._enabled: bool = enabled
        self._states: dict[str, _OptimizerState] = {}
        self._pending_sides: dict[str, int] = {}
        self._pending_start_times: dict[str, int] = {}

    def decide(
        self,
        spread_pts: int,
        near_depth: int,
        opp_depth: int,
        imbalance_ppm: int,
        side: int,
        ts_ns: int,
        urgent: bool = False,
        regime: Regime = Regime.NEUTRAL,
        symbol: str = "",
    ) -> OrderType:
        """Decide whether to use LIMIT or MARKET order.

        Parameters
        ----------
        spread_pts : int
            Current spread in price points (integer ticks).
        near_depth : int
            Queue depth on our side (depth we would join).
        opp_depth : int
            Queue depth on opposite side (depth we would hit).
        imbalance_ppm : int
            Current LOB imbalance in parts-per-million.
        side : int
            +1 for buy, -1 for sell.
        ts_ns : int
            Current timestamp in nanoseconds.
        urgent : bool
            If True, always return MARKET (risk exits, StormGuard HALT).
        regime : Regime
            Current execution regime from RegimeClassifier.
            ADVERSE forces MARKET, FAVORABLE relaxes thresholds.
            Default: NEUTRAL (existing heuristic unchanged).

        Returns
        -------
        OrderType
            LIMIT or MARKET.
        """
        # Fast paths: disabled or urgent -> MARKET
        if not self._enabled or urgent:
            return OrderType.MARKET

        # Regime gate: ADVERSE → force MARKET (don't waste time on limits)
        if regime == Regime.ADVERSE:
            return OrderType.MARKET

        # Spread too narrow -> limit saves nothing
        # In FAVORABLE regime, relax spread threshold by 1 pt (more aggressive limit usage)
        effective_spread_threshold = self._spread_threshold_pts
        if regime == Regime.FAVORABLE and effective_spread_threshold > 1:
            effective_spread_threshold -= 1

        if spread_pts < effective_spread_threshold:
            return OrderType.MARKET

        # Compute fill score: Q_opp / max(Q_near, 1) adjusted by imbalance
        # High ratio = opposite side has lots of depth (likely to trade through)
        # = favorable for limit order on our side
        # Imbalance bonus: favorable imbalance increases fill confidence
        near_clamped = near_depth if near_depth > 1 else 1
        if opp_depth <= 0:
            return OrderType.MARKET
        # Scaled integer arithmetic (x1000) — no float on hot path
        fill_score_x1000 = (opp_depth * 1000) // near_clamped
        # Favorable imbalance (buy+positive or sell+negative) adds 500 (= 0.5 × 1000)
        favorable_imb = (side > 0 and imbalance_ppm > 200_000) or (side < 0 and imbalance_ppm < -200_000)
        if favorable_imb:
            fill_score_x1000 += 500

        # In FAVORABLE regime, relax fill score threshold by 500 (= 0.5 × 1000)
        effective_threshold_x1000 = self._fill_score_threshold_x1000
        if regime == Regime.FAVORABLE:
            effective_threshold_x1000 = max(effective_threshold_x1000 - 500, 500)

        if fill_score_x1000 < effective_threshold_x1000:
            return OrderType.MARKET

        # All conditions met -> LIMIT
        self._states[symbol] = _OptimizerState.PENDING_LIMIT
        self._pending_sides[symbol] = side
        self._pending_start_times[symbol] = ts_ns

        return OrderType.LIMIT

    def check_timeout(self, ts_ns: int, symbol: str = "") -> bool:
        """Check if pending limit order has timed out.

        Parameters
        ----------
        ts_ns : int
            Current timestamp in nanoseconds.
        symbol : str
            Symbol to check timeout for.

        Returns
        -------
        bool
            True if the pending limit order should be cancelled
            and replaced with a market order.
        """
        if self._states.get(symbol, _OptimizerState.IDLE) != _OptimizerState.PENDING_LIMIT:
            return False

        elapsed = ts_ns - self._pending_start_times.get(symbol, 0)
        if elapsed >= self._limit_timeout_ns:
            logger.info(
                "execution_optimizer.limit_timeout",
                elapsed_ns=elapsed,
                side=self._pending_sides.get(symbol, 0),
                symbol=symbol,
            )
            return True

        return False

    def on_fill(self, symbol: str = "") -> None:
        """Called when pending limit order fills.  Resets state for symbol."""
        self._states.pop(symbol, None)
        self._pending_sides.pop(symbol, None)
        self._pending_start_times.pop(symbol, None)

    def on_cancel(self, symbol: str = "") -> None:
        """Called when pending limit order is cancelled.  Resets state for symbol."""
        self._states.pop(symbol, None)
        self._pending_sides.pop(symbol, None)
        self._pending_start_times.pop(symbol, None)

    # --- Properties ---

    def is_pending_for(self, symbol: str = "") -> bool:
        """True if a limit order decision is pending fill/cancel for symbol."""
        return self._states.get(symbol, _OptimizerState.IDLE) == _OptimizerState.PENDING_LIMIT

    @property
    def is_pending(self) -> bool:
        """True if any symbol has a pending limit order."""
        return any(s == _OptimizerState.PENDING_LIMIT for s in self._states.values())

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        if not value:
            self._states.clear()
            self._pending_sides.clear()
            self._pending_start_times.clear()
