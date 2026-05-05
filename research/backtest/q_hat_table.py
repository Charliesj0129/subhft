"""QHatTable: calibrated queue_fraction lookup keyed by (symbol, hour, depth_bucket).

Replaces the literal ``queue_fraction=0.5`` in ``QueueDepletionFill``
(see ``research/backtest/fill_models.py:42-77``) with a calibrated table
produced by ``research/backtest/calibrate_queue_fill.py`` (Slice B Task 6).

Bucket policy
-------------
``depth_bucket = "shallow"`` if ``depth < SHALLOW_THRESHOLD`` (default 5)
else ``"deep"``. The boundary value ``depth=5`` resolves to ``"deep"``.

Fallback policy
---------------
When ``(symbol, hour, depth_bucket)`` is not in the table, ``lookup`` returns
``self.fallback`` (default 0.5). This makes missing calibration cells degrade
gracefully -- never raises and never silently returns garbage. Slice B's
calibration harness deliberately drops cells with ``n < 30`` attempts; those
cells fall through to ``fallback`` here.

Parquet schema (consumed by :meth:`QHatTable.load`)
---------------------------------------------------
- ``symbol: string``
- ``hour: int`` (0-23)
- ``depth_bucket: string`` (``"shallow"`` | ``"deep"``)
- ``q_hat: float`` (calibrated fill rate in [0, 1])

Task 6 (the calibration harness) is the producer of this schema; Task 7 commits
generated fixtures to ``research/backtest/q_hat_data/``; Task 8 wires this
lookup into ``QueueDepletionFill``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pyarrow.parquet as pq

# A depth strictly less than this threshold is "shallow"; depth >= threshold is "deep".
SHALLOW_THRESHOLD: int = 5


@dataclass(frozen=True)
class QHatTable:
    """Frozen lookup of calibrated ``q_hat(symbol, hour, depth_bucket)``.

    The dataclass is frozen so the table is immutable after construction
    (Core Law 4 / immutability rule). ``_data`` uses ``default_factory=dict``
    so the empty-table constructor -- used both by the fallback path and by
    Task 8's "no calibration available" wiring -- is safe.
    """

    _data: dict[tuple[str, int, str], float] = field(default_factory=dict)
    fallback: float = 0.5

    @classmethod
    def load(cls, path: Path | str) -> "QHatTable":
        """Load a calibrated table from a parquet produced by Task 6's harness.

        Defensive casts (``str``, ``int``, ``float``) coerce pyarrow's numpy /
        arrow scalars back to plain Python primitives so the resulting dict
        keys/values hash and compare predictably.
        """
        arrow_table = pq.read_table(str(path))
        records = arrow_table.to_pylist()
        data: dict[tuple[str, int, str], float] = {
            (
                str(record["symbol"]),
                int(record["hour"]),
                str(record["depth_bucket"]),
            ): float(record["q_hat"])
            for record in records
        }
        return cls(_data=data)

    def lookup(self, symbol: str, hour: int, depth: int) -> float:
        """Return the calibrated ``q_hat`` for the cell, or ``self.fallback``."""
        bucket = "shallow" if depth < SHALLOW_THRESHOLD else "deep"
        return self._data.get((symbol, hour, bucket), self.fallback)
