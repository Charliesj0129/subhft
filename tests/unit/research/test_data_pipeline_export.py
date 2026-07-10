from __future__ import annotations

import json

import numpy as np

from research.data_pipeline import TICK_DTYPE, _write_day_outputs, rows_to_l2_and_ticks
from src.hft_platform.backtest.ch_data_source import (
    BUY_EVENT,
    DEPTH_CLEAR_EVENT,
    DEPTH_EVENT,
    EV_TYPE_MASK,
    SELL_EVENT,
    TRADE_EVENT,
)


def test_rows_to_l2_and_ticks_exports_snapshot_depth_trade_and_tick_side():
    rows = [
        ("BidAsk", 10, 11, [100_000_000, 99_000_000], [101_000_000, 102_000_000], [3, 2], [4, 1], 0, 0),
        ("Tick", 12, 13, [], [], [], [], 101_000_000, 5),
        ("BidAsk", 20, 21, [100_000_000, 98_000_000], [101_000_000, 102_000_000], [2, 1], [4, 1], 0, 0),
        ("Tick", 22, 23, [], [], [], [], 100_000_000, 6),
    ]

    events, ticks, dedup_removed = rows_to_l2_and_ticks(rows)

    assert dedup_removed == 0
    assert ticks.dtype == TICK_DTYPE
    assert ticks.tolist() == [(12, 13, 101.0, 101_000_000, 5.0, 1), (22, 23, 100.0, 100_000_000, 6.0, -1)]
    ev_types = events["ev"] & EV_TYPE_MASK
    assert ev_types[0] == DEPTH_CLEAR_EVENT
    assert np.any(ev_types == DEPTH_EVENT)
    assert np.sum(ev_types == TRADE_EVENT) == 2
    trade_events = events[ev_types == TRADE_EVENT]
    assert bool(trade_events[0]["ev"] & BUY_EVENT)
    assert bool(trade_events[1]["ev"] & SELL_EVENT)


def test_rows_to_l2_and_ticks_dedups_identical_bidask_within_window():
    rows = [
        ("BidAsk", 10, 10, [100_000_000], [101_000_000], [3], [4], 0, 0),
        ("BidAsk", 100, 100, [100_000_000], [101_000_000], [3], [4], 0, 0),
        ("Tick", 200, 200, [], [], [], [], 101_000_000, 1),
    ]

    events, ticks, dedup_removed = rows_to_l2_and_ticks(rows)

    assert dedup_removed == 1
    assert len(ticks) == 1
    assert len(events) == 4


def test_write_day_outputs_writes_contract_sidecars(tmp_path):
    rows = [
        ("BidAsk", 10, 10, [100_000_000], [101_000_000], [3], [4], 0, 0),
        ("BidAsk", 15, 15, [100_000_000], [101_000_000], [2], [4], 0, 0),
        ("Tick", 20, 20, [], [], [], [], 101_000_000, 2),
    ]
    events, ticks, dedup_removed = rows_to_l2_and_ticks(rows)

    out = _write_day_outputs(
        symbol="TMFF6",
        date="2026-06-03",
        out_dir=tmp_path,
        events=events,
        ticks=ticks,
        dedup_removed=dedup_removed,
        owner="test",
        overwrite=False,
    )

    assert out["status"] == "exported"
    l2_meta = json.loads((tmp_path / "tmff6" / "TMFF6_2026-06-03_l2.hftbt.npz.meta.json").read_text())
    tick_meta = json.loads((tmp_path / "tmff6" / "TMFF6_2026-06-03_ticks.npy.meta.json").read_text())
    assert l2_meta["generator"] == "research.data_pipeline.export_l2_ticks"
    assert l2_meta["data_kind"] == "l2_hftbacktest"
    assert tick_meta["data_kind"] == "tick"
    assert l2_meta["rows"] == len(events)
    assert tick_meta["rows"] == len(ticks)
