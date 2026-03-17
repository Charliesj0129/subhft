"""Shared utilities for HftBacktest adapter loops.

Extracted from adapter.py (WU-01) to eliminate 110 lines of duplication
between _run_feed() and _run_elapse().
"""

from __future__ import annotations

from importlib import metadata as importlib_metadata

import numpy as np
from structlog import get_logger

from hft_platform.events import LOBStatsEvent

logger = get_logger("hbt_adapter")
_MODERN_DEFAULT_WAIT_TIMEOUT = 10**18


# ---------------------------------------------------------------------------
# Depth validation (was duplicated at lines 220-230 / 328-338)
# ---------------------------------------------------------------------------
def validate_depth(best_bid: float, best_ask: float) -> bool:
    """Return True if bid/ask are valid for event construction."""
    return (
        best_bid == best_bid  # NaN check
        and best_ask == best_ask
        and best_bid > 0
        and best_ask < 2147483647
        and best_bid < best_ask
    )


# ---------------------------------------------------------------------------
# Attribute resolution
# ---------------------------------------------------------------------------
def resolve_qty(obj: object, *attr_names: str) -> int:
    """Return the first non-None attribute from *obj*, cast to int.

    Correctly preserves legitimate zero values (``0 is not None``).
    Falls back to ``0`` when every attribute is absent.
    """
    for name in attr_names:
        val = getattr(obj, name, None)
        if val is not None:
            return int(val)
    return 0


def call_if_exists(obj: object, method_name: str, *args: object) -> object:
    """Call *method_name* on *obj* if it exists, returning the result or *obj*."""
    method = getattr(obj, method_name, None)
    if not callable(method):
        return obj
    try:
        return method(*args)
    except Exception:
        return obj


# ---------------------------------------------------------------------------
# LOB event construction (was duplicated at lines 235-273 / 352-391)
# ---------------------------------------------------------------------------
def build_lob_event(
    adapter: object,
    dp: object,
    ts_ns: int,
    best_bid: int,
    best_ask: int,
) -> tuple[LOBStatsEvent, object | None]:
    """Build LOBStatsEvent (and optional feature event) from depth.

    Returns:
        (lob_event, feature_event) — feature_event is None unless
        feature_mode is ``"lob_feature"`` and a FeatureEngine is wired.
    """
    feature_event = None

    if adapter.feature_mode == "lob_feature" and adapter._lob_engine is not None:  # type: ignore[attr-defined]
        bidask_event = adapter._build_l1_bidask_event(dp, ts_ns)  # type: ignore[attr-defined]
        stats = adapter._lob_engine.process_event(bidask_event)  # type: ignore[attr-defined]
        if isinstance(stats, LOBStatsEvent):
            event = stats
            if adapter._feature_engine is not None:  # type: ignore[attr-defined]
                process_lob_update = getattr(adapter._feature_engine, "process_lob_update", None)  # type: ignore[attr-defined]
                if callable(process_lob_update):
                    feature_event = process_lob_update(bidask_event, stats, local_ts_ns=ts_ns)
                else:
                    feature_event = adapter._feature_engine.process_lob_stats(stats, local_ts_ns=ts_ns)  # type: ignore[attr-defined]
        else:
            event = _build_basic_lob_event(adapter.symbol, ts_ns, best_bid, best_ask, dp)  # type: ignore[attr-defined]
    else:
        event = _build_basic_lob_event(adapter.symbol, ts_ns, best_bid, best_ask, dp)  # type: ignore[attr-defined]

    return event, feature_event


def _build_basic_lob_event(
    symbol: str,
    ts_ns: int,
    best_bid: int,
    best_ask: int,
    dp: object,
) -> LOBStatsEvent:
    """Build a LOBStatsEvent from raw depth without LOBEngine."""
    bid_qty = resolve_qty(dp, "best_bid_qty", "bid_qty", "bid_volume")
    ask_qty = resolve_qty(dp, "best_ask_qty", "ask_qty", "ask_volume")
    total_qty = bid_qty + ask_qty
    imb = (bid_qty - ask_qty) / total_qty if total_qty > 0 else 0.0
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts_ns,
        imbalance=imb,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_qty,
        ask_depth=ask_qty,
    )


# ---------------------------------------------------------------------------
# Fill detection (was duplicated at lines 275-293 / 400-417)
# Now stores to SoA buffers (WU-02 Allocator Law fix)
# ---------------------------------------------------------------------------
def process_fills(adapter: object, ts_ns: int, best_bid: int, best_ask: int) -> None:
    """Detect fills, record in SoA buffers, and sample equity."""
    old_pos = adapter._prev_position  # type: ignore[attr-defined]
    adapter._sync_positions()  # type: ignore[attr-defined]
    new_pos = adapter.positions.get(adapter.symbol, 0)  # type: ignore[attr-defined]
    delta = new_pos - old_pos
    if delta != 0:
        if delta > 0:
            adapter._total_buy_fills += abs(delta)  # type: ignore[attr-defined]
        else:
            adapter._total_sell_fills += abs(delta)  # type: ignore[attr-defined]
        # mid_price_x2 = bid + ask (integer, no float division — Precision Law)
        adapter._record_fill(ts_ns, delta, new_pos, best_bid + best_ask)  # type: ignore[attr-defined]
    adapter._prev_position = new_pos  # type: ignore[attr-defined]
    adapter._maybe_record_equity_point(ts_ns, best_bid, best_ask)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Strategy dispatch (was duplicated at lines 296-305 / 421-430)
# ---------------------------------------------------------------------------
def dispatch_strategy(adapter: object, event: object, feature_event: object | None) -> None:
    """Call strategy.handle_event and execute returned intents."""
    intents = adapter.strategy.handle_event(adapter.ctx, event)  # type: ignore[attr-defined]
    if feature_event is not None and adapter.dispatch_feature_events:  # type: ignore[attr-defined]
        more = adapter.strategy.handle_event(adapter.ctx, feature_event)  # type: ignore[attr-defined]
        if more:
            intents.extend(more)
    for intent in intents:
        adapter.execute_intent(intent)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------
def detect_wait_status_mode() -> str:
    """Detect wait_next_feed status semantics.

    hftbacktest v2.4+ returns explicit status codes:
    0=timeout, 1=end, 2=feed, 3=order response.
    Older releases returned 0 on successful advancement.
    """
    try:
        version_text = importlib_metadata.version("hftbacktest")
    except Exception:
        return "legacy"

    parts = []
    for chunk in version_text.split(".")[:2]:
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 2:
        parts.append(0)

    return "modern" if tuple(parts[:2]) >= (2, 4) else "legacy"


# ---------------------------------------------------------------------------
# Tick size inference
# ---------------------------------------------------------------------------
def infer_tick_size_from_data(data_path: str) -> float:
    """Infer a positive tick size from research.npy or hftbt.npz price fields.

    Falls back to 1.0 when the source cannot be loaded or the price ladder
    does not expose a usable positive increment.
    """
    try:
        loaded = np.load(data_path, allow_pickle=False)
        try:
            if isinstance(loaded, np.lib.npyio.NpzFile):
                arr = np.asarray(loaded["data"])
            else:
                arr = np.asarray(loaded)
        finally:
            if hasattr(loaded, "close"):
                loaded.close()
    except Exception:
        return 1.0

    names = tuple(arr.dtype.names or ())
    price_fields = [name for name in ("px", "bid_px", "ask_px") if name in names]
    if not price_fields:
        return 1.0

    samples: list[np.ndarray] = []
    head = arr[: min(len(arr), 20_000)]
    for field in price_fields:
        col = np.asarray(head[field], dtype=np.float64)
        col = col[np.isfinite(col) & (col > 0.0)]
        if col.size:
            samples.append(col)
    if not samples:
        return 1.0

    prices = np.unique(np.concatenate(samples))
    if prices.size < 2:
        return 1.0

    diffs = np.diff(prices)
    diffs = diffs[np.isfinite(diffs) & (diffs > 0.0)]
    if diffs.size == 0:
        return 1.0

    return float(diffs.min())
