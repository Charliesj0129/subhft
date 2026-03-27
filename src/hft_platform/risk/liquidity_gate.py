# src/hft_platform/risk/liquidity_gate.py
"""Spread-based liquidity gate — rejects orders when spread exceeds threshold."""

from __future__ import annotations

from structlog import get_logger

logger = get_logger("risk.liquidity_gate")

try:
    from prometheus_client import Counter

    GATE_CHECKS: Counter | None = Counter(
        "hft_liquidity_gate_checks_total",
        "Total liquidity gate checks",
        ["result"],
    )
except ImportError:
    GATE_CHECKS = None


class LiquidityGate:
    __slots__ = ("_max_spread_pts", "_total_checked", "_total_rejected")

    def __init__(self, *, max_spread_pts: float = 5.0) -> None:
        self._max_spread_pts = max_spread_pts
        self._total_checked: int = 0
        self._total_rejected: int = 0

    @property
    def total_checked(self) -> int:
        return self._total_checked

    @property
    def total_rejected(self) -> int:
        return self._total_rejected

    def check(self, *, spread_pts: float) -> bool:
        self._total_checked += 1

        if spread_pts > self._max_spread_pts:
            self._total_rejected += 1
            if GATE_CHECKS is not None:
                GATE_CHECKS.labels(result="rejected").inc()
            logger.info(
                "liquidity_gate_rejected",
                spread_pts=round(spread_pts, 2),
                threshold=self._max_spread_pts,
            )
            return False

        if GATE_CHECKS is not None:
            GATE_CHECKS.labels(result="passed").inc()
        return True
