"""Tests for WU10: research/tools/data_ingest.py — ClickHouse data ingestion."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from research.tools.data_ingest import (
    _LOB_DTYPE,
    _build_meta,
    _ch_price_to_x10000,
    ingest_from_clickhouse,
)

# ---------------------------------------------------------------------------
# Price scaling
# ---------------------------------------------------------------------------


def test_ingest_price_scaling_basic() -> None:
    """price_scaled / 100 should equal x10000 integer (NTD * 10000)."""
    # ClickHouse stores 100 NTD as 100_000_000 (100 * 1_000_000)
    price_scaled = 100_000_000
    result = _ch_price_to_x10000(price_scaled)
    assert result == 1_000_000  # 100 NTD * 10000


def test_ingest_price_scaling_round_trip() -> None:
    """price_x10000 / 10000 == price_scaled / 1_000_000 (both equal NTD)."""
    price_scaled = 15_500_000  # 15.5 NTD in ClickHouse encoding
    price_x10000 = _ch_price_to_x10000(price_scaled)
    ntd_via_ch = price_scaled / 1_000_000
    ntd_via_npy = price_x10000 / 10_000
    assert abs(ntd_via_ch - ntd_via_npy) < 1e-9


def test_ingest_price_scaling_integer_division() -> None:
    """_ch_price_to_x10000 uses integer division — sub-cent precision is truncated."""
    # 100 NTD + 1 sub-unit → still 1_000_000 after integer division
    price_scaled = 100_000_001
    result = _ch_price_to_x10000(price_scaled)
    assert result == 1_000_000


def test_ingest_price_scaling_zero() -> None:
    assert _ch_price_to_x10000(0) == 0


def test_ingest_price_scaling_large_value() -> None:
    """Large futures price: 20000 NTD = 20_000_000_000 in ClickHouse."""
    price_scaled = 20_000 * 1_000_000
    result = _ch_price_to_x10000(price_scaled)
    assert result == 20_000 * 10_000


# ---------------------------------------------------------------------------
# _build_meta sidecar
# ---------------------------------------------------------------------------


def _sample_meta(output_path: Path, row_count: int = 100) -> dict:
    return _build_meta(
        symbol="TXFC6",
        start="2026-03-01",
        end="2026-03-10",
        clickhouse_host="localhost",
        row_count=row_count,
        data_fingerprint="abc123deadbeef",
        output_path=output_path,
    )


def test_meta_sidecar_has_required_ul3_fields(tmp_path: Path) -> None:
    """_build_meta should produce a dict containing all UL3 required fields."""
    output_path = tmp_path / "TXFC6_2026-03-01_2026-03-10.npy"
    meta = _sample_meta(output_path)

    # UL2 fields
    assert "dataset_id" in meta
    assert "schema_version" in meta
    assert "rows" in meta
    assert "fields" in meta

    # UL3 fields
    assert "rng_seed" in meta
    assert "generator_version" in meta
    assert "parameters" in meta

    # Provenance fields
    assert "source_type" in meta
    assert "data_fingerprint" in meta
    assert "data_ul" in meta
    assert "created_at" in meta


def test_meta_sidecar_data_ul_is_3(tmp_path: Path) -> None:
    output_path = tmp_path / "test.npy"
    meta = _sample_meta(output_path)
    assert meta["data_ul"] == 3


def test_meta_sidecar_source_type_is_real(tmp_path: Path) -> None:
    output_path = tmp_path / "test.npy"
    meta = _sample_meta(output_path)
    assert meta["source_type"] == "real"


def test_meta_sidecar_rng_seed_is_none(tmp_path: Path) -> None:
    """Real data has no RNG seed — should be None."""
    output_path = tmp_path / "test.npy"
    meta = _sample_meta(output_path)
    assert meta["rng_seed"] is None


def test_meta_sidecar_fields_match_lob_dtype(tmp_path: Path) -> None:
    """meta['fields'] should match the _LOB_DTYPE column names."""
    output_path = tmp_path / "test.npy"
    meta = _sample_meta(output_path)
    assert meta["fields"] == list(_LOB_DTYPE.names)


def test_meta_sidecar_symbols_list(tmp_path: Path) -> None:
    output_path = tmp_path / "test.npy"
    meta = _sample_meta(output_path)
    assert meta["symbols"] == ["TXFC6"]


def test_meta_sidecar_row_count_propagated(tmp_path: Path) -> None:
    output_path = tmp_path / "test.npy"
    meta = _sample_meta(output_path, row_count=42)
    assert meta["row_count"] == 42
    assert meta["rows"] == 42


def test_meta_sidecar_date_range(tmp_path: Path) -> None:
    output_path = tmp_path / "test.npy"
    meta = _sample_meta(output_path)
    assert meta["date_range"] == ["2026-03-01", "2026-03-10"]


# ---------------------------------------------------------------------------
# LOB dtype
# ---------------------------------------------------------------------------


def test_lob_dtype_has_expected_fields() -> None:
    expected_fields = {
        "timestamp_ns",
        "price",
        "volume",
        "bid_price",
        "bid_volume",
        "ask_price",
        "ask_volume",
        "side",
    }
    assert expected_fields.issubset(set(_LOB_DTYPE.names))


def test_lob_dtype_price_is_int() -> None:
    """Price fields should be signed int64 for x10000 convention."""
    assert _LOB_DTYPE["price"].kind == "i"  # signed integer
    assert _LOB_DTYPE["bid_price"].kind == "i"
    assert _LOB_DTYPE["ask_price"].kind == "i"


# ---------------------------------------------------------------------------
# ingest_from_clickhouse — mocked ClickHouse
# ---------------------------------------------------------------------------


def _make_mock_rows(n: int = 5) -> list[tuple]:
    """Return fake query rows in the expected column order."""
    return [
        (
            1_700_000_000_000_000_000 + i * 1_000_000,  # timestamp_ns
            100_000_000 + i * 10_000,  # price (ClickHouse scaled)
            10 + i,  # volume
            "Buy",  # side
            99_990_000,  # bid_price
            50,  # bid_volume
            100_010_000,  # ask_price
            50,  # ask_volume
        )
        for i in range(n)
    ]


def _mock_ch_client(rows: list[tuple]) -> MagicMock:
    client = MagicMock()
    result = MagicMock()
    result.result_rows = rows
    client.query.return_value = result
    return client


def test_ingest_creates_npy_and_meta(tmp_path: Path) -> None:
    """ingest_from_clickhouse should write .npy and .npy.meta.json files."""
    rows = _make_mock_rows(10)
    mock_client = _mock_ch_client(rows)

    with patch("clickhouse_connect.get_client", return_value=mock_client):
        output_path = ingest_from_clickhouse(
            symbol="TXFC6",
            date_range=("2026-03-01", "2026-03-05"),
            output_dir=str(tmp_path),
            clickhouse_host="localhost",
        )

    assert output_path.exists(), "Expected .npy file to be created"
    meta_path = output_path.parent / (output_path.name + ".meta.json")
    assert meta_path.exists(), "Expected .npy.meta.json sidecar to be created"


def test_ingest_npy_has_correct_dtype(tmp_path: Path) -> None:
    """Ingested .npy should have the LOB structured dtype."""
    rows = _make_mock_rows(5)
    mock_client = _mock_ch_client(rows)

    with patch("clickhouse_connect.get_client", return_value=mock_client):
        output_path = ingest_from_clickhouse(
            symbol="TXFC6",
            date_range=("2026-03-01", "2026-03-05"),
            output_dir=str(tmp_path),
        )

    arr = np.load(str(output_path), allow_pickle=True)
    assert arr.dtype == _LOB_DTYPE


def test_ingest_price_conversion_in_output(tmp_path: Path) -> None:
    """Prices in .npy should be x10000-scaled integers (price_scaled / 100)."""
    price_scaled = 100_000_000  # 100 NTD in ClickHouse
    expected_x10000 = 1_000_000  # 100 * 10000

    rows = [
        (
            1_700_000_000_000_000_000,  # timestamp_ns
            price_scaled,  # price
            5,  # volume
            "Buy",  # side
            price_scaled - 10_000,  # bid_price
            10,  # bid_volume
            price_scaled + 10_000,  # ask_price
            10,  # ask_volume
        )
    ]
    mock_client = _mock_ch_client(rows)

    with patch("clickhouse_connect.get_client", return_value=mock_client):
        output_path = ingest_from_clickhouse(
            symbol="TXFC6",
            date_range=("2026-03-01", "2026-03-02"),
            output_dir=str(tmp_path),
        )

    arr = np.load(str(output_path), allow_pickle=True)
    assert arr[0]["price"] == expected_x10000


def test_ingest_meta_json_content(tmp_path: Path) -> None:
    """The .meta.json sidecar should contain correct metadata fields."""
    rows = _make_mock_rows(3)
    mock_client = _mock_ch_client(rows)

    with patch("clickhouse_connect.get_client", return_value=mock_client):
        output_path = ingest_from_clickhouse(
            symbol="TXFC6",
            date_range=("2026-03-01", "2026-03-05"),
            output_dir=str(tmp_path),
        )

    meta_path = output_path.parent / (output_path.name + ".meta.json")
    meta = json.loads(meta_path.read_text())

    assert meta["source_type"] == "real"
    assert meta["data_ul"] == 3
    assert meta["symbols"] == ["TXFC6"]
    assert meta["row_count"] == 3
    assert meta["date_range"] == ["2026-03-01", "2026-03-05"]
    assert "data_fingerprint" in meta
    assert len(meta["data_fingerprint"]) == 64  # SHA-256 hex string


def test_ingest_row_count_in_npy(tmp_path: Path) -> None:
    """The written .npy should have exactly as many rows as returned by the query."""
    n = 7
    rows = _make_mock_rows(n)
    mock_client = _mock_ch_client(rows)

    with patch("clickhouse_connect.get_client", return_value=mock_client):
        output_path = ingest_from_clickhouse(
            symbol="TXFC6",
            date_range=("2026-03-01", "2026-03-05"),
            output_dir=str(tmp_path),
        )

    arr = np.load(str(output_path), allow_pickle=True)
    assert len(arr) == n


def test_ingest_handles_no_data(tmp_path: Path) -> None:
    """An empty result set from ClickHouse should raise ValueError, not silently create an empty file."""
    mock_client = _mock_ch_client([])  # zero rows

    with patch("clickhouse_connect.get_client", return_value=mock_client):
        with pytest.raises(ValueError, match="No data returned"):
            ingest_from_clickhouse(
                symbol="TXFC6",
                date_range=("2026-03-01", "2026-03-05"),
                output_dir=str(tmp_path),
            )


def test_ingest_missing_clickhouse_connect(tmp_path: Path) -> None:
    """If clickhouse_connect is not installed, should raise ImportError with helpful message."""
    import builtins

    real_import = builtins.__import__

    def mock_import(name: str, *args, **kwargs):
        if name == "clickhouse_connect":
            raise ImportError("No module named 'clickhouse_connect'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=mock_import):
        with pytest.raises(ImportError, match="clickhouse-connect"):
            ingest_from_clickhouse(
                symbol="TXFC6",
                date_range=("2026-03-01", "2026-03-02"),
                output_dir=str(tmp_path),
            )


def test_ingest_client_is_closed_on_success(tmp_path: Path) -> None:
    """ClickHouse client.close() must be called even on success."""
    rows = _make_mock_rows(2)
    mock_client = _mock_ch_client(rows)

    with patch("clickhouse_connect.get_client", return_value=mock_client):
        ingest_from_clickhouse(
            symbol="TXFC6",
            date_range=("2026-03-01", "2026-03-02"),
            output_dir=str(tmp_path),
        )

    mock_client.close.assert_called_once()


def test_ingest_client_is_closed_on_query_error(tmp_path: Path) -> None:
    """ClickHouse client.close() must be called even when query raises an exception."""
    mock_client = MagicMock()
    mock_client.query.side_effect = RuntimeError("connection lost")

    with patch("clickhouse_connect.get_client", return_value=mock_client):
        with pytest.raises(RuntimeError, match="connection lost"):
            ingest_from_clickhouse(
                symbol="TXFC6",
                date_range=("2026-03-01", "2026-03-02"),
                output_dir=str(tmp_path),
            )

    mock_client.close.assert_called_once()


def test_ingest_output_filename_convention(tmp_path: Path) -> None:
    """Output filename should follow <symbol>_<start>_<end>.npy pattern."""
    rows = _make_mock_rows(1)
    mock_client = _mock_ch_client(rows)

    with patch("clickhouse_connect.get_client", return_value=mock_client):
        output_path = ingest_from_clickhouse(
            symbol="TXFD6",
            date_range=("2026-04-01", "2026-04-30"),
            output_dir=str(tmp_path),
        )

    assert output_path.name == "TXFD6_2026-04-01_2026-04-30.npy"
