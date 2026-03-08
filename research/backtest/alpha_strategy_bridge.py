"""alpha_strategy_bridge.py — Wraps AlphaProtocol as BaseStrategy for HftBacktestAdapter.

Part B of the dirty-data-repair + golden-data pipeline plan.

The bridge extracts L1 LOB data from LOBStatsEvent, calls alpha.update(**payload),
records (ts_ns, signal, mid_price) in signal_log, and returns empty OrderIntents
(position management is handled by HftNativeRunner from the signal log).
"""
from __future__ import annotations

from typing import Any

import numpy as np

from hft_platform.contracts.strategy import OrderIntent, Side, TIF
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy, StrategyContext

_PRICE_SCALE = 10_000  # platform default


class AlphaStrategyBridge(BaseStrategy):
    """Wraps AlphaProtocol as a BaseStrategy for HftBacktestAdapter.

    This bridge does NOT manage orders itself.  Instead it records every
    (ts_ns, signal, mid_price) tuple in signal_log so that HftNativeRunner
    can extract them after the backtest loop and compute BacktestResult metrics
    using the same infrastructure as ResearchBacktestRunner.

    Args:
        alpha: Any object implementing AlphaProtocol (manifest, reset, update).
        max_position: Upper bound on absolute position size (used by callers).
        signal_threshold: Minimum |signal| to act on (used by callers).
        symbol: Asset symbol string (used to filter events).
        price_scale: Divisor to convert scaled-integer prices to float.
    """

    def __init__(
        self,
        alpha: Any,
        *,
        max_position: int = 5,
        signal_threshold: float = 0.3,
        symbol: str = "",
        price_scale: int = _PRICE_SCALE,
        strategy_id: str = "alpha_bridge",
    ):
        super().__init__(strategy_id=strategy_id, subscribe_symbols=[symbol] if symbol else [])
        self._alpha = alpha
        self.max_position = int(max_position)
        self.signal_threshold = float(signal_threshold)
        self._price_scale = int(price_scale)
        self._symbol = symbol
        self._signal_log: list[tuple[int, float, float]] = []  # (ts_ns, signal, mid_price)

    def reset(self) -> None:
        """Reset alpha state and clear signal log."""
        self._signal_log.clear()
        try:
            self._alpha.reset()
        except Exception:
            pass

    @property
    def signal_log(self) -> list[tuple[int, float, float]]:
        """Read-only access to accumulated (ts_ns, signal, mid_price) tuples."""
        return self._signal_log

    # ------------------------------------------------------------------
    # BaseStrategy event dispatch
    # ------------------------------------------------------------------

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Respond to LOBStatsEvent: call alpha, record signal."""
        ts_ns = int(event.ts)

        # Extract float prices from scaled integers
        best_bid = float(event.best_bid) / self._price_scale
        best_ask = float(event.best_ask) / self._price_scale
        mid_price = (best_bid + best_ask) / 2.0

        bid_depth = float(getattr(event, "bid_depth", 0) or 0)
        ask_depth = float(getattr(event, "ask_depth", 0) or 0)
        imbalance = float(getattr(event, "imbalance", 0.0) or 0.0)

        # Build payload matching typical alpha.update() field names
        payload: dict[str, Any] = {
            "bid_px": best_bid,
            "ask_px": best_ask,
            "bid_qty": bid_depth,
            "ask_qty": ask_depth,
            "mid_price": mid_price,
            "current_mid": mid_price,
            "spread_bps": (best_ask - best_bid) / mid_price * 10_000.0 if mid_price > 0.0 else 0.0,
            "volume": 0.0,  # LOBStatsEvent does not carry trade volume
            "trade_vol": 0.0,
            "imbalance": imbalance,
            "local_ts": ts_ns,
        }

        try:
            signal = float(self._alpha.update(**payload))
        except TypeError:
            # Some alphas only accept positional-style; try positional via keyword subset
            try:
                signal = float(self._alpha.update(
                    bid_px=best_bid,
                    ask_px=best_ask,
                    bid_qty=bid_depth,
                    ask_qty=ask_depth,
                ))
            except Exception:
                signal = 0.0
        except Exception:
            signal = 0.0

        self._signal_log.append((ts_ns, signal, mid_price))

    # Return empty intents — HftNativeRunner drives position management externally
    # (the adapter's order execution is not used by this runner)
    def handle_event(self, ctx: StrategyContext, event: Any) -> list[OrderIntent]:
        self.ctx = ctx
        self._generated_intents.clear()

        if isinstance(event, LOBStatsEvent):
            # Apply symbol filter if configured
            if self._symbol and hasattr(event, "symbol") and event.symbol != self._symbol:
                return []
            self.on_stats(event)

        return self._generated_intents


# ---------------------------------------------------------------------------
# Numpy helpers — used by HftNativeRunner to extract arrays from signal_log
# ---------------------------------------------------------------------------
def signal_log_to_arrays(
    signal_log: list[tuple[int, float, float]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Convert signal_log to (timestamps, signals, mid_prices) arrays.

    Returns:
        timestamps_ns: int64 array
        signals: float64 array
        mid_prices: float64 array
    """
    if not signal_log:
        empty_i = np.zeros(0, dtype=np.int64)
        empty_f = np.zeros(0, dtype=np.float64)
        return empty_i, empty_f, empty_f

    arr = np.array(signal_log, dtype=np.float64)
    timestamps = arr[:, 0].astype(np.int64)
    signals = arr[:, 1]
    mid_prices = arr[:, 2]
    return timestamps, signals, mid_prices
