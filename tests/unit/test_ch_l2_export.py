"""Tests for research/tools/ch_l2_export.py — ClickHouse L2 depth exporter."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from research.tools.ch_l2_export import (
    _arrays_equal,
    _bidask_to_depth_events,
    _build_event_dtype,
    _dedup_bidask,
    _event_flags,
    _parse_date,
    _tick_to_trade_event,
    convert_rows_to_events,
    write_meta_json,
    write_npz,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bidask(
    ingest_ts: int,
    bids_price: list[int] | None = None,
    bids_vol: list[int] | None = None,
    asks_price: list[int] | None = None,
    asks_vol: list[int] | None = None,
    seq_no: int = 1,
) -> dict[str, Any]:
    return {
        "symbol": "TEST",
        "type": "BidAsk",
        "ingest_ts": ingest_ts,
        "price_scaled": 0,
        "volume": 0,
        "bids_price": bids_price or [100_000_000, 99_000_000, 98_000_000, 97_000_000, 96_000_000],
        "bids_vol": bids_vol or [10, 20, 30, 40, 50],
        "asks_price": asks_price or [101_000_000, 102_000_000, 103_000_000, 104_000_000, 105_000_000],
        "asks_vol": asks_vol or [5, 15, 25, 35, 45],
        "seq_no": seq_no,
    }


def _make_tick(
    ingest_ts: int,
    price_scaled: int = 100_500_000,
    volume: int = 3,
    seq_no: int = 2,
) -> dict[str, Any]:
    return {
        "symbol": "TEST",
        "type": "Tick",
        "ingest_ts": ingest_ts,
        "price_scaled": price_scaled,
        "volume": volume,
        "bids_price": [],
        "bids_vol": [],
        "asks_price": [],
        "asks_vol": [],
        "seq_no": seq_no,
    }


# ---------------------------------------------------------------------------
# Tests: dedup
# ---------------------------------------------------------------------------


class TestDedupBidask:
    def test_empty(self) -> None:
        result, removed = _dedup_bidask([])
        assert result == []
        assert removed == 0

    def test_single_row(self) -> None:
        rows = [_make_bidask(1000)]
        result, removed = _dedup_bidask(rows)
        assert len(result) == 1
        assert removed == 0

    def test_identical_within_window_removed(self) -> None:
        ts = 1_000_000_000
        rows = [
            _make_bidask(ts, seq_no=1),
            _make_bidask(ts + 100_000, seq_no=2),  # +0.1ms < 0.5ms
        ]
        result, removed = _dedup_bidask(rows)
        assert len(result) == 1
        assert removed == 1

    def test_identical_outside_window_kept(self) -> None:
        ts = 1_000_000_000
        rows = [
            _make_bidask(ts, seq_no=1),
            _make_bidask(ts + 600_000, seq_no=2),  # +0.6ms > 0.5ms
        ]
        result, removed = _dedup_bidask(rows)
        assert len(result) == 2
        assert removed == 0

    def test_different_prices_within_window_kept(self) -> None:
        ts = 1_000_000_000
        rows = [
            _make_bidask(ts, bids_price=[100_000_000, 0, 0, 0, 0], seq_no=1),
            _make_bidask(ts + 100_000, bids_price=[101_000_000, 0, 0, 0, 0], seq_no=2),
        ]
        result, removed = _dedup_bidask(rows)
        assert len(result) == 2
        assert removed == 0

    def test_triple_dedup(self) -> None:
        ts = 1_000_000_000
        rows = [
            _make_bidask(ts, seq_no=1),
            _make_bidask(ts + 100_000, seq_no=2),
            _make_bidask(ts + 200_000, seq_no=3),
        ]
        result, removed = _dedup_bidask(rows)
        assert len(result) == 1
        assert removed == 2


# ---------------------------------------------------------------------------
# Tests: BidAsk → DEPTH_EVENTs
# ---------------------------------------------------------------------------


class TestBidaskToDepthEvents:
    def test_full_5_levels_produces_10_events(self) -> None:
        row = _make_bidask(1_000_000_000)
        flags = _event_flags()
        dtype = _build_event_dtype()
        events = _bidask_to_depth_events(
            row, is_snapshot=True, flags=flags, evt_dtype=dtype
        )
        assert len(events) == 10

    def test_snapshot_flags_on_first_row(self) -> None:
        row = _make_bidask(1_000_000_000)
        flags = _event_flags()
        dtype = _build_event_dtype()
        events = _bidask_to_depth_events(
            row, is_snapshot=True, flags=flags, evt_dtype=dtype
        )
        # First 5 should be snapshot bid, next 5 snapshot ask
        for i in range(5):
            assert int(events[i]["ev"]) == flags["DEPTH_SNAPSHOT_BID"]
        for i in range(5, 10):
            assert int(events[i]["ev"]) == flags["DEPTH_SNAPSHOT_ASK"]

    def test_depth_flags_on_subsequent_rows(self) -> None:
        row = _make_bidask(2_000_000_000)
        flags = _event_flags()
        dtype = _build_event_dtype()
        events = _bidask_to_depth_events(
            row, is_snapshot=False, flags=flags, evt_dtype=dtype
        )
        for i in range(5):
            assert int(events[i]["ev"]) == flags["DEPTH_BID"]
        for i in range(5, 10):
            assert int(events[i]["ev"]) == flags["DEPTH_ASK"]

    def test_price_scaling(self) -> None:
        row = _make_bidask(
            1_000_000_000,
            bids_price=[33121_000_000, 0, 0, 0, 0],
            bids_vol=[1, 0, 0, 0, 0],
            asks_price=[33288_000_000, 0, 0, 0, 0],
            asks_vol=[1, 0, 0, 0, 0],
        )
        flags = _event_flags()
        dtype = _build_event_dtype()
        events = _bidask_to_depth_events(
            row, is_snapshot=True, flags=flags, evt_dtype=dtype
        )
        # bid price: 33121_000_000 / 1e6 = 33121.0
        assert events[0]["px"] == pytest.approx(33121.0)
        # ask price: 33288_000_000 / 1e6 = 33288.0
        assert events[1]["px"] == pytest.approx(33288.0)

    def test_skip_zero_price_levels(self) -> None:
        row = _make_bidask(
            1_000_000_000,
            bids_price=[100_000_000, 0, 0, 0, 0],
            bids_vol=[10, 0, 0, 0, 0],
            asks_price=[101_000_000, 102_000_000, 0, 0, 0],
            asks_vol=[5, 15, 0, 0, 0],
        )
        flags = _event_flags()
        dtype = _build_event_dtype()
        events = _bidask_to_depth_events(
            row, is_snapshot=False, flags=flags, evt_dtype=dtype
        )
        # 1 bid level + 2 ask levels = 3
        assert len(events) == 3

    def test_empty_depth(self) -> None:
        row = _make_bidask(
            1_000_000_000,
            bids_price=[0, 0, 0, 0, 0],
            bids_vol=[0, 0, 0, 0, 0],
            asks_price=[0, 0, 0, 0, 0],
            asks_vol=[0, 0, 0, 0, 0],
        )
        flags = _event_flags()
        dtype = _build_event_dtype()
        events = _bidask_to_depth_events(
            row, is_snapshot=False, flags=flags, evt_dtype=dtype
        )
        assert len(events) == 0


# ---------------------------------------------------------------------------
# Tests: Tick → TRADE_EVENT
# ---------------------------------------------------------------------------


class TestTickToTradeEvent:
    def test_single_trade(self) -> None:
        row = _make_tick(1_000_000_000, price_scaled=33150_000_000, volume=5)
        flags = _event_flags()
        dtype = _build_event_dtype()
        events = _tick_to_trade_event(row, flags=flags, evt_dtype=dtype)
        assert len(events) == 1
        assert int(events[0]["ev"]) == flags["TRADE"]
        assert events[0]["px"] == pytest.approx(33150.0)
        assert events[0]["qty"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Tests: full pipeline (convert_rows_to_events)
# ---------------------------------------------------------------------------


class TestConvertRowsToEvents:
    def test_mixed_bidask_and_tick(self) -> None:
        ts = 1_000_000_000
        rows = [
            _make_bidask(ts, seq_no=1),
            _make_tick(ts + 1_000_000, seq_no=2),
            _make_bidask(
                ts + 2_000_000, seq_no=3,
                bids_price=[200_000_000, 199_000_000, 198_000_000, 197_000_000, 196_000_000],
            ),
        ]
        events, stats = convert_rows_to_events(rows)
        # First BidAsk: 10 snapshot events, Tick: 1 trade, Second BidAsk: 10 depth
        assert stats["total_events"] == 21
        assert stats["original_bidask_rows"] == 2
        assert stats["original_tick_rows"] == 1
        assert stats["dedup_removed"] == 0
        assert len(events) == 21

    def test_no_bidask(self) -> None:
        rows = [_make_tick(1_000_000_000)]
        events, stats = convert_rows_to_events(rows)
        assert stats["total_events"] == 1
        assert stats["original_bidask_rows"] == 0

    def test_no_tick(self) -> None:
        rows = [_make_bidask(1_000_000_000)]
        events, stats = convert_rows_to_events(rows)
        assert stats["total_events"] == 10
        assert stats["original_tick_rows"] == 0

    def test_empty_rows(self) -> None:
        events, stats = convert_rows_to_events([])
        assert len(events) == 0
        assert stats["total_events"] == 0

    def test_dedup_in_pipeline(self) -> None:
        ts = 1_000_000_000
        rows = [
            _make_bidask(ts, seq_no=1),
            _make_bidask(ts + 100_000, seq_no=2),  # duplicate within 0.5ms
        ]
        events, stats = convert_rows_to_events(rows)
        assert stats["dedup_removed"] == 1
        assert stats["total_events"] == 10  # only first row kept


# ---------------------------------------------------------------------------
# Tests: write helpers
# ---------------------------------------------------------------------------


class TestWriteNpz:
    def test_write_and_read(self, tmp_path: Path) -> None:
        dtype = _build_event_dtype()
        events = np.zeros(3, dtype=dtype)
        for i, (ev, ts, px, qty) in enumerate([(1, 100, 10.0, 1.0), (2, 200, 20.0, 2.0), (3, 300, 30.0, 3.0)]):
            events[i]["ev"] = ev
            events[i]["exch_ts"] = ts
            events[i]["local_ts"] = ts
            events[i]["px"] = px
            events[i]["qty"] = qty

        out = tmp_path / "test.npz"
        write_npz(events, out)
        assert out.exists()

        loaded = np.load(str(out))
        assert "data" in loaded
        assert len(loaded["data"]) == 3


class TestWriteMetaJson:
    def test_meta_fields(self, tmp_path: Path) -> None:
        dtype = _build_event_dtype()
        events = np.zeros(5, dtype=dtype)
        npz_path = tmp_path / "TEST_20260306.npz"
        np.savez_compressed(str(npz_path), data=events)

        meta_path = write_meta_json(
            npz_path,
            symbol="TEST",
            date_str="2026-03-06",
            stats={
                "total_events": 5,
                "dedup_removed": 2,
                "original_bidask_rows": 10,
                "original_tick_rows": 3,
            },
        )
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["dataset_id"] == "TEST_20260306"
        assert meta["source_type"] == "real"
        assert meta["data_ul"] == 5
        assert meta["depth_levels"] == 5
        assert meta["symbol"] == "TEST"
        assert meta["dedup_removed"] == 2
        assert meta["data_fingerprint"]
        assert meta["generator_script"] == "ch_l2_export.py"


# ---------------------------------------------------------------------------
# Tests: utilities
# ---------------------------------------------------------------------------


class TestParseDate:
    def test_valid(self) -> None:
        assert _parse_date("2026-03-06") == 20260306

    def test_invalid(self) -> None:
        with pytest.raises(ValueError):
            _parse_date("20260306")


class TestArraysEqual:
    def test_equal(self) -> None:
        assert _arrays_equal([1, 2, 3], [1, 2, 3]) is True

    def test_not_equal(self) -> None:
        assert _arrays_equal([1, 2, 3], [1, 2, 4]) is False

    def test_different_length(self) -> None:
        assert _arrays_equal([1, 2], [1, 2, 3]) is False
