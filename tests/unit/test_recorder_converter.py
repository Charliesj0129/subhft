"""Unit tests for hft_platform.recorder.converter module.

Covers WALConverter, _normalize_date_str, and _date_bounds_ns.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

from hft_platform.recorder.converter import (
    WALConverter,
    _date_bounds_ns,
    _normalize_date_str,
)

# ---------------------------------------------------------------------------
# _normalize_date_str
# ---------------------------------------------------------------------------


class TestNormalizeDateStr:
    """Tests for date string normalization helper."""

    def test_empty_string(self) -> None:
        assert _normalize_date_str("") == ""

    def test_today_returns_current_date(self) -> None:
        result = _normalize_date_str("today")
        # Should be YYYY-MM-DD format
        assert len(result) == 10
        assert result[4] == "-" and result[7] == "-"

    def test_already_hyphenated(self) -> None:
        assert _normalize_date_str("2026-03-15") == "2026-03-15"

    def test_compact_8digit(self) -> None:
        assert _normalize_date_str("20260315") == "2026-03-15"

    def test_passthrough_other_formats(self) -> None:
        assert _normalize_date_str("abc") == "abc"


# ---------------------------------------------------------------------------
# _date_bounds_ns
# ---------------------------------------------------------------------------


class TestDateBoundsNs:
    """Tests for date-to-nanosecond-range conversion."""

    def test_valid_date(self) -> None:
        bounds = _date_bounds_ns("2026-01-01")
        assert bounds is not None
        start_ns, end_ns = bounds
        assert end_ns - start_ns == 86_400 * 1_000_000_000  # one day in ns

    def test_compact_date(self) -> None:
        bounds = _date_bounds_ns("20260101")
        assert bounds is not None
        start_ns, end_ns = bounds
        assert end_ns > start_ns

    def test_empty_date(self) -> None:
        assert _date_bounds_ns("") is None

    def test_invalid_date(self) -> None:
        assert _date_bounds_ns("not-a-date") is None

    def test_bounds_are_consistent(self) -> None:
        b1 = _date_bounds_ns("2026-03-15")
        b2 = _date_bounds_ns("20260315")
        assert b1 == b2


# ---------------------------------------------------------------------------
# WALConverter
# ---------------------------------------------------------------------------


class TestWALConverter:
    """Tests for JSONL WAL to NPZ conversion."""

    def _write_jsonl(self, path: str, rows: list[dict]) -> None:
        """Helper to write JSONL test data."""
        with open(path, "w") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def test_convert_basic(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)

        # Use timestamps within 2026-01-01 UTC day
        bounds = _date_bounds_ns("2026-01-01")
        assert bounds is not None
        day_start = bounds[0]

        # Create test WAL file
        rows = [
            {
                "symbol": "2330",
                "type": "trade",
                "exch_ts": day_start + 1000,
                "ingest_ts": day_start + 1100,
                "price": 580_0000,
                "volume": 10,
            },
            {
                "symbol": "2330",
                "type": "book",
                "exch_ts": day_start + 2000,
                "ingest_ts": day_start + 2100,
                "price": 0,
                "volume": 0,
                "bids_price": [579_0000, 578_0000],
                "bids_vol": [50, 100],
                "asks_price": [580_0000, 581_0000],
                "asks_vol": [30, 60],
            },
        ]
        self._write_jsonl(os.path.join(wal_dir, "market_data_20260101.jsonl"), rows)

        converter = WALConverter(wal_dir, out_dir)
        converter.convert("20260101", "2330")

        out_path = os.path.join(out_dir, "2330_20260101.npz")
        assert os.path.exists(out_path)

        loaded = np.load(out_path, allow_pickle=True)
        data = loaded["data"]
        assert len(data) == 2
        # First row: trade event (ev=1)
        assert data[0]["ev"] == 1
        assert data[0]["price"] == 580_0000
        assert data[0]["qty"] == 10
        # Second row: book event (ev=2)
        assert data[1]["ev"] == 2
        assert data[1]["bid_p_0"] == 579_0000
        assert data[1]["ask_p_0"] == 580_0000

    def test_convert_sorts_by_exch_ts(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)

        rows = [
            {"symbol": "2330", "type": "trade", "exch_ts": 300, "ingest_ts": 0, "price": 1, "volume": 1},
            {"symbol": "2330", "type": "trade", "exch_ts": 100, "ingest_ts": 0, "price": 2, "volume": 2},
            {"symbol": "2330", "type": "trade", "exch_ts": 200, "ingest_ts": 0, "price": 3, "volume": 3},
        ]
        self._write_jsonl(os.path.join(wal_dir, "market_data.jsonl"), rows)

        converter = WALConverter(wal_dir, out_dir)
        converter.convert("", "2330")

        out_path = os.path.join(out_dir, "2330_.npz")
        loaded = np.load(out_path, allow_pickle=True)
        data = loaded["data"]
        assert list(data["exch_ts"]) == [100, 200, 300]

    def test_convert_filters_by_symbol(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)

        rows = [
            {"symbol": "2330", "type": "trade", "exch_ts": 100, "price": 1, "volume": 1},
            {"symbol": "2454", "type": "trade", "exch_ts": 200, "price": 2, "volume": 2},
            {"symbol": "2330", "type": "trade", "exch_ts": 300, "price": 3, "volume": 3},
        ]
        self._write_jsonl(os.path.join(wal_dir, "market_data.jsonl"), rows)

        converter = WALConverter(wal_dir, out_dir)
        converter.convert("", "2330")

        out_path = os.path.join(out_dir, "2330_.npz")
        loaded = np.load(out_path, allow_pickle=True)
        data = loaded["data"]
        assert len(data) == 2

    def test_convert_no_symbol_filter(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)

        rows = [
            {"symbol": "2330", "type": "trade", "exch_ts": 100, "price": 1, "volume": 1},
            {"symbol": "2454", "type": "trade", "exch_ts": 200, "price": 2, "volume": 2},
        ]
        self._write_jsonl(os.path.join(wal_dir, "market_data.jsonl"), rows)

        converter = WALConverter(wal_dir, out_dir)
        converter.convert("")  # no symbol

        out_path = os.path.join(out_dir, "full_.npz")
        loaded = np.load(out_path, allow_pickle=True)
        data = loaded["data"]
        assert len(data) == 2

    def test_convert_filters_by_date(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)

        # 2026-01-01 00:00:00 UTC in nanoseconds
        bounds = _date_bounds_ns("2026-01-01")
        assert bounds is not None
        start_ns, end_ns = bounds

        rows = [
            {"symbol": "2330", "type": "trade", "exch_ts": start_ns + 1000, "price": 1, "volume": 1},
            {"symbol": "2330", "type": "trade", "exch_ts": end_ns + 1000, "price": 2, "volume": 2},  # next day
        ]
        self._write_jsonl(os.path.join(wal_dir, "market_data.jsonl"), rows)

        converter = WALConverter(wal_dir, out_dir)
        converter.convert("2026-01-01", "2330")

        out_path = os.path.join(out_dir, "2330_2026-01-01.npz")
        loaded = np.load(out_path, allow_pickle=True)
        data = loaded["data"]
        assert len(data) == 1
        assert data[0]["exch_ts"] == start_ns + 1000

    def test_convert_no_data_returns_none(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)

        # Create empty WAL file
        self._write_jsonl(os.path.join(wal_dir, "market_data.jsonl"), [])

        converter = WALConverter(wal_dir, out_dir)
        result = converter.convert("20260101", "NOSYM")
        assert result is None
        assert not os.path.exists(out_dir)

    def test_convert_corrupt_lines_skipped(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)

        fpath = os.path.join(wal_dir, "market_data.jsonl")
        with open(fpath, "w") as f:
            f.write("not valid json\n")
            f.write(json.dumps({"symbol": "2330", "type": "trade", "exch_ts": 100, "price": 1, "volume": 1}) + "\n")
            f.write("{broken\n")

        converter = WALConverter(wal_dir, out_dir)
        converter.convert("", "2330")

        out_path = os.path.join(out_dir, "2330_.npz")
        loaded = np.load(out_path, allow_pickle=True)
        data = loaded["data"]
        assert len(data) == 1

    def test_convert_l2_depth_filled(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)

        rows = [
            {
                "symbol": "2330",
                "type": "book",
                "exch_ts": 100,
                "ingest_ts": 200,
                "price": 0,
                "volume": 0,
                "bids_price": [100, 99, 98, 97, 96],
                "bids_vol": [10, 20, 30, 40, 50],
                "asks_price": [101, 102, 103, 104, 105],
                "asks_vol": [5, 15, 25, 35, 45],
            }
        ]
        self._write_jsonl(os.path.join(wal_dir, "market_data.jsonl"), rows)

        converter = WALConverter(wal_dir, out_dir)
        converter.convert("", "2330")

        out_path = os.path.join(out_dir, "2330_.npz")
        loaded = np.load(out_path, allow_pickle=True)
        data = loaded["data"]

        for lvl in range(5):
            assert data[0][f"bid_p_{lvl}"] == 100 - lvl
            assert data[0][f"ask_p_{lvl}"] == 101 + lvl

    def test_convert_metadata_included(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)

        rows = [{"symbol": "2330", "type": "trade", "exch_ts": 100, "price": 1, "volume": 1}]
        self._write_jsonl(os.path.join(wal_dir, "market_data.jsonl"), rows)

        converter = WALConverter(wal_dir, out_dir)
        converter.convert("", "2330")

        out_path = os.path.join(out_dir, "2330_.npz")
        loaded = np.load(out_path, allow_pickle=True)
        metadata_str = str(loaded["metadata"])
        metadata = json.loads(metadata_str)
        assert metadata["rows"] == 1
        assert "config_hash" in metadata
        assert "created_at" in metadata
        assert metadata["seed"] == 42

    def test_convert_creates_output_dir(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "output" / "nested" / "dir")
        os.makedirs(wal_dir)

        rows = [{"symbol": "X", "type": "trade", "exch_ts": 100, "price": 1, "volume": 1}]
        self._write_jsonl(os.path.join(wal_dir, "market_data.jsonl"), rows)

        converter = WALConverter(wal_dir, out_dir)
        converter.convert("", "X")
        assert os.path.isdir(out_dir)

    def test_convert_no_wal_files(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)
        # No files at all
        converter = WALConverter(wal_dir, out_dir)
        result = converter.convert("20260101", "2330")
        assert result is None

    def test_convert_secondary_sort_by_seq_no(self, tmp_path: pytest.TempPathFactory) -> None:
        wal_dir = str(tmp_path / "wal")
        out_dir = str(tmp_path / "out")
        os.makedirs(wal_dir)

        rows = [
            {"symbol": "2330", "type": "trade", "exch_ts": 100, "seq_no": 2, "price": 20, "volume": 1},
            {"symbol": "2330", "type": "trade", "exch_ts": 100, "seq_no": 1, "price": 10, "volume": 1},
        ]
        self._write_jsonl(os.path.join(wal_dir, "market_data.jsonl"), rows)

        converter = WALConverter(wal_dir, out_dir)
        converter.convert("", "2330")

        out_path = os.path.join(out_dir, "2330_.npz")
        loaded = np.load(out_path, allow_pickle=True)
        data = loaded["data"]
        # seq_no=1 should come first
        assert data[0]["price"] == 10
        assert data[1]["price"] == 20
