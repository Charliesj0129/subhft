"""Queue-fill calibration harness (Slice B Task 6).

Produces an empirical ``q_hat(symbol, hour, depth_bucket)`` table from CK-replay
events, written to parquet for consumption by ``QueueDepletionFill`` (Task 8).

Background
----------
The literal ``queue_fraction = 0.5`` baked into ``QueueDepletionFill``
(``research/backtest/fill_models.py``) is a placeholder. Real fill probability
varies systematically by hour-of-day (liquidity regime) and queue depth (one's
position in line at the best bid/ask). This harness measures the empirical
fill rate from CK-replay actual events on each ``(symbol, hour, depth_bucket)``
cell and writes a parquet that ``QHatTable.load`` can consume.

post_quote_attempt definition
-----------------------------
Plan §6 Task 5/6: a *post_quote_attempt* is "a quote was placed AND tracked
AND at least one trade arrived through the price". We do **not** have the
maker's actual placement timestamps in raw CK, so we use a forward-looking
proxy:

  For every distinct best-bid/best-ask snapshot in the event stream, we
  treat it as if a passive quote had been placed at that price on each side,
  then look at the next ``LOOKAHEAD_NS`` (1 second) of trade events to see
  whether any trade crossed through that price (counts as a fill).

This proxy is documented in ``ALGORITHM`` below and is intentionally simple:
the harness is a calibration tool, not the fill model itself, so it exists to
produce a *relative* ranking of cells. Task 8 will plug the resulting
``QHatTable`` into ``QueueDepletionFill`` where the absolute scale matters.

ALGORITHM
---------
Given a numpy structured array of hftbacktest events (see
``hft_platform.backtest.ch_data_source._event_dtype``):

1. Walk the event stream in time order. Maintain a running view of the
   current best bid and best ask (with their qty at top of book).
2. Each time the best bid OR best ask price *changes* (a new "tracked
   quote"), open one tracking record per side at that price+ts+depth_bucket.
   ``depth_bucket`` is determined by the **per-side** top-of-book quantity:
   ``depth = bid_qty`` for the bid record, ``ask_qty`` for the ask record;
   ``"shallow"`` if ``depth < SHALLOW_THRESHOLD`` (=5) else ``"deep"``.
3. As subsequent trade events arrive, mark a tracked record as "filled" if a
   trade price crossed the tracked price within ``LOOKAHEAD_NS`` of the
   tracked timestamp:
       - bid record at price P fills if any trade has price <= P.
       - ask record at price P fills if any trade has price >= P.
4. When a tracked record's ``LOOKAHEAD_NS`` window closes (or end-of-day),
   it counts as one "attempt" in cell ``(symbol, hour, bucket)``; if it was
   marked filled it also counts as one "fill".
5. ``q_hat[(symbol, hour, bucket)] = fills / attempts`` for any cell with
   ``attempts >= min_attempts`` (default 30, per plan).
6. Cells with ``attempts < min_attempts`` are *dropped* — i.e. not written
   to parquet. ``QHatTable.lookup`` will fall through to ``self.fallback``
   (=0.5) for those cells, which is the documented graceful-degradation
   policy.

Out of scope
------------
- This harness does not run any backtest engine; it consumes raw event
  arrays from a ``ChDataSourceLike`` only.
- It does not connect to live ClickHouse; the caller injects the source
  (DI established in Task 5). Tests use an in-memory fake source.
- It does not generate per-symbol fixtures; that is Task 7.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.q_hat_table import SHALLOW_THRESHOLD, QHatTable

# Replicates hft_platform.backtest.ch_data_source event-flag constants. Keeping
# them local to this module avoids pulling the runtime import path through the
# research package (which is ruff-excluded but mypy-checked) and keeps Task 6
# unit tests fully hermetic — they construct synthetic numpy arrays without
# touching the platform package.
_DEPTH_EVENT = 1
_TRADE_EVENT = 2
_DEPTH_CLEAR_EVENT = 3
_DEPTH_SNAPSHOT_EVENT = 4
_BUY_EVENT = 1 << 29
_SELL_EVENT = 1 << 28
_EV_TYPE_MASK = 0xFF

# Forward-looking window in which a trade can be matched against a tracked
# quote. 1 second is a deliberately conservative match for the maker-realism
# horizon — long enough to attribute the trade to the resting quote, short
# enough that we are not double-counting unrelated trades long after the price
# moved on. Documented in module docstring step 3.
LOOKAHEAD_NS: int = 1_000_000_000

# Minimum cell occupancy before we trust a calibration result. Plan §6 Task 6:
# "Cells with n < 30 attempts are dropped (fallback applies)." Lower-occupancy
# cells fall through to QHatTable.fallback (0.5).
MIN_ATTEMPTS_PER_CELL: int = 30


class ChDataSourceLike(Protocol):
    """Minimum surface the calibration harness needs from ``ChDataSource``.

    Defining a Protocol here lets unit tests substitute a fake source without
    importing ``src/hft_platform/backtest/ch_data_source.py`` (and thus without
    requiring ClickHouse credentials).
    """

    def load_day(self, instrument: str, date: str) -> np.ndarray: ...


@dataclass(frozen=True)
class CalibrationResult:
    """Outcome of one ``calibrate(...)`` run.

    Attributes
    ----------
    table:
        The frozen ``QHatTable`` reloaded from the parquet that was just written
        (so it reflects exactly what downstream consumers will see).
    cell_occupancy:
        ``(symbol, hour, depth_bucket) -> attempts_count`` for every cell that
        had at least one attempt. Includes dropped cells; useful for plotting
        coverage maps in Task 7.
    cells_dropped:
        Number of cells with ``attempts < min_attempts``. These cells are NOT
        written to parquet; their lookups fall through to ``QHatTable.fallback``.
    cells_calibrated:
        Number of cells with ``attempts >= min_attempts``. These cells appear
        in the parquet and ``QHatTable._data``.
    """

    table: QHatTable
    cell_occupancy: dict[tuple[str, int, str], int]
    cells_dropped: int
    cells_calibrated: int


@dataclass
class _TrackedQuote:
    """Mutable per-side tracking record used during one event walk.

    Not exported; lifetime is bounded by ``LOOKAHEAD_NS`` after which the
    record is committed to the (attempts, fills) tally and discarded.
    """

    side: str  # "bid" | "ask"
    price: float
    opened_ts_ns: int
    hour: int
    depth_bucket: str
    filled: bool = False


def _hour_of_day(ts_ns: int) -> int:
    """Hour-of-day in the system's local UTC interpretation (0-23).

    The CK timestamps are exch_ts in nanoseconds since epoch. Hour-of-day is
    derived modulo 24 hours from epoch — this matches how
    ``hft.market_data`` is partitioned and how Task 5's ``QHatTable`` is keyed.
    Calibration consistency is what matters here, not absolute clock alignment.
    """
    return int((ts_ns // 1_000_000_000) // 3600 % 24)


def _bucket_for(depth: int) -> str:
    """Map per-side top-of-book qty to the QHatTable depth bucket."""
    return "shallow" if depth < SHALLOW_THRESHOLD else "deep"


def _commit(
    record: _TrackedQuote,
    symbol: str,
    attempts: dict[tuple[str, int, str], int],
    fills: dict[tuple[str, int, str], int],
) -> None:
    """Commit one expired tracking record to the running tallies."""
    cell = (symbol, record.hour, record.depth_bucket)
    attempts[cell] += 1
    if record.filled:
        fills[cell] += 1


def _ingest_day(  # noqa: C901 - calibration walk has structural per-event branches
    symbol: str,
    events: np.ndarray,
    *,
    attempts: dict[tuple[str, int, str], int],
    fills: dict[tuple[str, int, str], int],
    lookahead_ns: int,
) -> None:
    """Walk one day's events and update ``attempts`` / ``fills`` in place.

    See module docstring ALGORITHM for the per-event semantics.
    """
    # Maintain a partial L1 view: best bid/ask price + per-side qty.
    best_bid_px: float | None = None
    best_ask_px: float | None = None
    bid_qty: int = 0
    ask_qty: int = 0

    # At most one open tracked quote per side (one quote at a time, per the
    # plan's "post a quote, watch for fills" semantics).
    open_bid: _TrackedQuote | None = None
    open_ask: _TrackedQuote | None = None

    for row in events:
        ev_flags = int(row["ev"])
        ev_type = ev_flags & _EV_TYPE_MASK
        ts_ns = int(row["exch_ts"])
        px = float(row["px"])
        qty = float(row["qty"])

        # -- Expire any tracked quote whose lookahead window has closed.
        if open_bid is not None and ts_ns - open_bid.opened_ts_ns > lookahead_ns:
            _commit(open_bid, symbol, attempts, fills)
            open_bid = None
        if open_ask is not None and ts_ns - open_ask.opened_ts_ns > lookahead_ns:
            _commit(open_ask, symbol, attempts, fills)
            open_ask = None

        if ev_type == _DEPTH_CLEAR_EVENT:
            # Snapshot reset: drop tracked quotes (their reference book is gone).
            best_bid_px = None
            best_ask_px = None
            bid_qty = 0
            ask_qty = 0
            open_bid = None
            open_ask = None
            continue

        if ev_type == _DEPTH_EVENT:
            # Update L1 view if this row is at-or-better than current best.
            if ev_flags & _BUY_EVENT:
                if qty <= 0.0:
                    # Level removed.
                    if best_bid_px is not None and abs(px - best_bid_px) < 1e-12:
                        best_bid_px = None
                        bid_qty = 0
                else:
                    if best_bid_px is None or px >= best_bid_px:
                        new_best = best_bid_px is None or px > best_bid_px + 1e-12
                        best_bid_px = px
                        bid_qty = int(qty)
                        if new_best:
                            # Best bid changed → open a new tracked record.
                            if open_bid is not None:
                                _commit(open_bid, symbol, attempts, fills)
                            open_bid = _TrackedQuote(
                                side="bid",
                                price=px,
                                opened_ts_ns=ts_ns,
                                hour=_hour_of_day(ts_ns),
                                depth_bucket=_bucket_for(bid_qty),
                            )
            elif ev_flags & _SELL_EVENT:
                if qty <= 0.0:
                    if best_ask_px is not None and abs(px - best_ask_px) < 1e-12:
                        best_ask_px = None
                        ask_qty = 0
                else:
                    if best_ask_px is None or px <= best_ask_px:
                        new_best = best_ask_px is None or px < best_ask_px - 1e-12
                        best_ask_px = px
                        ask_qty = int(qty)
                        if new_best:
                            if open_ask is not None:
                                _commit(open_ask, symbol, attempts, fills)
                            open_ask = _TrackedQuote(
                                side="ask",
                                price=px,
                                opened_ts_ns=ts_ns,
                                hour=_hour_of_day(ts_ns),
                                depth_bucket=_bucket_for(ask_qty),
                            )
            continue

        if ev_type == _TRADE_EVENT:
            # Mark fill if the trade crosses an open tracked quote.
            if open_bid is not None and px <= open_bid.price + 1e-12:
                open_bid.filled = True
            if open_ask is not None and px >= open_ask.price - 1e-12:
                open_ask.filled = True
            continue

        # _DEPTH_SNAPSHOT_EVENT and unknown types are ignored — they are not
        # part of the calibration's L1-update vocabulary.

    # End-of-day: commit any still-open tracked records so we do not lose the
    # tail of the session.
    if open_bid is not None:
        _commit(open_bid, symbol, attempts, fills)
    if open_ask is not None:
        _commit(open_ask, symbol, attempts, fills)


def calibrate(
    symbol: str,
    dates: list[str],
    out_path: Path | str,
    *,
    ch_source: ChDataSourceLike,
    min_attempts: int = MIN_ATTEMPTS_PER_CELL,
    lookahead_ns: int = LOOKAHEAD_NS,
) -> CalibrationResult:
    """Calibrate ``q_hat(symbol, hour, depth_bucket)`` from CK-replay events.

    Parameters
    ----------
    symbol:
        Symbol to calibrate (e.g. ``"TMFD6"``).
    dates:
        Trading dates (``"YYYY-MM-DD"``) to ingest.
    out_path:
        Parquet path to write. Schema is the QHatTable input contract:
        ``(symbol: string, hour: int, depth_bucket: string, q_hat: float)``.
    ch_source:
        A ``ChDataSourceLike`` providing ``load_day(symbol, date) -> np.ndarray``.
        Tests inject an in-memory fake; production injects ``ChDataSource``.
    min_attempts:
        Minimum cell occupancy before the cell is written. Cells below this
        threshold are dropped (lookups fall through to ``QHatTable.fallback``).
    lookahead_ns:
        Forward-looking window matched-trade attribution; see ALGORITHM step 3.

    Returns
    -------
    CalibrationResult with the reloaded ``QHatTable`` plus calibration metadata.
    """
    attempts: dict[tuple[str, int, str], int] = defaultdict(int)
    fills: dict[tuple[str, int, str], int] = defaultdict(int)

    for date in dates:
        events = ch_source.load_day(symbol, date)
        _ingest_day(
            symbol,
            events,
            attempts=attempts,
            fills=fills,
            lookahead_ns=lookahead_ns,
        )

    cells_calibrated = 0
    cells_dropped = 0
    records: list[dict[str, object]] = []
    for cell, n_attempts in attempts.items():
        if n_attempts < min_attempts:
            cells_dropped += 1
            continue
        cells_calibrated += 1
        sym, hour, bucket = cell
        q_hat = fills[cell] / n_attempts
        records.append(
            {
                "symbol": sym,
                "hour": int(hour),
                "depth_bucket": bucket,
                "q_hat": float(q_hat),
            }
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Empty-records case: write a parquet with the documented schema but no
    # rows. ``QHatTable.load`` will then return an empty table, and every
    # lookup falls through to ``fallback`` — the explicit graceful-degradation
    # path documented in QHatTable.
    if records:
        arrow_table = pa.Table.from_pylist(records)
    else:
        arrow_table = pa.table(
            {
                "symbol": pa.array([], type=pa.string()),
                "hour": pa.array([], type=pa.int64()),
                "depth_bucket": pa.array([], type=pa.string()),
                "q_hat": pa.array([], type=pa.float64()),
            }
        )
    pq.write_table(arrow_table, out_path)

    table = QHatTable.load(out_path)
    return CalibrationResult(
        table=table,
        cell_occupancy=dict(attempts),
        cells_dropped=cells_dropped,
        cells_calibrated=cells_calibrated,
    )
