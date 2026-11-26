"""ClickHouse writer, watermark reads and writes, and the K-bar window loop shared by both sources.

Connection env, as in research/data_pipeline: HFT_CLICKHOUSE_HOST, HFT_CLICKHOUSE_PORT (HTTP, default 8123),
HFT_CLICKHOUSE_USER, HFT_CLICKHOUSE_PASSWORD. Each refetch replaces complete trading-day snapshots: mark FAILED,
insert every new batch, synchronously delete older versions, then mark DONE/EMPTY. Insert failure retains all old
keys, although successful new same-key values can supersede old versions. Readers can see mixed old/new versions;
replacement is not atomic and a successful watermark does not isolate concurrent reads. File locks arbitrate writers
on this host only; writers on different hosts require external coordination.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import functools
import hashlib
import json
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlsplit

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


def default_lock_directory() -> Path:
    """Use the canonical repository log directory, shared by its linked worktrees."""
    root = Path(__file__).resolve().parents[3]
    git_dir = root / ".git"
    if git_dir.is_file():
        label, sep, target = git_dir.read_text().strip().partition(":")
        if label != "gitdir" or not sep:
            raise SourceError("cannot resolve historical ingestion lock directory")
        git_dir = (root / target.strip()).resolve()
    common = git_dir / "commondir"
    if common.is_file():
        git_dir = (git_dir / common.read_text().strip()).resolve()
    return git_dir.parent / "log" / "hist_ingest" / "locks"


class WriterBusyError(SourceError):
    """Another writer owns this destination/source/product/kind on this host."""


def locked_ingestion(source: str, kind: str) -> Callable[..., Any]:
    """Acquire all products before any library ingestion reads or broker calls."""

    def decorate(fn: Callable[..., Counter[str]]) -> Callable[..., Counter[str]]:
        @functools.wraps(fn)
        def wrapped(src: Any, store: HistStore, *, products: Sequence[str], **kwargs: Any) -> Counter[str]:
            with store.writer_locks(source, products, kind):
                return fn(src, store, products=products, **kwargs)

        return wrapped

    return decorate


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
        self._held: dict[str, tuple[int, int, int, int]] = {}
        self._lock_guard = threading.RLock()

    def _lock_key(self, source: str, product: str, kind: str) -> str:
        # Qualified TABLES identify the actual destination, not client.database.
        host, port = clickhouse_endpoint()
        url = getattr(self.client, "url", None)
        if isinstance(url, str):
            parsed = urlsplit(url)
            host, port = parsed.hostname or host, parsed.port or port
        else:
            host = getattr(self.client, "host", host)
            port = getattr(self.client, "port", port)
        if host in {"127.0.0.1", "::1"}:
            host = "localhost"
        host, port = str(host).lower(), int(port)
        database = TABLES[TickRow if kind == "ticks" else KbarRow][0].split(".")[0]
        return hashlib.sha256(json.dumps([host, port, database, source, product, kind]).encode()).hexdigest()

    @contextmanager
    def writer_lock(self, source: str, product: str, kind: str) -> Iterator[None]:
        """Nonblocking same-host exclusion; only this store and owning thread may reenter.

        Independent stores use separate descriptors and cannot bypass flock in one process.
        The default canonical repo log/hist_ingest/locks persists across worktrees and reboots.
        HFT_HIST_INGEST_LOCK_DIR overrides it and must be shared by all writers on the host.
        Lock files are retained: unlinking a locked inode would let another writer bypass exclusion.
        """
        if self.dry_run:
            yield
            return
        key = self._lock_key(source, product, kind)
        owner = (os.getpid(), threading.get_ident())
        with self._lock_guard:
            held = self._held.get(key)
            if held and held[:2] == owner:
                self._held[key] = (*owner, held[2], held[3] + 1)
            else:
                override = os.environ.get("HFT_HIST_INGEST_LOCK_DIR")
                directory = Path(override) if override else default_lock_directory()
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(directory / (key + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    os.close(fd)
                    log.error("hist_ingest_writer_busy", source=source, product=product, kind=kind)
                    raise WriterBusyError("historical ingestion writer lock busy") from exc
                except BaseException:
                    os.close(fd)
                    raise
                self._held[key] = (*owner, fd, 1)
        try:
            yield
        finally:
            with self._lock_guard:
                held = self._held[key]
                if held[3] > 1:
                    self._held[key] = (*owner, held[2], held[3] - 1)
                else:
                    del self._held[key]
                    try:
                        fcntl.flock(held[2], fcntl.LOCK_UN)
                    finally:
                        os.close(held[2])

    @contextmanager
    def writer_locks(self, source: str, products: Sequence[str], kind: str) -> Iterator[None]:
        """Acquire the entire product set before reads/writes; release all on any refusal."""
        with ExitStack() as stack:
            for product in sorted(set(products)):
                stack.enter_context(self.writer_lock(source, product, kind))
            yield

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
        with self.writer_lock(source, product, kind):
            return self._replace_days(
                source,
                product,
                kind,
                days,
                rows,
                contract_code=contract_code,
                usage_bytes_after=usage_bytes_after,
            )

    def _replace_days(
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
        Before stale deletion, any insert failure keeps all old keys readable, including absent refetch keys.
        Successful newer same-key rows may replace old values, so readers can see a mixed snapshot.
        A delete failure leaves FAILED. A final-watermark failure may mark only some days complete;
        all data inserts and stale deletion have already succeeded for those days.
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
            where = "source = {source:String} AND product = {product:String} AND " + predicate
            if not self.dry_run:
                existing = self.client.query(
                    f"SELECT max(ingest_ts) FROM {table} WHERE " + where, parameters=params
                ).result_rows[0][0]
                threshold = max(self.clock_ns(), int(existing or 0) + 1)
            else:
                threshold = self.clock_ns()
            selected = [r._replace(ingest_ts=threshold) for r in selected]
            count = self.insert(selected)
            if not self.dry_run:
                params["threshold"] = threshold
                self.client.command(
                    f"ALTER TABLE {table} DELETE WHERE " + where + " AND ingest_ts < {threshold:Int64}",
                    parameters=params,
                    settings={"mutations_sync": 1},
                )
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
    with store.writer_lock(source, product, "kbars"):
        return _run_kbar_windows(
            store,
            source=source,
            product=product,
            since=since,
            until=until,
            chunk_days=chunk_days,
            force=force,
            fetch=fetch,
        )


def _run_kbar_windows(
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

    Only originally pending weekdays get watermarks counted by TAIFEX trading day (0 is EMPTY). The fetch
    starts NIGHT_LOOKBACK_DAYS earlier so the previous evening's night session (Friday night for a Monday) is included.
    A window that raises SourceError is marked FAILED, and three failures stop the loop.
    Only trading days in the window are replaced; partial lookback days are never inserted.
    """
    totals: Counter[str] = Counter()
    pending = store.pending_days(source, product, "kbars", weekdays(since, until), force=force)
    while pending and totals["failed"] < 3:
        start = pending[0]
        end = min(start + dt.timedelta(days=chunk_days - 1), until)
        days, pending = [d for d in pending if d <= end], [d for d in pending if d > end]
        runs: list[list[dt.date]] = []
        for day in days:
            if not runs or weekdays(runs[-1][-1], day) != [runs[-1][-1], day]:
                runs.append([])
            runs[-1].append(day)
        try:
            result = fetch(start - dt.timedelta(days=NIGHT_LOOKBACK_DAYS), end)
            if result.status is None:
                count = sum(
                    store.replace_days(
                        source,
                        product,
                        "kbars",
                        run,
                        result.rows,
                        contract_code=result.contract_code,
                        usage_bytes_after=result.usage_bytes_after,
                    )
                    for run in runs
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
        except WriterBusyError:
            raise
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
