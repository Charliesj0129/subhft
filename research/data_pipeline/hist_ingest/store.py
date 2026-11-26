"""ClickHouse writer, watermark reads and writes, and the K-bar window loop shared by both sources.

Connection env, as in research/data_pipeline: HFT_CLICKHOUSE_HOST, HFT_CLICKHOUSE_PORT (HTTP, default 8123),
HFT_CLICKHOUSE_USER, HFT_CLICKHOUSE_PASSWORD. Each refetch replaces complete trading-day snapshots: mark FAILED,
wait for the old slice's deletion, insert the new rows, then mark DONE/EMPTY. A crash leaves the slice retryable.
This requires a single writer per source/product/kind. Replacement is not atomic for readers: they can see an empty
or partial slice until its final watermark is written; a successful watermark does not isolate concurrent reads.
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
    TAIPEI,
    KbarRow,
    SourceError,
    TickRow,
    WatermarkRow,
    iso_to_utc_ns,
    trading_day_of,
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


def clickhouse_endpoint() -> tuple[str, int]:
    return os.getenv("HFT_CLICKHOUSE_HOST", "localhost"), int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))


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

        host, port = clickhouse_endpoint()
        client = clickhouse_connect.get_client(
            host=host,
            port=port,
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

    def replace_days(
        self,
        source: str,
        product: str,
        kind: str,
        days: Sequence[dt.date],
        rows: Sequence[Any],
        *,
        contract_code: str = "",
        usage_bytes_after: int = -1,
    ) -> int:
        """Replace a contiguous weekday window, including a valid empty reply.

        K-bar lookback rows outside this window are excluded: their trading days may only be partially fetched.
        ``mutations_sync=1`` waits for completion on this server (the existing tables are unreplicated).
        All slice values are bound parameters; table names come only from the fixed table map.
        """
        if not days:
            return 0
        first, last = min(days), max(days)
        if list(days) != weekdays(first, last):
            raise SourceError("snapshot days must be a contiguous ascending weekday window")
        params: dict[str, Any] = {"source": source, "product": product}
        if kind == "ticks":
            selected = [r for r in rows if r.trading_day in days]
            table = TABLES[TickRow][0]
            params.update(start=first, end=last)
            predicate = "trading_day >= {start:Date} AND trading_day <= {end:Date}"
            counts = Counter(r.trading_day for r in selected)
        elif kind == "kbars":
            selected = [r for r in rows if trading_day_of(r.bar_ts) in days]
            table = TABLES[KbarRow][0]
            previous = first - dt.timedelta(days=1)
            while previous.weekday() >= 5:
                previous -= dt.timedelta(days=1)
            start = dt.datetime.combine(previous, dt.time(15), tzinfo=TAIPEI)
            end = dt.datetime.combine(last, dt.time(15), tzinfo=TAIPEI)
            params.update(start=iso_to_utc_ns(start.isoformat()), end=iso_to_utc_ns(end.isoformat()), timeframe=1)
            predicate = "timeframe_min = {timeframe:UInt16} AND bar_ts >= {start:Int64} AND bar_ts < {end:Int64}"
            counts = Counter(trading_day_of(r.bar_ts) for r in selected)
        else:
            raise SourceError("unknown snapshot kind")
        try:
            # Invalidate even an old DONE/EMPTY before any destructive operation, including --force.
            self.mark_days(source, product, kind, days, FAILED, note="snapshot replacement in progress")
            if not self.dry_run:
                sql = f"ALTER TABLE {table} DELETE WHERE source = {{source:String}} AND product = {{product:String}} "
                self.client.command(sql + "AND " + predicate, parameters=params, settings={"mutations_sync": 1})
            count = self.insert(selected)
            self.mark_days(
                source,
                product,
                kind,
                days,
                None,
                rows_by_day=dict(counts),
                contract_code=contract_code,
                usage_bytes_after=usage_bytes_after,
            )
        except Exception as exc:
            raise SourceError(f"snapshot replacement failed: {type(exc).__name__}") from exc
        return count

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


NIGHT_LOOKBACK_DAYS = 3


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

    Every weekday of a window gets a watermark counted by TAIFEX trading day (trading_day_of; 0 is EMPTY). The fetch
    starts NIGHT_LOOKBACK_DAYS earlier so the previous evening's night session (Friday night for a Monday) is included.
    A window that raises SourceError is marked FAILED, and three failures stop the loop.
    Only trading days in the window are replaced; partial lookback days are never inserted.
    """
    totals: Counter[str] = Counter()
    pending = store.pending_days(source, product, "kbars", weekdays(since, until), force=force)
    while pending and totals["failed"] < 3:
        start = pending[0]
        end = min(start + dt.timedelta(days=chunk_days - 1), until)
        days, pending = weekdays(start, end), [d for d in pending if d > end]
        try:
            result = fetch(start - dt.timedelta(days=NIGHT_LOOKBACK_DAYS), end)
            if result.status is None:
                count = store.replace_days(
                    source,
                    product,
                    "kbars",
                    days,
                    result.rows,
                    contract_code=result.contract_code,
                    usage_bytes_after=result.usage_bytes_after,
                )
            else:
                # Quota/stopped responses are not complete snapshots and must retain existing rows.
                count = 0
                store.mark_days(
                    source,
                    product,
                    "kbars",
                    days,
                    result.status,
                    contract_code=result.contract_code,
                    usage_bytes_after=result.usage_bytes_after,
                    note=result.note,
                )
        except SourceError as exc:
            log.error(
                "kbars_window_failed", source=source, product=product, start=str(start), end=str(end), error=str(exc)
            )
            store.mark_days(source, product, "kbars", days, FAILED, note=str(exc)[:200])
            totals["failed"] += 1
            continue
        totals["windows"] += 1
        totals["rows"] += count
        if result.stop:
            totals["stopped"] += 1
            break
    return totals
