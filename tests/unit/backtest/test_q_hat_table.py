"""Slice B Task 5 tests for QHatTable lookup + explicit fallback semantics.

The QHatTable provides a calibrated `q_hat(symbol, hour, depth_bucket)` lookup
to replace the literal `queue_fraction=0.5` in `QueueDepletionFill`. Out-of-table
lookups must return the explicit fallback (default 0.5) so missing calibration
data degrades gracefully — never raises and never silently returns garbage.

Depth bucketing policy: `depth < SHALLOW_THRESHOLD (=5)` => "shallow" else "deep".
The boundary value `depth=5` MUST resolve to "deep".

Parquet schema (used by `QHatTable.load`):
- ``symbol: string``
- ``hour: int`` (0-23)
- ``depth_bucket: string`` ("shallow" | "deep")
- ``q_hat: float`` (calibrated fill rate in [0, 1])
"""
from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.q_hat_table import QHatTable


def test_empty_table_returns_fallback() -> None:
    # Arrange: default-constructed empty table (uses default fallback 0.5).
    table = QHatTable()

    # Act
    result = table.lookup("TMFD6", 9, 3)

    # Assert
    assert result == 0.5


def test_loaded_table_hit_returns_calibrated_value() -> None:
    # Arrange: table with one calibrated cell for TMFD6 hour=9 shallow (depth<5).
    table = QHatTable(_data={("TMFD6", 9, "shallow"): 0.42})

    # Act
    result = table.lookup("TMFD6", 9, 3)

    # Assert
    assert result == 0.42


def test_unknown_symbol_returns_fallback() -> None:
    # Arrange
    table = QHatTable(_data={("TMFD6", 9, "shallow"): 0.42})

    # Act
    result = table.lookup("UNKNOWN", 9, 3)

    # Assert
    assert result == 0.5


def test_unknown_hour_returns_fallback() -> None:
    # Arrange
    table = QHatTable(_data={("TMFD6", 9, "shallow"): 0.42})

    # Act
    result = table.lookup("TMFD6", 14, 3)

    # Assert
    assert result == 0.5


def test_boundary_depth_five_uses_deep_bucket() -> None:
    # Arrange: only the "deep" cell is calibrated; "shallow" cell falls through.
    table = QHatTable(_data={("TMFD6", 9, "deep"): 0.31})

    # Act
    deep_hit = table.lookup("TMFD6", 9, 5)  # depth=5 -> "deep" -> 0.31
    shallow_miss = table.lookup("TMFD6", 9, 4)  # depth=4 -> "shallow" -> fallback

    # Assert
    assert deep_hit == 0.31
    assert shallow_miss == 0.5


def test_load_roundtrip_via_parquet(tmp_path) -> None:
    # Arrange: write a small parquet matching the documented schema.
    records = [
        {"symbol": "TMFD6", "hour": 9, "depth_bucket": "shallow", "q_hat": 0.42},
        {"symbol": "TMFD6", "hour": 9, "depth_bucket": "deep", "q_hat": 0.31},
        {"symbol": "TXFD6", "hour": 13, "depth_bucket": "shallow", "q_hat": 0.55},
    ]
    arrow_table = pa.Table.from_pylist(records)
    parquet_path = tmp_path / "q_hat.parquet"
    pq.write_table(arrow_table, parquet_path)

    # Act
    loaded = QHatTable.load(parquet_path)

    # Assert: every calibrated cell is recovered; uncalibrated cells fall through.
    assert loaded.lookup("TMFD6", 9, 3) == 0.42  # shallow hit
    assert loaded.lookup("TMFD6", 9, 5) == 0.31  # deep hit (boundary)
    assert loaded.lookup("TXFD6", 13, 1) == 0.55  # other-symbol shallow hit
    assert loaded.lookup("TXFD6", 13, 5) == 0.5  # missing deep cell -> fallback
    assert loaded.lookup("UNKNOWN", 9, 3) == 0.5  # unknown symbol -> fallback
