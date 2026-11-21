"""ClickHouse writer, watermark reads and writes, and the K-bar window loop shared by both sources.

Connection env, as in research/data_pipeline: HFT_CLICKHOUSE_HOST, HFT_CLICKHOUSE_PORT (HTTP, default 8123),
HFT_CLICKHOUSE_USER, HFT_CLICKHOUSE_PASSWORD. Rows are inserted BEFORE the DONE watermark that describes them, so a
crash in between leaves a day that is fetched again (ReplacingMergeTree replaces it, never duplicates).
"""

from __future__ import annotations

import datetime as dt
import os
import time
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple

import structlog

from .model import (
    DONE,
    EMPTY,
    FAILED,
    SKIP_STATUSES,
    KbarRow,
    SourceError,
    TickRow,
    WatermarkRow,
    taipei_date,
    weekdays,
)

log = structlog.get_logger("hist_ingest.store")

BATCH_ROWS = 100_000
TABLES = {
    TickRow: ("hft.hist_ticks", TickRow._fields),
    KbarRow: ("hft.hist_kbars", KbarRow._fields),
    WatermarkRow: ("hft.hist_ingest_watermark", WatermarkRow._fields),
}
_WATERMARK_SQL = (
    "SELECT trading_day, status FROM hft.hist_ingest_watermark FINAL "
    "WHERE source = {source:String} AND product = {product:String} AND kind = {kind:String} "
    "AND trading_day >= {start:Date} AND trading_day <= {end:Date}"
)
_SUMMARY_SQL = (
    "SELECT source, product, kind, status, count(), sum(`rows`), min(trading_day), max(trading_day) "
    "FROM hft.hist_ingest_watermark FINAL GROUP BY source, product, kind, status ORDER BY source, product, kind, status"
)


class HistStore:
    """Thin writer over a ``clickhouse_connect`` client. ``dry_run`` skips every write; reads still happen."""

    def __init__(
        self,
        client: Any,
        *,
        dry_run: bool = False,
        batch_rows: int = BATCH_ROWS,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.client, self.dry_run, self.batch_rows, self.clock_ns = client, dry_run, batch_rows, clock_ns

    @classmethod
    def from_env(cls, *, dry_run: bool = False) -> HistStore:
        import clickhouse_connect

        client = clickhouse_connect.get_client(
            host=os.getenv("HFT_CLICKHOUSE_HOST", "localhost"),
            port=int(os.getenv("HFT_CLICKHOUSE_PORT", "8123")),
            username=os.getenv("HFT_CLICKHOUSE_USER", "default"),
            password=os.getenv("HFT_CLICKHOUSE_PASSWORD") or os.getenv("CLICKHOUSE_PASSWORD", ""),
        )
        return cls(client, dry_run=dry_run)

    def insert(self, rows: Sequence[Any]) -> int:
        """Insert homogeneous rows in batches of at most ``batch_rows``; returns the count (dry-run writes nothing)."""
        if rows and not self.dry_run:
            table, columns = TABLES[type(rows[0])]
            for i in range(0, len(rows), self.batch_rows):
                self.client.insert(table, rows[i : i + self.batch_rows], column_names=list(columns))
        return len(rows)

    def pending_days(
        self, source: str, product: str, kind: str, days: Sequence[dt.date], *, force: bool
    ) -> list[dt.date]:
        """``days`` (order kept) without a DONE or EMPTY watermark; every day with ``force``."""
        if force or not days:
            return list(days)
        params = {
            "source": source,
            "product": product,
            "kind": kind,
            "start": min(days).isoformat(),
            "end": max(days).isoformat(),
        }
        marked = {row[0]: row[1] for row in self.client.query(_WATERMARK_SQL, parameters=params).result_rows}
        return [d for d in days if marked.get(d) not in SKIP_STATUSES]

    def mark_days(
        self,
        source: str,
        product: str,
        kind: str,
        days: Sequence[dt.date],
        status: str | None,
        *,
        rows_by_day: dict[dt.date, int] | None = None,
        contract_code: str = "",
        usage_bytes_after: int = -1,
        note: str = "",
    ) -> None:
        """One watermark per day. ``status=None`` means DONE if that day has rows, else EMPTY."""
        counts, now = rows_by_day or {}, self.clock_ns()
        marks = [
            WatermarkRow(
                source,
                product,
                kind,
                d,
                status or (DONE if counts.get(d) else EMPTY),
                counts.get(d, 0),
                contract_code,
                usage_bytes_after,
                note,
                now,
            )
            for d in days
        ]
        log.info("watermark", source=source, product=product, kind=kind, days=len(marks), status=status)
        self.insert(marks)

    def summary(self) -> list[tuple[Any, ...]]:
        return [tuple(r) for r in self.client.query(_SUMMARY_SQL).result_rows]


class WindowResult(NamedTuple):
    """One K-bar window fetch. ``status=None`` lets each day's bar count decide DONE or EMPTY."""

    rows: Sequence[KbarRow]
    contract_code: str = ""
    status: str | None = None
    usage_bytes_after: int = -1
    note: str = ""
    stop: bool = False


def run_kbar_windows(
    store: HistStore,
    *,
    source: str,
    product: str,
    since: dt.date,
    until: dt.date,
    chunk_days: int,
    force: bool,
    fetch: Callable[[dt.date, dt.date], WindowResult],
) -> Counter[str]:
    """Windows of at most ``chunk_days`` calendar days from the oldest pending weekday up to ``until``.

    Every weekday of a fetched window gets a watermark (bars per Taipei calendar day; 0 is EMPTY). A window that raises
    SourceError is marked FAILED, and three failures stop the loop. Overlapping re-fetches are harmless.
    """
    totals: Counter[str] = Counter()
    pending = store.pending_days(source, product, "kbars", weekdays(since, until), force=force)
    while pending and totals["failed"] < 3:
        start = pending[0]
        end = min(start + dt.timedelta(days=chunk_days - 1), until)
        days, pending = weekdays(start, end), [d for d in pending if d > end]
        try:
            result = fetch(start, end)
        except SourceError as exc:
            log.error(
                "kbars_window_failed", source=source, product=product, start=str(start), end=str(end), error=str(exc)
            )
            store.mark_days(source, product, "kbars", days, FAILED, note=str(exc)[:200])
            totals["failed"] += 1
            continue
        store.insert(result.rows)
        by_day = Counter(taipei_date(r.bar_ts) for r in result.rows)
        store.mark_days(
            source,
            product,
            "kbars",
            days,
            result.status,
            rows_by_day=dict(by_day),
            contract_code=result.contract_code,
            usage_bytes_after=result.usage_bytes_after,
            note=result.note,
        )
        totals["windows"] += 1
        totals["rows"] += len(result.rows)
        if result.stop:
            totals["stopped"] += 1
            break
    return totals
