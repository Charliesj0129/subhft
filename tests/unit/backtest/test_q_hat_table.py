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

Task 8 additions
----------------
The bottom of this file adds three regression cases for the
``QueueDepletionFill`` integration: backward-compat (no table → unchanged),
table-hit (looked-up qf used), and table-miss-with-table-supplied (falls back
to ``QHatTable.fallback`` per the design decision documented inline).
"""
from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq

from research.backtest.fill_models import QueueDepletionFill
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


# ---------------------------------------------------------------------------
# Task 8 — QueueDepletionFill ↔ QHatTable wiring regression tests
# ---------------------------------------------------------------------------
# These tests exercise the new ``q_hat_table`` / ``symbol`` / ``clock`` keyword
# arguments wired into ``QueueDepletionFill`` in Task 8. They live in this file
# (rather than ``tests/unit/test_fill_models.py``) because they target the
# QHatTable integration surface; the existing fill_models tests stay focused
# on the legacy positional-only constructor path that ``_gate_c.py:232`` and
# every existing call site still rely on.


def test_queue_depletion_fill_without_table_unchanged() -> None:
    """Without a table, behaves identically to pre-B (regression check).

    This is the non-intrusive pre-B baseline path. ``_gate_c.py:232`` and the
    existing ``tests/unit/test_fill_models.py`` cases still construct
    ``QueueDepletionFill(queue_fraction=0.5)`` positionally; that path MUST
    keep producing the same ``queue_ahead`` as before.
    """
    qdf = QueueDepletionFill(queue_fraction=0.5)

    pos = qdf.post_quote(side="buy", price=10000, book_qty=10)

    # max(1, int(10 * 0.5)) == 5
    assert pos.queue_ahead == 5


def test_queue_depletion_fill_with_table_uses_lookup() -> None:
    """With a table + symbol + clock, queue_ahead reflects the looked-up qf."""
    table = QHatTable(_data={("TMFD6", 9, "shallow"): 0.30})

    # Fixed clock at 09:00:00 UTC (hour=9 matches the calibrated cell).
    fixed_clock = lambda: 9 * 3600 * 1_000_000_000  # noqa: E731

    qdf = QueueDepletionFill(
        queue_fraction=0.5,
        q_hat_table=table,
        symbol="TMFD6",
        clock=fixed_clock,
    )

    # depth=4 < SHALLOW_THRESHOLD(5) -> "shallow" bucket -> q_hat=0.30
    pos = qdf.post_quote(side="buy", price=10000, book_qty=4)

    # max(1, int(4 * 0.30)) == max(1, 1) == 1 (truncation: int(1.2) == 1)
    assert pos.queue_ahead == 1


def test_queue_depletion_fill_unknown_cell_falls_back_to_qhat_fallback() -> None:
    """When a table is provided AND the cell is missing, the looked-up value
    is ``QHatTable.fallback`` (0.5), NOT the ``QueueDepletionFill._qf``
    constructor argument.

    Design decision (Task 8): when a calibration table is supplied, we trust
    the table's own fallback policy. The constructor's ``queue_fraction``
    argument only matters in the no-table-supplied path (backward compat).
    Rationale: callers wiring a table opt into the table's documented
    graceful-degradation policy (single source of truth for fallback). Mixing
    the constructor's qf into the missing-cell path would re-introduce two
    competing fallbacks and confuse later promotion gates.
    """
    table = QHatTable(_data={("TMFD6", 9, "shallow"): 0.30})

    # hour=14 is not in the table -> lookup returns table.fallback (0.5).
    fixed_clock = lambda: 14 * 3600 * 1_000_000_000  # noqa: E731

    qdf = QueueDepletionFill(
        queue_fraction=0.7,  # constructor qf intentionally != table.fallback
        q_hat_table=table,
        symbol="TMFD6",
        clock=fixed_clock,
    )

    pos = qdf.post_quote(side="buy", price=10000, book_qty=10)

    # max(1, int(10 * 0.5)) == 5  (uses table.fallback=0.5, not _qf=0.7)
    assert pos.queue_ahead == 5
