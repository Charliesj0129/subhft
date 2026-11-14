"""Dead-letter queue for orphaned fills (WU-03)."""

import os
import tempfile
from collections import deque
from dataclasses import asdict
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("execution.fill_dlq")

_DEFAULT_PERSIST_PATH = ".state/fill_dlq.jsonl"
_MAX_QUARANTINE_TRACKED = 10_000


class OrphanedFillDLQ:
    __slots__ = (
        "_queue",
        "_max_size",
        "_persist_path",
        "_overflow_evicted",
        "_quarantined_ids",
        "_sidecar_unsynced",
        "last_unrecovered",
    )

    def __init__(self, max_size: int = 1000, persist_path: str | None = None) -> None:
        self._queue: deque[Any] = deque(maxlen=max_size)
        self._max_size = max_size
        self._persist_path: str = persist_path or os.getenv("HFT_FILL_DLQ_PERSIST_PATH") or _DEFAULT_PERSIST_PATH
        self._overflow_evicted: list[Any] = []
        # Fills already written to the sidecar, keyed by id(): an evicted fill stays in
        # ``_overflow_evicted`` and would otherwise be quarantined again by every persist().
        # The value holds the fill itself so its id() cannot be reused by a later object
        # while the entry exists; bounded FIFO (a re-quarantine after eviction is only a
        # duplicate sidecar line, never a lost fill).
        self._quarantined_ids: dict[int, Any] = {}
        # True once the sidecar took a flush-only write (event-loop path); the next persist() fsyncs it.
        self._sidecar_unsynced = False
        # Fills the last persist() could neither write to the main file nor keep durably in the sidecar.
        self.last_unrecovered = 0

    def add(self, fill_event: Any) -> None:
        if len(self._queue) == self._max_size:
            MetricsRegistry.get().fill_dlq_overflow_total.inc()
            # Retain evicted fill in memory so persist() includes it.
            evicted = self._queue[0]
            self._overflow_evicted.append(evicted)
            # Also append to disk as immediate safety net in case of crash.
            self._persist_single(evicted)
            logger.error(
                "fill_dlq_overflow — evicted fill retained for persist",
                evicted_symbol=getattr(evicted, "symbol", ""),
                evicted_order_id=getattr(evicted, "order_id", ""),
                new_symbol=getattr(fill_event, "symbol", ""),
                dlq_size=len(self._queue),
                overflow_evicted=len(self._overflow_evicted),
            )
        self._queue.append(fill_event)
        logger.warning(
            "Orphaned fill added to DLQ", symbol=getattr(fill_event, "symbol", ""), dlq_size=len(self._queue)
        )

    def _persist_single(self, fill_event: Any) -> None:
        """Append a single fill to the DLQ file (best-effort, no fsync)."""
        try:
            import orjson

            row = asdict(fill_event)
            path = self._persist_path
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "ab") as f:
                f.write(orjson.dumps(row) + b"\n")
        except Exception as exc:
            logger.error("fill_dlq_persist_single_failed", error=str(exc))
            # add() runs on the event loop: flush only, like the main-file append above.
            self._quarantine([fill_event], durable=False)

    @staticmethod
    def _fsync_dir(directory: str) -> None:
        """fsync a directory so a file created or linked in it survives power loss; raises OSError."""
        fd = os.open(directory or ".", os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _quarantine(self, fills: list[Any], durable: bool = True) -> bool:
        """Keep fills that did not reach the main file in ``<persist_path>.unserializable``.

        A fill is money. Dropping one because a field would not serialise, or
        because the disk refused the write, made it vanish with only a log line;
        the ``repr`` here lets an operator rebuild it by hand. The count is
        exported so an alert can fire on it. Never raises: it runs on the
        ``add()`` path and during shutdown, where a second failure must not
        cost the fill that is still in memory.

        A fill counts as quarantined only after the sidecar write succeeded; a
        failed write (ENOSPC, EIO) leaves it eligible, so the next ``persist()``
        retries it. ``durable=False`` skips the fsync for callers on the event
        loop (``add()``), where a slow disk must not stall fill processing.

        Returns True when every fill in ``fills`` now has a durable copy in the sidecar.
        """
        try:
            fresh = [fill for fill in fills if id(fill) not in self._quarantined_ids]
            if not fresh:
                return not self._sidecar_unsynced
            try:
                MetricsRegistry.get().fill_dlq_persist_failed_total.inc(len(fresh))
            except Exception as exc:  # noqa: BLE001 -- a metrics fault must not stop the sidecar write
                logger.error("fill_dlq_quarantine_metric_failed", error=str(exc))
            sidecar = self._persist_path + ".unserializable"
            try:
                os.makedirs(os.path.dirname(sidecar) or ".", exist_ok=True)
                with open(sidecar, "a", encoding="utf-8") as f:
                    for fill in fresh:
                        f.write(repr(fill) + "\n")
                    f.flush()
                    if durable:
                        os.fsync(f.fileno())
                if durable:
                    # a new sidecar's directory entry is not durable until its parent is synced
                    self._fsync_dir(os.path.dirname(sidecar))
                # a durable write fsyncs the whole file, so it also covers earlier flush-only lines
                self._sidecar_unsynced = not durable
                written_durably = durable
            except OSError as exc:
                logger.error("fill_dlq_quarantine_failed", error=str(exc), path=sidecar, count=len(fresh))
                written_durably = False
            else:
                for fill in fresh:
                    self._quarantined_ids[id(fill)] = fill
                while len(self._quarantined_ids) > _MAX_QUARANTINE_TRACKED:
                    del self._quarantined_ids[next(iter(self._quarantined_ids))]
            for fill in fresh:
                logger.error(
                    "fill_dlq_fill_not_persisted",
                    fill_id=getattr(fill, "fill_id", ""),
                    order_id=getattr(fill, "order_id", ""),
                    symbol=getattr(fill, "symbol", ""),
                )
            return written_durably
        except Exception as exc:  # noqa: BLE001 -- see docstring: must not raise into add()/shutdown
            logger.error("fill_dlq_quarantine_error", error=str(exc))
            return False

    def _sync_sidecar(self) -> None:
        """fsync a sidecar that only took flush-only writes from ``add()``; keep the flag if it fails."""
        if not self._sidecar_unsynced:
            return
        sidecar = self._persist_path + ".unserializable"
        try:
            with open(sidecar, "ab") as f:
                os.fsync(f.fileno())
            self._fsync_dir(os.path.dirname(sidecar))
            self._sidecar_unsynced = False
        except OSError as exc:
            logger.error("fill_dlq_quarantine_sync_failed", error=str(exc), path=sidecar)
            # Nothing in the sidecar is known durable: make every fill eligible again so the
            # persist() that called us rewrites and fsyncs them before it renames the main file.
            self._quarantined_ids.clear()

    @property
    def count(self) -> int:
        return len(self._queue)

    def drain(self) -> list[Any]:
        items = list(self._queue)
        self._queue.clear()
        return items

    def retry(self, resolver_fn: Any) -> tuple[list[Any], list[Any]]:
        """Attempt to re-resolve orphaned fills.

        Args:
            resolver_fn: Callable that takes a fill event and returns a strategy_id str.

        Returns:
            (resolved, still_orphaned) — resolved fills have strategy_id set.
        """
        items = list(self._queue)
        self._queue.clear()
        resolved = []
        still_orphaned = []
        for fill in items:
            new_strategy_id = resolver_fn(fill)
            if new_strategy_id and new_strategy_id != "UNKNOWN":
                fill.strategy_id = new_strategy_id
                resolved.append(fill)
            else:
                still_orphaned.append(fill)
        for f in still_orphaned:
            self._queue.append(f)
        return resolved, still_orphaned

    def persist(self) -> tuple[int, int]:
        """Persist DLQ to disk atomically (temp+fsync+rename).

        Called during graceful shutdown so orphaned fills survive restart.
        Includes overflow-evicted fills that were too old for the in-memory deque.

        Returns ``(written, failed)``. A fill that cannot be serialised is not
        written to the main file but is quarantined (see :meth:`_quarantine`).
        """
        path = self._persist_path
        self._sync_sidecar()
        snapshot = list(self._overflow_evicted) + list(self._queue)
        if not snapshot:
            # Remove stale file if queue is empty
            if os.path.exists(path):
                os.unlink(path)
            self.last_unrecovered = 0
            return (0, 0)
        written = 0
        failed: list[Any] = []
        renamed = False
        keep_tmp = False
        unrecovered = 0
        try:
            import orjson

            persist_dir = os.path.dirname(path) or "."
            os.makedirs(persist_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=persist_dir)
            # M2 (2026-04-25): finally-cleanup so orphan tmpfiles don't
            # accumulate when the worker dies between fsync and rename.
            try:
                with os.fdopen(fd, "wb") as f:
                    for fill in snapshot:
                        try:
                            row = asdict(fill)
                            # Side is IntEnum — orjson handles it natively
                            line = orjson.dumps(row) + b"\n"
                        except Exception:
                            failed.append(fill)
                            continue
                        f.write(line)
                        written += 1
                    f.flush()
                    os.fsync(f.fileno())
                # Before the rename: the sidecar must exist by the time the main
                # file stops carrying these fills.
                if failed and not self._quarantine(failed):
                    unrecovered = len(failed)
                    # The old main file is the only other place these fills may still be: keep
                    # it beside the new one instead of renaming over it.
                    if not self._keep_previous_main_file(path):
                        # The old main file stays, so this fsynced temp file is the only copy of the
                        # fills that did serialise: leave it on disk for the operator, never delete it.
                        keep_tmp = True
                        logger.critical("fill_dlq_new_file_kept", path=tmp_path, main=path)
                        raise OSError("could not preserve the previous DLQ file; leaving it in place")
                os.rename(tmp_path, path)
                renamed = True
                try:
                    self._fsync_dir(persist_dir)
                except OSError as exc:
                    # The rename may not survive a power cut: report these fills as not durable
                    # so shutdown logs it, instead of a clean (written, 0).
                    logger.error("fill_dlq_dir_sync_failed", error=str(exc), path=persist_dir)
                    unrecovered = max(unrecovered, written)
            finally:
                if not keep_tmp and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            logger.info("fill_dlq_persisted", written=written, failed=len(failed), path=path)
        except Exception as exc:
            logger.warning("fill_dlq_persist_failed", error=str(exc), path=path)
        if not renamed:
            # The disk refused the write (or the rename): nothing new is in the
            # main file, so every fill in memory goes to the sidecar instead.
            if not self._quarantine(snapshot):
                unrecovered = len(snapshot)
            self.last_unrecovered = unrecovered
            return (0, len(snapshot))
        self.last_unrecovered = unrecovered
        return (written, len(failed))

    @classmethod
    def _keep_previous_main_file(cls, path: str) -> bool:
        """Hard-link the current main file to a new ``<path>.bak.<ns>`` so a rename cannot be its last copy.

        Never overwrites or deletes an earlier backup: a repeat persist() while a fill is still
        unrecovered must not replace the one file that holds it. Returns False when the link
        could not be made, in which case the caller must not rename over the main file.
        """
        if not os.path.exists(path):
            return True
        stamp = timebase.now_ns()
        for attempt in range(100):
            backup = f"{path}.bak.{stamp}" if attempt == 0 else f"{path}.bak.{stamp}.{attempt}"
            try:
                os.link(path, backup)
                # the new name must be durable before the caller renames over the original
                cls._fsync_dir(os.path.dirname(backup))
                return True
            except FileExistsError:
                continue
            except OSError as exc:
                logger.error("fill_dlq_backup_failed", error=str(exc), path=backup)
                return False
        return False

    def load(self) -> tuple[int, int]:
        """Load DLQ from disk on startup. Returns ``(loaded, skipped)``."""
        path = self._persist_path
        if not os.path.exists(path):
            return (0, 0)
        loaded = 0
        skipped = 0
        try:
            import orjson

            from hft_platform.contracts.execution import FillEvent
            from hft_platform.contracts.strategy import Side

            with open(path, "rb") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = orjson.loads(raw)
                        if not isinstance(obj, dict):
                            skipped += 1
                            continue
                        obj["side"] = Side(obj["side"])
                        fill = FillEvent(**obj)
                        self._queue.append(fill)
                        loaded += 1
                    except Exception:
                        skipped += 1
                        continue
            if skipped:
                logger.error("fill_dlq_load_skipped_lines", loaded=loaded, skipped=skipped, path=path)
            logger.info("fill_dlq_loaded", count=loaded, skipped=skipped, path=path)
        except Exception as exc:
            logger.warning("fill_dlq_load_failed", error=str(exc), path=path)
        return (loaded, skipped)


_dlq: OrphanedFillDLQ | None = None


def get_orphaned_fill_dlq() -> OrphanedFillDLQ:
    global _dlq
    if _dlq is None:
        _dlq = OrphanedFillDLQ()
        _dlq.load()
    return _dlq
