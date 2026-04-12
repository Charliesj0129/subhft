"""ClickHouse batch writer for shadow order records."""

from __future__ import annotations

import os
from typing import Any

from structlog import get_logger

from hft_platform.infra.ch_client import get_ch_client as _shared_get_ch_client

logger = get_logger("order.shadow_writer")

_INSERT_SQL = (
    "INSERT INTO hft.shadow_orders (ts_ns, strategy_id, symbol, side, price, qty, intent_type, intent_id) VALUES"
)

_RECORD_KEYS: tuple[str, ...] = (
    "ts_ns",
    "strategy_id",
    "symbol",
    "side",
    "price",
    "qty",
    "intent_type",
    "intent_id",
)


def _get_ch_client() -> Any:
    """Return a clickhouse_connect client connected via env-configured host."""
    return _shared_get_ch_client()


class ShadowOrderWriter:
    """Buffers shadow order records and batch-inserts them into ClickHouse.

    Thread-safety: NOT thread-safe. Intended for use on a single async event
    loop or a single thread (consistent with the hot-path design).
    """

    __slots__ = ("_batch", "_batch_size", "_client", "_enabled")

    def __init__(
        self,
        batch_size: int = 50,
        enabled: bool | None = None,
    ) -> None:
        self._batch_size = batch_size
        self._batch: list[tuple[Any, ...]] = []
        self._client: Any = None

        if enabled is not None:
            self._enabled = enabled
        else:
            self._enabled = os.getenv("HFT_CLICKHOUSE_ENABLED", "0") == "1"

    @property
    def pending_count(self) -> int:
        """Number of records buffered and not yet flushed."""
        return len(self._batch)

    def add(self, record: dict[str, Any]) -> None:
        """Append a record dict to the batch, flushing if the batch is full."""
        row = tuple(record.get(k) for k in _RECORD_KEYS)
        self._batch.append(row)
        if len(self._batch) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        """Write all buffered records to ClickHouse and clear the buffer.

        If disabled or empty, this is a no-op (besides logging). Exceptions
        from ClickHouse are caught and logged at WARNING level — never raised.
        """
        if not self._batch:
            return

        rows = self._batch
        self._batch = []

        if not self._enabled:
            logger.debug(
                "Shadow writer disabled — dropping records",
                count=len(rows),
            )
            return

        try:
            client = _get_ch_client()
            client.execute(_INSERT_SQL, rows)
            logger.debug("Shadow orders flushed", count=len(rows))
        except Exception as exc:
            logger.warning(
                "Shadow writer flush failed — records dropped",
                count=len(rows),
                error=str(exc),
            )
