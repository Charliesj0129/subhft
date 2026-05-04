"""Deterministic strategy replay harness for Slice C parity gate.

Drives a strategy over a market-data fixture (loaded by
:func:`hft_platform.replay.wal_fixture_loader.load_market_data_events`)
with a patched clock and seeded RNG, capturing emitted ``OrderIntent``
objects into a :class:`ReplayedIntentLog`.

Determinism contract
====================
Two replays of identical :class:`ReplayConfig` MUST produce byte-identical
:meth:`ReplayedIntentLog.hash` digests. This is achieved by:

1. Patching ``hft_platform.core.timebase.now_ns`` to a state-driven function
   so any strategy using the canonical clock observes a per-event-deterministic
   nanosecond value (the event's ``exch_ts`` unless ``clock_start_ns`` is set).
2. Constructing a seeded :class:`random.Random` and passing it to the
   strategy factory so RNG-driven branches are reproducible.
3. Bypassing :class:`hft_platform.strategy.runner.StrategyRunner` (and its
   stale-event drop logic). Instead the harness invokes
   ``strategy.handle_event(ctx, event)`` directly — this matches the
   synchronous ``BaseStrategy`` contract (``base.py:266-270``).

If a strategy uses ``time.monotonic_ns`` / ``time.time_ns`` directly
(anti-pattern under HFT Core Law §3) the determinism test will detect the
leak by emitting non-identical hashes — that's intentional evidence.
"""
from __future__ import annotations

import random
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from unittest.mock import patch

import numpy as np

from hft_platform.events import BidAskEvent, BookStats, MetaData, TickEvent
from hft_platform.replay.intent_log import ReplayedIntentLog
from hft_platform.replay.wal_fixture_loader import load_market_data_events


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """Frozen configuration for a single replay invocation.

    ``strategy_factory`` is any callable accepting a keyword ``rng=...`` and
    returning an object exposing ``handle_event(ctx, event) -> list[OrderIntent]``.
    """

    fixture_path: str
    strategy_factory: Any
    symbols: set[str] | None = None
    rng_seed: int = 0
    clock_start_ns: int | None = None
    max_events: int | None = None


@contextmanager
def _deterministic_clock() -> Iterator[dict]:
    """Patch ``hft_platform.core.timebase.now_ns`` with a state-driven mock.

    The yielded mutable dict has key ``"now"``; the harness loop updates it
    to the current event's ``exch_ts`` before invoking the strategy so the
    strategy's ``timebase.now_ns()`` calls return a replay-stable value.
    """
    state = {"now": 0}

    def _now() -> int:
        return state["now"]

    with patch("hft_platform.core.timebase.now_ns", side_effect=_now):
        yield state


def _zip_book(prices: Any, vols: Any) -> np.ndarray:
    """Build an ``(N, 2)`` int64 array from parallel price/volume lists."""
    if not prices:
        return np.empty((0, 2), dtype=np.int64)
    n = min(len(prices), len(vols)) if vols else 0
    if n == 0:
        return np.empty((0, 2), dtype=np.int64)
    out = np.empty((n, 2), dtype=np.int64)
    for i in range(n):
        out[i, 0] = int(prices[i])
        out[i, 1] = int(vols[i])
    return out


def _book_stats(bids: np.ndarray, asks: np.ndarray) -> BookStats | None:
    """Compute backward-compat ``BookStats`` from L1 of bids/asks."""
    if bids.shape[0] == 0 or asks.shape[0] == 0:
        return None
    best_bid = int(bids[0, 0])
    best_ask = int(asks[0, 0])
    bid_depth = int(bids[:, 1].sum())
    ask_depth = int(asks[:, 1].sum())
    mid_x2 = best_bid + best_ask
    spread = best_ask - best_bid
    total = bid_depth + ask_depth
    imbalance = float(bid_depth - ask_depth) / float(total) if total > 0 else 0.0
    return BookStats(
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        mid_price=mid_x2 / 2.0,
        spread=float(spread),
        imbalance=imbalance,
    )


def _market_data_row_to_event(row: dict) -> BidAskEvent | TickEvent:
    """Convert a WAL ``hft.market_data`` row dict into a typed event.

    Dispatches on the ``type`` field (``"BidAsk"`` / ``"Snapshot"`` →
    :class:`BidAskEvent`; ``"Tick"`` → :class:`TickEvent`). Falls back to
    BidAsk when ``bids_price`` is non-empty so malformed rows still produce
    a sensible event for the replay harness.

    Schema reference: ``MARKET_DATA_COLUMNS`` in
    ``src/hft_platform/recorder/worker.py`` (the WAL extractor that wrote
    these rows).
    """
    row_type = str(row.get("type") or "").strip().lower()
    exch_ts = int(row.get("exch_ts", 0) or 0)
    ingest_ts = int(row.get("ingest_ts", exch_ts) or exch_ts)
    seq = int(row.get("seq_no", row.get("seq", 0)) or 0)
    symbol = str(row.get("symbol") or "")
    meta = MetaData(seq=seq, source_ts=exch_ts, local_ts=ingest_ts, topic="market_data")

    is_book = row_type in ("bidask", "snapshot") or (
        row_type == "" and bool(row.get("bids_price"))
    )
    if is_book:
        bids = _zip_book(row.get("bids_price") or [], row.get("bids_vol") or [])
        asks = _zip_book(row.get("asks_price") or [], row.get("asks_vol") or [])
        return BidAskEvent(
            meta=meta,
            symbol=symbol,
            bids=bids,
            asks=asks,
            stats=_book_stats(bids, asks),
            is_snapshot=row_type == "snapshot",
        )

    # Default: treat as Tick.
    return TickEvent(
        meta=meta,
        symbol=symbol,
        price=int(row.get("price_scaled", 0) or 0),
        volume=int(row.get("volume", 0) or 0),
        trade_direction=int(row.get("trade_direction", 0) or 0),
    )


def replay_strategy(cfg: ReplayConfig) -> ReplayedIntentLog:
    """Replay market data through ``cfg.strategy_factory(rng=...)`` and capture intents.

    The strategy contract is the synchronous ``BaseStrategy.handle_event(ctx, event)``
    surface (``src/hft_platform/strategy/base.py:266-270``); the harness does
    not await it.

    ``ctx`` is passed as ``None`` because building a real
    :class:`hft_platform.strategy.base.StrategyContext` requires positions,
    intent factory, and price-scaler wiring that production strategies depend
    on but the parity harness does not need — the test strategy under replay
    must not exercise context-dependent helpers. (See plan §Task 4.3 note.)
    """
    rng = random.Random(cfg.rng_seed)
    log = ReplayedIntentLog()
    strategy = cfg.strategy_factory(rng=rng)
    ctx: Any = None
    n_events = 0

    with _deterministic_clock() as clock:
        for row in load_market_data_events(cfg.fixture_path, symbols=cfg.symbols):
            if cfg.max_events is not None and n_events >= cfg.max_events:
                break
            exch_ts = int(row.get("exch_ts", 0) or 0)
            # Advance the patched clock BEFORE invoking the strategy so its
            # timebase.now_ns() calls observe a deterministic value.
            clock["now"] = (
                cfg.clock_start_ns if cfg.clock_start_ns is not None else exch_ts
            )
            event = _market_data_row_to_event(row)
            intents = strategy.handle_event(ctx, event)  # SYNC contract
            for intent in intents or ():
                log.append(intent)
            n_events += 1

    log.n_events_processed = n_events
    return log
