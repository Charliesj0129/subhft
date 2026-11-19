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
    table = QHatTable()

    result = table.lookup("TMFD6", 9, 3)

    assert result == 0.5


def test_loaded_table_hit_returns_calibrated_value() -> None:
    table = QHatTable(_data={("TMFD6", 9, "shallow"): 0.42})

    result = table.lookup("TMFD6", 9, 3)

    assert result == 0.42


def test_unknown_symbol_returns_fallback() -> None:
    table = QHatTable(_data={("TMFD6", 9, "shallow"): 0.42})

    result = table.lookup("UNKNOWN", 9, 3)

    assert result == 0.5


def test_unknown_hour_returns_fallback() -> None:
    table = QHatTable(_data={("TMFD6", 9, "shallow"): 0.42})

    result = table.lookup("TMFD6", 14, 3)

    assert result == 0.5


def test_boundary_depth_five_uses_deep_bucket() -> None:
    table = QHatTable(_data={("TMFD6", 9, "deep"): 0.31})

    deep_hit = table.lookup("TMFD6", 9, 5)
    shallow_miss = table.lookup("TMFD6", 9, 4)

    assert deep_hit == 0.31
    assert shallow_miss == 0.5


def test_load_roundtrip_via_parquet(tmp_path) -> None:
    records = [
        {"symbol": "TMFD6", "hour": 9, "depth_bucket": "shallow", "q_hat": 0.42},
        {"symbol": "TMFD6", "hour": 9, "depth_bucket": "deep", "q_hat": 0.31},
        {"symbol": "TXFD6", "hour": 13, "depth_bucket": "shallow", "q_hat": 0.55},
    ]
    arrow_table = pa.Table.from_pylist(records)
    parquet_path = tmp_path / "q_hat.parquet"
    pq.write_table(arrow_table, parquet_path)

    loaded = QHatTable.load(parquet_path)

    assert loaded.lookup("TMFD6", 9, 3) == 0.42
    assert loaded.lookup("TMFD6", 9, 5) == 0.31
    assert loaded.lookup("TXFD6", 13, 1) == 0.55
    assert loaded.lookup("TXFD6", 13, 5) == 0.5
    assert loaded.lookup("UNKNOWN", 9, 3) == 0.5


def test_queue_depletion_fill_without_table_unchanged() -> None:
    """Without a table, behaves identically to pre-B (regression check).

    This is the non-intrusive pre-B baseline path. ``_gate_c.py:232`` and the
    existing ``tests/unit/test_fill_models.py`` cases still construct
    ``QueueDepletionFill(queue_fraction=0.5)`` positionally; that path MUST
    keep producing the same ``queue_ahead`` as before.
    """
    qdf = QueueDepletionFill(queue_fraction=0.5)

    pos = qdf.post_quote(side="buy", price=10000, book_qty=10)

    assert pos.queue_ahead == 5


def test_queue_depletion_fill_with_table_uses_lookup() -> None:
    """With a table + symbol + clock, queue_ahead reflects the looked-up qf."""
    table = QHatTable(_data={("TMFD6", 9, "shallow"): 0.30})

    fixed_clock = lambda: 9 * 3600 * 1_000_000_000

    qdf = QueueDepletionFill(
        queue_fraction=0.5,
        q_hat_table=table,
        symbol="TMFD6",
        clock=fixed_clock,
    )

    pos = qdf.post_quote(side="buy", price=10000, book_qty=4)

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

    fixed_clock = lambda: 14 * 3600 * 1_000_000_000

    qdf = QueueDepletionFill(
        queue_fraction=0.7,
        q_hat_table=table,
        symbol="TMFD6",
        clock=fixed_clock,
    )

    pos = qdf.post_quote(side="buy", price=10000, book_qty=10)

    assert pos.queue_ahead == 5
