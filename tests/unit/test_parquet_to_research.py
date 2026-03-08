"""Unit tests for research/tools/parquet_to_research.py."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

# Import under test
from research.tools.legacy.parquet_to_research import (
    DefectStats,
    _RESEARCH_DTYPE,
    _col_val,
    _col_val_str,
    _write_defect_report,
    _write_meta,
    _write_research_npy,
    convert_symbol,
    detect_columns,
)


# ---------------------------------------------------------------------------
# Minimal DataFrame stub (avoids pandas dependency in tests)
# ---------------------------------------------------------------------------
class _Row:
    """Simple row object mimicking pandas namedtuple row.

    NOTE: __getattr__ must raise AttributeError for missing keys so that
    getattr(row, col, default) works correctly.
    """
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __getattr__(self, name):
        # Only called for names NOT in __dict__
        raise AttributeError(name)


def _make_df_stub(rows: list[dict]) -> list[_Row]:
    return [_Row(**r) for r in rows]


def _stub_itertuples(df_stub):
    return iter(df_stub)


# ---------------------------------------------------------------------------
# _col_val / _col_val_str
# ---------------------------------------------------------------------------
class TestColVal:
    def test_col_val_missing_col(self):
        row = _Row(a=1.0)
        assert _col_val(row, None, 99.0) == 99.0

    def test_col_val_existing(self):
        row = _Row(price=12345.0)
        assert _col_val(row, "price") == pytest.approx(12345.0)

    def test_col_val_nan(self):
        import math
        row = _Row(price=float("nan"))
        assert _col_val(row, "price", -1.0) == pytest.approx(-1.0)

    def test_col_val_str(self):
        row = _Row(event_type="BidAsk")
        assert _col_val_str(row, "event_type") == "BidAsk"

    def test_col_val_str_missing(self):
        row = _Row()
        assert _col_val_str(row, "missing", "default") == "default"


# ---------------------------------------------------------------------------
# detect_columns
# ---------------------------------------------------------------------------
class TestDetectColumns:
    def test_detects_standard_columns(self):
        cols = ["symbol", "exch_ts", "type", "bid_price", "ask_price",
                "bid_qty", "ask_qty", "price", "volume"]
        mapping = detect_columns(cols)
        assert mapping["exch_ts"] == "exch_ts"
        assert mapping["type"] == "type"
        assert mapping["symbol"] == "symbol"
        assert mapping["bid_px"] == "bid_price"
        assert mapping["ask_px"] == "ask_price"

    def test_detects_alternate_names(self):
        cols = ["code", "ts", "msg_type", "best_bid", "best_ask"]
        mapping = detect_columns(cols)
        assert mapping["exch_ts"] == "ts"
        assert mapping["symbol"] == "code"
        assert mapping["bid_px"] == "best_bid"

    def test_raises_without_timestamp(self):
        with pytest.raises(ValueError, match="timestamp"):
            detect_columns(["symbol", "bid_price"])


# ---------------------------------------------------------------------------
# convert_symbol — BidAsk repair rules
# ---------------------------------------------------------------------------
class TestConvertSymbolBidAsk:
    """Test defect repair rules for BidAsk events."""

    _BASE_COLUMNS = {
        "type": "type",
        "symbol": "symbol",
        "exch_ts": "exch_ts",
        "bid_px": "bid_price",
        "ask_px": "ask_price",
        "bid_qty": "bid_qty",
        "ask_qty": "ask_qty",
        "price": "price",
        "volume": "volume",
    }

    def _run(self, rows):
        df = _make_df_stub(rows)
        # Monkey-patch to use our stub iteration
        import research.tools.legacy.parquet_to_research as mod
        orig_iter = mod.convert_symbol.__code__
        # Use convert_symbol directly with stub rows via a wrapper
        return _convert_with_stub(df, self._BASE_COLUMNS)

    def test_valid_bidask_passes_through(self):
        rows = [_Row(type="BidAsk", exch_ts=1_000_000_000, bid_price=99.9, ask_price=100.1,
                     bid_qty=10.0, ask_qty=5.0, price=0.0, volume=0.0)]
        _, research_rows, stats = _call_convert(rows, self._BASE_COLUMNS)
        assert len(research_rows) == 1
        assert stats.bid_ask_defect_dropped == 0
        mid = research_rows[0][4]
        assert mid == pytest.approx(100.0)

    def test_both_zero_is_dropped(self):
        rows = [_Row(type="BidAsk", exch_ts=1_000_000_000, bid_price=0.0, ask_price=0.0,
                     bid_qty=0.0, ask_qty=0.0, price=0.0, volume=0.0)]
        _, research_rows, stats = _call_convert(rows, self._BASE_COLUMNS)
        assert len(research_rows) == 0
        assert stats.bid_ask_defect_dropped == 1

    def test_bid_zero_ask_positive_recovers(self):
        rows = [
            _Row(type="BidAsk", exch_ts=1_000_000_000, bid_price=50.0, ask_price=51.0,
                 bid_qty=5.0, ask_qty=5.0, price=0.0, volume=0.0),
            _Row(type="BidAsk", exch_ts=2_000_000_000, bid_price=0.0, ask_price=51.5,
                 bid_qty=0.0, ask_qty=3.0, price=0.0, volume=0.0),
        ]
        _, research_rows, stats = _call_convert(rows, self._BASE_COLUMNS)
        assert stats.bid_recovered == 1
        assert len(research_rows) == 2
        # Second row bid should be forward-filled from first row
        assert research_rows[1][2] == pytest.approx(50.0)

    def test_ask_zero_bid_positive_recovers(self):
        rows = [
            _Row(type="BidAsk", exch_ts=1_000_000_000, bid_price=50.0, ask_price=51.0,
                 bid_qty=5.0, ask_qty=5.0, price=0.0, volume=0.0),
            _Row(type="BidAsk", exch_ts=2_000_000_000, bid_price=50.2, ask_price=0.0,
                 bid_qty=5.0, ask_qty=0.0, price=0.0, volume=0.0),
        ]
        _, research_rows, stats = _call_convert(rows, self._BASE_COLUMNS)
        assert stats.ask_recovered == 1
        assert research_rows[1][3] == pytest.approx(51.0)

    def test_snapshot_is_skipped(self):
        rows = [_Row(type="Snapshot", exch_ts=1_000_000_000, bid_price=50.0, ask_price=51.0,
                     bid_qty=5.0, ask_qty=5.0, price=0.0, volume=0.0)]
        _, research_rows, stats = _call_convert(rows, self._BASE_COLUMNS)
        assert len(research_rows) == 0
        assert stats.snapshots_skipped == 1

    def test_local_ts_equals_exch_ts(self):
        """ingest_ts is invalid — local_ts must equal exch_ts."""
        rows = [_Row(type="BidAsk", exch_ts=999_000_000_000, bid_price=50.0, ask_price=51.0,
                     bid_qty=1.0, ask_qty=1.0, price=0.0, volume=0.0)]
        _, research_rows, stats = _call_convert(rows, self._BASE_COLUMNS)
        assert research_rows[0][7] == 999_000_000_000  # local_ts = exch_ts


class TestConvertSymbolTick:
    _BASE_COLUMNS = {
        "type": "type",
        "symbol": "symbol",
        "exch_ts": "exch_ts",
        "bid_px": "bid_price",
        "ask_px": "ask_price",
        "bid_qty": "bid_qty",
        "ask_qty": "ask_qty",
        "price": "price",
        "volume": "volume",
    }

    def test_valid_tick_passes_through(self):
        rows = [
            _Row(type="BidAsk", exch_ts=1_000_000_000, bid_price=50.0, ask_price=51.0,
                 bid_qty=5.0, ask_qty=5.0, price=0.0, volume=0.0),
            _Row(type="Tick", exch_ts=2_000_000_000, bid_price=0.0, ask_price=0.0,
                 bid_qty=0.0, ask_qty=0.0, price=50.5, volume=100.0),
        ]
        _, research_rows, stats = _call_convert(rows, self._BASE_COLUMNS)
        assert stats.tick_defect_dropped == 0
        tick_row = research_rows[1]
        assert tick_row[6] == pytest.approx(100.0)  # volume

    def test_tick_price_zero_volume_positive_ffill(self):
        rows = [
            _Row(type="BidAsk", exch_ts=1_000_000_000, bid_price=50.0, ask_price=51.0,
                 bid_qty=5.0, ask_qty=5.0, price=0.0, volume=0.0),
            _Row(type="Tick", exch_ts=2_000_000_000, bid_price=0.0, ask_price=0.0,
                 bid_qty=0.0, ask_qty=0.0, price=50.5, volume=100.0),
            _Row(type="Tick", exch_ts=3_000_000_000, bid_price=0.0, ask_price=0.0,
                 bid_qty=0.0, ask_qty=0.0, price=0.0, volume=50.0),
        ]
        _, research_rows, stats = _call_convert(rows, self._BASE_COLUMNS)
        assert stats.tick_price_ffill == 1
        # ffill: last trade was 50.5
        tick_row = research_rows[2]
        assert tick_row[4] == pytest.approx(50.5)  # mid_price = ffill price

    def test_tick_price_zero_volume_zero_dropped(self):
        rows = [_Row(type="Tick", exch_ts=1_000_000_000, bid_price=0.0, ask_price=0.0,
                     bid_qty=0.0, ask_qty=0.0, price=0.0, volume=0.0)]
        _, research_rows, stats = _call_convert(rows, self._BASE_COLUMNS)
        assert len(research_rows) == 0
        assert stats.tick_defect_dropped == 1


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------
class TestWriters:
    def test_write_research_npy(self, tmp_path):
        rows = [(1.0, 2.0, 99.0, 101.0, 100.0, 20.0, 5.0, 1_000_000_000)]
        arr = np.array(rows, dtype=_RESEARCH_DTYPE)
        out = tmp_path / "research.npy"
        _write_research_npy(rows, out)
        loaded = np.load(str(out))
        assert loaded.shape == (1,)
        assert loaded["bid_px"][0] == pytest.approx(99.0)

    def test_write_defect_report(self, tmp_path):
        stats = DefectStats(
            total_input=100, bid_ask_defect_dropped=1, bid_recovered=2,
            output_rows=97
        )
        out = tmp_path / "defect_report.json"
        _write_defect_report(stats, "TXFB6", out)
        data = json.loads(out.read_text())
        assert data["symbol"] == "TXFB6"
        assert data["total_input"] == 100
        assert data["bid_recovered"] == 2

    def test_write_meta_ul3(self, tmp_path):
        rows = [(1.0, 2.0, 99.0, 101.0, 100.0, 20.0, 5.0, 1_000_000_000)]
        out = tmp_path / "meta.json"
        _write_meta(rows, "TXFB6", "input.parquet", out, "abc123")
        meta = json.loads(out.read_text())
        assert meta["source_type"] == "real"
        assert meta["data_ul"] == 3
        assert meta["rng_seed"] is None
        assert "generator_script" in meta
        assert meta["rows"] == 1


# ---------------------------------------------------------------------------
# Helper: call convert_symbol with stub row list
# ---------------------------------------------------------------------------
def _call_convert(rows, columns, price_scale=1, limit=None):
    """Call convert_symbol using the _Row stub objects directly."""
    import research.tools.legacy.parquet_to_research as mod

    # Build a minimal "DataFrame" object with itertuples
    class StubDF:
        def itertuples(self, index=False):
            return iter(rows)

    return mod.convert_symbol(
        df=StubDF(),
        symbol="TEST",
        col_type=columns.get("type"),
        col_symbol=columns.get("symbol"),
        col_exch_ts=columns.get("exch_ts", "exch_ts"),
        col_bid_px=columns.get("bid_px"),
        col_ask_px=columns.get("ask_px"),
        col_bid_qty=columns.get("bid_qty"),
        col_ask_qty=columns.get("ask_qty"),
        col_price=columns.get("price"),
        col_volume=columns.get("volume"),
        price_scale=price_scale,
        limit=limit,
    )
