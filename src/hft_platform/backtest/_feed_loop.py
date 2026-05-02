"""Feed-mode run loop for HftBacktestAdapter.

Extracted from adapter.py (WU-01) — advances on every LOB feed event.
"""

from __future__ import annotations

from hft_platform.backtest._hbt_utils import (
    build_lob_event,
    dispatch_strategy,
    logger,
    process_fills,
    validate_depth,
)


def run_feed(adapter: object) -> object:
    """Run simulation advancing on every feed event.

    Args:
        adapter: HftBacktestAdapter instance (duck-typed to avoid circular import).

    Returns:
        Result of ``adapter.hbt.close()``.
    """
    logger.info("Starting HftBacktest simulation (feed mode)...")
    adapter._reset_equity_buffers()  # type: ignore[attr-defined]

    while adapter._wait_for_next_feed():  # type: ignore[attr-defined]
        dp = adapter.hbt.depth(0)  # type: ignore[attr-defined]
        best_bid = dp.best_bid
        best_ask = dp.best_ask
        if not validate_depth(best_bid, best_ask):
            continue

        best_bid_int = int(round(float(best_bid) * adapter.price_scale))  # type: ignore[attr-defined]
        best_ask_int = int(round(float(best_ask) * adapter.price_scale))  # type: ignore[attr-defined]
        ts_ns = int(adapter.hbt.current_timestamp)  # type: ignore[attr-defined]

        event, feature_event = build_lob_event(
            adapter,
            dp,
            ts_ns,
            best_bid_int,
            best_ask_int,
        )
        process_fills(adapter, ts_ns, best_bid_int, best_ask_int)
        dispatch_strategy(adapter, event, feature_event)

    return adapter.hbt.close()  # type: ignore[attr-defined]
