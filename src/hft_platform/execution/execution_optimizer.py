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
    fill_score_threshold : float
        Minimum Q_opp / Q_near ratio to use limit.  Default: 1.5.
    limit_timeout_ns : int
        Max wait for limit fill before fallback to market.
        Default: 3_000_000_000 (3s).
    enabled : bool
        If False, always returns MARKET.
    """

    __slots__ = (
        "_spread_threshold_pts",
        "_fill_score_threshold",
        "_limit_timeout_ns",
        "_enabled",
        "_state",
        "_pending_side",
        "_pending_start_ns",
    )

    def __init__(
        self,
        spread_threshold_pts: int = 2,
        fill_score_threshold: float = 1.5,
        limit_timeout_ns: int = 3_000_000_000,
        enabled: bool = True,
    ) -> None:
        self._spread_threshold_pts: int = spread_threshold_pts
        self._fill_score_threshold: float = fill_score_threshold
        self._limit_timeout_ns: int = limit_timeout_ns
        self._enabled: bool = enabled
        self._state: _OptimizerState = _OptimizerState.IDLE
        self._pending_side: int = 0  # +1 = buy, -1 = sell
        self._pending_start_ns: int = 0

    def decide(
        self,
        spread_pts: int,
        near_depth: int,
        opp_depth: int,
        imbalance_ppm: int,
        side: int,
        ts_ns: int,
        urgent: bool = False,
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

        Returns
        -------
        OrderType
            LIMIT or MARKET.
        """
        # Fast paths: disabled or urgent -> MARKET
        if not self._enabled or urgent:
            return OrderType.MARKET

        # Spread too narrow -> limit saves nothing
        if spread_pts < self._spread_threshold_pts:
            return OrderType.MARKET

        # Compute fill score: Q_opp / max(Q_near, 1) adjusted by imbalance
        # High ratio = opposite side has lots of depth (likely to trade through)
        # = favorable for limit order on our side
        # Imbalance bonus: favorable imbalance increases fill confidence
        near_clamped = near_depth if near_depth > 1 else 1
        if opp_depth <= 0:
            return OrderType.MARKET
        fill_score = opp_depth / near_clamped
        # Favorable imbalance (buy+positive or sell+negative) adds 0.5 bonus
        favorable_imb = (side > 0 and imbalance_ppm > 200_000) or (side < 0 and imbalance_ppm < -200_000)
        if favorable_imb:
            fill_score += 0.5

        if fill_score < self._fill_score_threshold:
            return OrderType.MARKET

        # All conditions met -> LIMIT
        self._state = _OptimizerState.PENDING_LIMIT
        self._pending_side = side
        self._pending_start_ns = ts_ns

        return OrderType.LIMIT

    def check_timeout(self, ts_ns: int) -> bool:
        """Check if pending limit order has timed out.

        Parameters
        ----------
        ts_ns : int
            Current timestamp in nanoseconds.

        Returns
        -------
        bool
            True if the pending limit order should be cancelled
            and replaced with a market order.
        """
        if self._state != _OptimizerState.PENDING_LIMIT:
            return False

        elapsed = ts_ns - self._pending_start_ns
        if elapsed >= self._limit_timeout_ns:
            logger.info(
                "execution_optimizer.limit_timeout",
                elapsed_ns=elapsed,
                side=self._pending_side,
            )
            return True

        return False

    def on_fill(self) -> None:
        """Called when pending limit order fills.  Resets state."""
        self._state = _OptimizerState.IDLE
        self._pending_side = 0
        self._pending_start_ns = 0

    def on_cancel(self) -> None:
        """Called when pending limit order is cancelled.  Resets state."""
        self._state = _OptimizerState.IDLE
        self._pending_side = 0
        self._pending_start_ns = 0

    # --- Properties ---

    @property
    def is_pending(self) -> bool:
        """True if a limit order decision is pending fill/cancel."""
        return self._state == _OptimizerState.PENDING_LIMIT

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        if not value:
            self._state = _OptimizerState.IDLE
