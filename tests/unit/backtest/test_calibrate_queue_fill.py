"""Slice B Task 6 tests for the queue-fill calibration harness.

Verifies the harness recovers a known target q_hat from a synthetic event
stream within ±0.02 (the plan's tolerance), and that cells with
n < MIN_ATTEMPTS_PER_CELL are dropped (their lookups fall through to
QHatTable.fallback).

The synthetic event stream uses the same numpy structured-array layout that
ChDataSource.load_day produces (see hft_platform.backtest.ch_data_source
event_dtype): 8 fields ``(ev, exch_ts, local_ts, px, qty, order_id, ival, fval)``
with ``ev`` encoding event type in the lower byte and BUY/SELL flags in bits
29/28.

Why no real ClickHouse: the harness accepts a Protocol ``ChDataSourceLike``
so tests inject an in-memory fake source. This keeps Task 6 hermetic — Task 7
will run the harness against a real ChDataSource against committed CK events.
"""

from __future__ import annotations

import numpy as np
import pyarrow.parquet as pq

from research.backtest.calibrate_queue_fill import (
    LOOKAHEAD_NS,
    MIN_ATTEMPTS_PER_CELL,
    CalibrationResult,
    calibrate,
)

_EVENT_DTYPE = np.dtype(
    [
        ("ev", "u8"),
        ("exch_ts", "i8"),
        ("local_ts", "i8"),
        ("px", "f8"),
        ("qty", "f8"),
        ("order_id", "u8"),
        ("ival", "i8"),
        ("fval", "f8"),
    ]
)

_DEPTH_EVENT = 1
_TRADE_EVENT = 2
_DEPTH_CLEAR_EVENT = 3
_BUY_EVENT = 1 << 29
_SELL_EVENT = 1 << 28

_LOOKAHEAD_NS = LOOKAHEAD_NS


class _FakeChSource:
    """In-memory ChDataSourceLike substitute. Returns pre-built event arrays."""

    def __init__(self, events_per_date: dict[str, np.ndarray]) -> None:
        self._events = events_per_date

    def load_day(self, instrument: str, date: str) -> np.ndarray:
        return self._events.get(date, np.array([], dtype=_EVENT_DTYPE))


def _build_synthetic_events(
    *,
    target_q_hat: float,
    n_attempts: int,
    hour: int = 9,
    bid_qty: int = 3,
    ask_qty: int = 3,
    base_price: float = 100.0,
    initial_spread: float = 100.0,
    price_step: float = 0.001,
) -> np.ndarray:
    """Generate ``n_attempts`` distinct best-bid/ask snapshots.

    Each attempt block opens one tracked-bid and one tracked-ask record at
    a fresh price level. For a fraction ``target_q_hat`` of the blocks, a
    follow-up trade is placed inside the lookahead window that crosses the
    bid (so the bid record fills); for the remainder, a trade is placed
    strictly between bid and ask (no fill on either side).

    Aggregate q_hat for the resulting (symbol, hour, "shallow") cell is:
        (n_fills_bid + n_fills_ask) / (n_attempts_bid + n_attempts_ask)
        = round(target_q_hat * n_attempts) / (2 * n_attempts)

    All events are stamped at ``hour`` so every attempt lands in the same
    cell when bid_qty < SHALLOW_THRESHOLD.
    """
    base_epoch_s = 1_772_582_400 + hour * 3600
    base_epoch_ns = base_epoch_s * 1_000_000_000

    n_fills = round(target_q_hat * n_attempts)
    fill_set = set(range(n_fills))

    block_stride_ns = 2 * _LOOKAHEAD_NS

    rows: list[tuple] = []
    rows.append(
        (
            _DEPTH_CLEAR_EVENT,
            base_epoch_ns - 1,
            base_epoch_ns - 1,
            0.0,
            0.0,
            0,
            0,
            0.0,
        )
    )
    for i in range(n_attempts):
        ts_block = base_epoch_ns + i * block_stride_ns
        bid_px = base_price + price_step * i
        ask_px = base_price + initial_spread - price_step * i

        rows.append(
            (
                _DEPTH_EVENT | _BUY_EVENT,
                ts_block,
                ts_block,
                bid_px,
                float(bid_qty),
                0,
                0,
                0.0,
            )
        )
        rows.append(
            (
                _DEPTH_EVENT | _SELL_EVENT,
                ts_block,
                ts_block,
                ask_px,
                float(ask_qty),
                0,
                0,
                0.0,
            )
        )

        trade_ts = ts_block + _LOOKAHEAD_NS // 2
        if i in fill_set:
            trade_px = bid_px
        else:
            trade_px = (bid_px + ask_px) / 2.0
        rows.append(
            (
                _TRADE_EVENT,
                trade_ts,
                trade_ts,
                trade_px,
                1.0,
                0,
                0,
                0.0,
            )
        )

    return np.array(rows, dtype=_EVENT_DTYPE)


def test_calibration_recovers_known_q_hat_within_tolerance(tmp_path) -> None:
    target_bid_only = 0.42
    n_attempts = 200
    events = _build_synthetic_events(
        target_q_hat=target_bid_only,
        n_attempts=n_attempts,
        hour=9,
        bid_qty=3,
        ask_qty=3,
    )
    fake = _FakeChSource(events_per_date={"2026-03-01": events})

    out = tmp_path / "tmfd6_q_hat.parquet"

    result = calibrate(
        symbol="TMFD6",
        dates=["2026-03-01"],
        out_path=out,
        ch_source=fake,
    )

    assert isinstance(result, CalibrationResult)
    assert out.exists()

    expected = round(target_bid_only * n_attempts) / (2 * n_attempts)
    q_hat = result.table.lookup("TMFD6", 9, depth=3)
    assert abs(q_hat - expected) <= 0.02, f"calibrated q_hat={q_hat} differs from expected={expected} by > 0.02"
    assert result.cells_calibrated >= 1
    arrow_table = pq.read_table(out)
    rows = arrow_table.to_pylist()
    assert len(rows) == result.cells_calibrated
    cell_row = next(r for r in rows if r["depth_bucket"] == "shallow" and r["hour"] == 9)
    assert cell_row["symbol"] == "TMFD6"
    assert abs(float(cell_row["q_hat"]) - expected) <= 0.02


def test_calibration_drops_cells_below_min_attempts(tmp_path) -> None:
    events_a = _build_synthetic_events(target_q_hat=0.5, n_attempts=10, hour=9, base_price=100.0)
    events_b = _build_synthetic_events(target_q_hat=0.5, n_attempts=50, hour=10, base_price=200.0)
    events = np.concatenate([events_a, events_b])
    fake = _FakeChSource(events_per_date={"2026-03-01": events})

    out = tmp_path / "tmfd6_q_hat.parquet"

    result = calibrate(
        symbol="TMFD6",
        dates=["2026-03-01"],
        out_path=out,
        ch_source=fake,
    )

    assert result.cells_dropped == 1, (
        f"expected 1 dropped cell (hour=9 shallow), got {result.cells_dropped}; occupancy={result.cell_occupancy}"
    )
    assert result.cells_calibrated == 1, (
        f"expected 1 calibrated cell (hour=10 shallow), got {result.cells_calibrated}; "
        f"occupancy={result.cell_occupancy}"
    )

    assert result.table.lookup("TMFD6", 9, depth=3) == 0.5
    expected = round(0.5 * 50) / (2 * 50)
    assert abs(result.table.lookup("TMFD6", 10, depth=3) - expected) <= 0.02

    assert result.cell_occupancy[("TMFD6", 9, "shallow")] == 20
    assert result.cell_occupancy[("TMFD6", 10, "shallow")] == 100

    assert MIN_ATTEMPTS_PER_CELL == 30
