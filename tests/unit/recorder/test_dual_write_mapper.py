"""Unit tests for the L7 dual-write mapper in ``recorder/writer.py``.

Covers:
  * ``_detect_l7_audit_columns`` populates ``_l7_audit_active_tables`` based on
    ``hft.schema_migrations`` rows.
  * Both migrations applied  -> both tables active (extended mode).
  * Neither migration applied -> no tables active (legacy mode).
  * Exactly one applied       -> ``L7PartialMigrationError`` raised.
  * Detection failure         -> defaults to legacy (does NOT raise).
  * ``_strip_l7_columnar`` and ``_strip_l7_rowdicts`` drop audit columns when
    in legacy mode and pass through in extended mode.
  * Tables outside ``_L7_AUDIT_TABLES`` always pass through.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hft_platform.recorder.writer import (
    _L7_AUDIT_COLUMN_NAMES,
    DataWriter,
    L7PartialMigrationError,
)


def _make_writer() -> DataWriter:
    """Construct a DataWriter with CH disabled — we manually inject ch_client."""
    with patch.dict(os.environ, {"HFT_CLICKHOUSE_ENABLED": "0"}, clear=False):
        with patch("hft_platform.recorder.writer.WALWriter"):
            return DataWriter(ch_host="localhost", ch_port=9000)


def _stub_ch_client(rows: list[tuple[str]]) -> SimpleNamespace:
    """Minimal ch_client stub returning the supplied rows from query()."""
    return SimpleNamespace(query=lambda _q: SimpleNamespace(result_rows=rows))


class TestDetectL7AuditColumns:
    def test_both_migrations_applied_activates_both_tables(self) -> None:
        writer = _make_writer()
        writer.ch_client = _stub_ch_client([("20260505_001",), ("20260505_002",)])
        writer._detect_l7_audit_columns()
        assert writer._l7_audit_detected is True
        assert writer._l7_audit_active_tables == {"hft.orders", "hft.fills"}

    def test_neither_migration_applied_activates_no_tables(self) -> None:
        writer = _make_writer()
        writer.ch_client = _stub_ch_client([])
        writer._detect_l7_audit_columns()
        assert writer._l7_audit_detected is True
        assert writer._l7_audit_active_tables == set()

    def test_only_orders_migration_applied_raises_partial(self) -> None:
        writer = _make_writer()
        writer.ch_client = _stub_ch_client([("20260505_001",)])
        with pytest.raises(L7PartialMigrationError) as exc_info:
            writer._detect_l7_audit_columns()
        assert "20260505_001" in str(exc_info.value)
        assert "20260505_002" in str(exc_info.value)

    def test_only_fills_migration_applied_raises_partial(self) -> None:
        writer = _make_writer()
        writer.ch_client = _stub_ch_client([("20260505_002",)])
        with pytest.raises(L7PartialMigrationError) as exc_info:
            writer._detect_l7_audit_columns()
        assert "20260505_001" in str(exc_info.value)
        assert "20260505_002" in str(exc_info.value)

    def test_query_failure_defaults_to_legacy_no_raise(self) -> None:
        writer = _make_writer()

        def _boom(_q: str):
            raise RuntimeError("ch unavailable")

        writer.ch_client = SimpleNamespace(query=_boom)
        writer._detect_l7_audit_columns()
        assert writer._l7_audit_detected is False
        assert writer._l7_audit_active_tables == set()

    def test_no_ch_client_is_no_op(self) -> None:
        writer = _make_writer()
        writer.ch_client = None
        writer._detect_l7_audit_columns()
        assert writer._l7_audit_detected is False
        assert writer._l7_audit_active_tables == set()


class TestStripL7Columnar:
    """Verify columnar-payload audit-column strip behavior."""

    def _payload(self) -> tuple[list[str], list[list[object]]]:
        # 4 columns, 2 rows. 'trace_id' is one of the 7 L7 audit columns.
        column_names = ["order_id", "strategy_id", "price_scaled", "trace_id"]
        column_data = [
            ["ord-1", "ord-2"],
            ["r47_maker", "r47_maker"],
            [171_950_000, 171_960_000],
            ["t-abc", "t-def"],
        ]
        return column_names, column_data

    def test_extended_mode_passes_audit_columns_through(self) -> None:
        writer = _make_writer()
        writer._l7_audit_active_tables = {"hft.orders", "hft.fills"}
        names, data = self._payload()
        out_names, out_data = writer._strip_l7_columnar("hft.orders", names, data)
        assert out_names == names
        assert out_data == data

    def test_legacy_mode_strips_audit_columns(self) -> None:
        writer = _make_writer()
        writer._l7_audit_active_tables = set()
        names, data = self._payload()
        out_names, out_data = writer._strip_l7_columnar("hft.orders", names, data)
        assert "trace_id" not in out_names
        assert out_names == ["order_id", "strategy_id", "price_scaled"]
        assert len(out_data) == 3
        assert out_data[0] == ["ord-1", "ord-2"]

    def test_non_audit_table_always_passes_through(self) -> None:
        writer = _make_writer()
        writer._l7_audit_active_tables = set()  # legacy mode
        names = ["symbol", "trace_id"]  # trace_id present but table is exempt
        data = [["TMFR1"], ["t-abc"]]
        out_names, out_data = writer._strip_l7_columnar("hft.market_data", names, data)
        # market_data is not in _L7_AUDIT_TABLES — pass-through even though
        # 'trace_id' shadows an audit column name.
        assert out_names == names
        assert out_data == data

    def test_no_audit_columns_in_payload_passes_through(self) -> None:
        writer = _make_writer()
        writer._l7_audit_active_tables = set()
        names = ["order_id", "price_scaled"]
        data = [["ord-1"], [100]]
        out_names, out_data = writer._strip_l7_columnar("hft.orders", names, data)
        # Identity since no audit columns to drop.
        assert out_names == names
        assert out_data == data


class TestStripL7RowDicts:
    """Verify row-dict-payload audit-key strip behavior."""

    def _rows(self) -> list[dict[str, object]]:
        return [
            {
                "order_id": "ord-1",
                "strategy_id": "r47_maker",
                "trace_id": "t-abc",
                "git_sha": "deadbeef",
            },
            {
                "order_id": "ord-2",
                "strategy_id": "r47_maker",
                "trace_id": "t-def",
                "git_sha": "deadbeef",
            },
        ]

    def test_extended_mode_passes_audit_keys_through(self) -> None:
        writer = _make_writer()
        writer._l7_audit_active_tables = {"hft.orders", "hft.fills"}
        rows = self._rows()
        out = writer._strip_l7_rowdicts("hft.fills", rows)
        assert out == rows

    def test_legacy_mode_strips_audit_keys(self) -> None:
        writer = _make_writer()
        writer._l7_audit_active_tables = set()
        rows = self._rows()
        out = writer._strip_l7_rowdicts("hft.fills", rows)
        for row in out:
            assert _L7_AUDIT_COLUMN_NAMES.isdisjoint(row.keys())
        # Non-audit keys preserved.
        assert out[0]["order_id"] == "ord-1"
        assert out[0]["strategy_id"] == "r47_maker"

    def test_non_audit_table_passes_through(self) -> None:
        writer = _make_writer()
        writer._l7_audit_active_tables = set()
        rows = [{"symbol": "TMFR1", "trace_id": "t-abc"}]
        out = writer._strip_l7_rowdicts("hft.market_data", rows)
        # market_data is exempt — even an audit-named key passes through.
        assert out == rows
