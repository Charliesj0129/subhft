# src/hft_platform/execution/slippage_tracker.py
"""Per-fill real-time slippage tracker.

Computes slippage for each fill and exports Prometheus metrics.
Not hot-path: called in the fill callback path (parallel to recording).
"""

from __future__ import annotations

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent
from hft_platform.tca.slippage import SlippageDecomposer

logger = get_logger("execution.slippage_tracker")

try:
    from prometheus_client import Counter, Histogram

    SLIPPAGE_BPS: Histogram | None = Histogram(
        "hft_fill_slippage_bps",
        "Per-fill total slippage in basis points",
        ["strategy", "symbol"],
        buckets=[0, 0.5, 1, 2, 5, 10, 20, 50],
    )
    FILLS_TRACKED: Counter | None = Counter(
        "hft_slippage_fills_tracked_total",
        "Total fills processed by slippage tracker",
    )
except ImportError:
    SLIPPAGE_BPS = None
    FILLS_TRACKED = None


_MAX_LABEL_SYMBOLS = 200
_seen_symbols: set[str] = set()


def _cap_symbol(symbol: str) -> str:
    if symbol in _seen_symbols:
        return symbol
    if len(_seen_symbols) < _MAX_LABEL_SYMBOLS:
        _seen_symbols.add(symbol)
        return symbol
    return "_other"


class SlippageTracker:
    __slots__ = ("_decomposer", "_total_tracked", "_last_slippage_bps")

    def __init__(self, *, point_value: int = 10, tick_size: float = 1.0) -> None:
        self._decomposer = SlippageDecomposer(point_value=point_value, tick_size=tick_size)
        self._total_tracked: int = 0
        self._last_slippage_bps: float = 0.0

    @property
    def total_tracked(self) -> int:
        return self._total_tracked

    @property
    def last_slippage_bps(self) -> float:
        return self._last_slippage_bps

    def track(self, fill: FillEvent) -> None:
        self._total_tracked += 1

        if fill.decision_price == 0 and fill.arrival_price == 0:
            self._last_slippage_bps = 0.0
            return

        notional_ntd = abs(fill.price * fill.qty) // 10_000
        if notional_ntd == 0:
            self._last_slippage_bps = 0.0
            return

        breakdown = self._decomposer.decompose(
            decision_price=fill.decision_price,
            arrival_price=fill.arrival_price,
            fill_price=fill.price,
            notional_ntd=notional_ntd,
            fee_ntd=fill.fee // 10_000,
            tax_ntd=fill.tax // 10_000,
        )
        self._last_slippage_bps = breakdown.total_bps

        if SLIPPAGE_BPS is not None:
            SLIPPAGE_BPS.labels(strategy=fill.strategy_id, symbol=_cap_symbol(fill.symbol)).observe(breakdown.total_bps)
        if FILLS_TRACKED is not None:
            FILLS_TRACKED.inc()

        logger.debug(
            "slippage_tracked",
            fill_id=fill.fill_id,
            total_bps=round(breakdown.total_bps, 2),
            delay_bps=round(breakdown.delay_cost_bps, 2),
            exec_bps=round(breakdown.execution_cost_bps, 2),
        )
