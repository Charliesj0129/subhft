"""Elapse-mode run loop for HftBacktestAdapter.

Extracted from adapter.py (WU-01) — steps by ``elapse_ns`` nanoseconds.
All intermediate LOB updates are processed internally by hftbacktest
(queue position stays accurate), but Python only gets called at each
elapse boundary.
"""

from __future__ import annotations

from hft_platform.backtest._hbt_utils import (
    build_lob_event,
    dispatch_strategy,
    logger,
    process_fills,
    validate_depth,
)


def run_elapse(adapter: object) -> object:
    """Run simulation stepping by elapse_ns nanoseconds at a time.

    Args:
        adapter: HftBacktestAdapter instance (duck-typed to avoid circular import).

    Returns:
        Result of ``adapter.hbt.close()``.
    """
    logger.info(
        "Starting HftBacktest simulation (elapse mode)...",
        elapse_ns=adapter.elapse_ns,  # type: ignore[attr-defined]
    )
    adapter._reset_equity_buffers()  # type: ignore[attr-defined]

    while adapter.hbt.elapse(adapter.elapse_ns) == 0:  # type: ignore[attr-defined]
        dp = adapter.hbt.depth(0)  # type: ignore[attr-defined]
        best_bid = dp.best_bid
        best_ask = dp.best_ask
        if not validate_depth(best_bid, best_ask):
            continue

        best_bid_int = int(best_bid)
        best_ask_int = int(best_ask)
        ts_ns = int(adapter.hbt.current_timestamp)  # type: ignore[attr-defined]

        # Access trades that occurred during this elapse interval
        last_trades = None
        try:
            last_trades = adapter.hbt.last_trades(0)  # type: ignore[attr-defined]
            adapter.hbt.clear_last_trades(0)  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass

        event, feature_event = build_lob_event(
            adapter,
            dp,
            ts_ns,
            best_bid_int,
            best_ask_int,
        )

        # Attach last_trades to event for MM strategies (best-effort)
        if last_trades is not None:
            try:
                event.last_trades = last_trades  # type: ignore[attr-defined]
            except AttributeError:
                pass  # __slots__ dataclass — skip

        process_fills(adapter, ts_ns, best_bid_int, best_ask_int)
        dispatch_strategy(adapter, event, feature_event)

    return adapter.hbt.close()  # type: ignore[attr-defined]
