"""DataSource protocol and implementations for the Signal Monitor TUI.

Three concrete implementations:
- CHDataSource:     wraps existing CHPoller (pure ClickHouse)
- ShmDataSource:    reads ShmSnapshotReader (shared memory snapshots)
- HybridDataSource: SHM for live polling + CH for sparkline history
"""

from __future__ import annotations

from typing import Any, Protocol

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.monitor._types import (
    CH_PRICE_SCALE,
    PLATFORM_SCALE,
    RowView,
)

logger = get_logger("monitor.data_source")


class DataSource(Protocol):
    """Abstract data source for the monitor engine."""

    def connect(self) -> None: ...

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]: ...

    def fetch_recent_valid(
        self,
        symbol: str,
        limit: int,
        min_ingest_ts: int = 0,
    ) -> list[RowView]: ...

    def try_reconnect(self) -> bool: ...

    @property
    def connected(self) -> bool: ...

    @property
    def retry_count(self) -> int: ...

    @property
    def last_error(self) -> str: ...

    def remaining_backoff_seconds(self) -> float: ...


class CHDataSource:
    """Thin wrapper delegating to existing CHPoller (no behavioral change)."""

    __slots__ = ("_poller",)

    def __init__(self, poller: Any) -> None:
        self._poller = poller

    def connect(self) -> None:
        self._poller.connect()

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        return self._poller.poll(cursors)

    def fetch_recent_valid(
        self,
        symbol: str,
        limit: int,
        min_ingest_ts: int = 0,
    ) -> list[RowView]:
        return self._poller.fetch_recent_valid(symbol, limit, min_ingest_ts=min_ingest_ts)

    def try_reconnect(self) -> bool:
        return self._poller.try_reconnect()

    @property
    def connected(self) -> bool:
        return self._poller.connected

    @property
    def retry_count(self) -> int:
        return self._poller.retry_count

    @property
    def last_error(self) -> str:
        return self._poller.last_error

    def remaining_backoff_seconds(self) -> float:
        return self._poller.remaining_backoff_seconds()


# Scale factor: SHM uses platform x10000, CH uses x1000000
_PLATFORM_TO_CH_SCALE = CH_PRICE_SCALE // PLATFORM_SCALE  # 100


class ShmDataSource:
    """Reads ShmSnapshotReader, converts slots to RowView format.

    Tracks per-slot version to skip unchanged data.
    """

    __slots__ = (
        "_reader",
        "_symbols",
        "_symbol_to_slot",
        "_slot_versions",
        "_rows_by_symbol",
        "_connected",
        "_retry_count",
        "_last_error",
    )

    def __init__(
        self,
        shm_name: str = "hft_monitor_snapshot",
        max_symbols: int = 64,
        symbols: tuple[str, ...] = (),
    ) -> None:
        self._reader: Any = None
        self._symbols = symbols
        self._symbol_to_slot: dict[str, int] = {}
        self._slot_versions: dict[int, int] = {}
        self._rows_by_symbol: dict[str, list[RowView]] = {s: [] for s in symbols}
        self._connected = False
        self._retry_count = 0
        self._last_error = ""

        try:
            from hft_platform.ipc.shm_snapshot import ShmSnapshotReader, _symbol_hash

            self._reader = ShmSnapshotReader(shm_name, max_symbols=max_symbols)
            # Build symbol→slot mapping by scanning for known symbol hashes
            sym_hashes = {_symbol_hash(s): s for s in symbols}
            for slot_idx in range(max_symbols):
                snap = self._reader.read_slot(slot_idx)
                if snap is not None and snap.symbol_hash in sym_hashes:
                    sym = sym_hashes[snap.symbol_hash]
                    self._symbol_to_slot[sym] = slot_idx
            self._connected = True
            logger.info(
                "shm_data_source_connected",
                shm_name=shm_name,
                mapped_symbols=list(self._symbol_to_slot.keys()),
            )
        except Exception as exc:
            self._last_error = str(exc)
            logger.warning("shm_data_source_connect_failed", error=str(exc))

    def connect(self) -> None:
        pass  # Connection happens in __init__

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        if self._reader is None:
            return {}

        for lst in self._rows_by_symbol.values():
            lst.clear()

        for sym, cursor_ts in cursors.items():
            slot_idx = self._symbol_to_slot.get(sym)
            if slot_idx is None:
                # Try lazy discovery
                self._try_discover_slot(sym)
                slot_idx = self._symbol_to_slot.get(sym)
                if slot_idx is None:
                    continue

            snap = self._reader.read_slot(slot_idx)
            if snap is None:
                continue

            # Skip if version unchanged
            prev_ver = self._slot_versions.get(slot_idx, 0)
            if snap.version == prev_ver:
                continue
            self._slot_versions[slot_idx] = snap.version

            # Skip if older than cursor
            if snap.ts_ns <= cursor_ts:
                continue

            row = _snapshot_to_row_view(sym, snap)
            sym_list = self._rows_by_symbol.get(sym)
            if sym_list is not None:
                sym_list.append(row)

        return self._rows_by_symbol

    def _try_discover_slot(self, sym: str) -> None:
        """Scan SHM slots for a newly appearing symbol."""
        if self._reader is None:
            return
        from hft_platform.ipc.shm_snapshot import _symbol_hash

        target_hash = _symbol_hash(sym)
        for slot_idx in range(self._reader.max_symbols):
            if slot_idx in {v for v in self._symbol_to_slot.values()}:
                continue
            snap = self._reader.read_slot(slot_idx)
            if snap is not None and snap.symbol_hash == target_hash:
                self._symbol_to_slot[sym] = slot_idx
                return

    def fetch_recent_valid(
        self,
        symbol: str,
        limit: int,
        min_ingest_ts: int = 0,
    ) -> list[RowView]:
        raise NotImplementedError("ShmDataSource does not support historical replay; use CH")

    def try_reconnect(self) -> bool:
        return self._connected

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def retry_count(self) -> int:
        return self._retry_count

    @property
    def last_error(self) -> str:
        return self._last_error

    def remaining_backoff_seconds(self) -> float:
        return 0.0


class HybridDataSource:
    """SHM for live polling + CH for sparkline history and bootstrap.

    Graceful degradation: SHM fail → CH-only, CH fail → SHM-only.
    """

    __slots__ = (
        "_shm",
        "_ch",
        "_shm_ok",
        "_ch_ok",
        "_mode_label",
        "_backfill_interval_s",
        "_last_backfill_ns",
    )

    def __init__(
        self,
        shm_source: ShmDataSource,
        ch_source: CHDataSource,
        backfill_interval_s: float = 30.0,
    ) -> None:
        self._shm = shm_source
        self._ch = ch_source
        self._shm_ok = shm_source.connected
        self._ch_ok = False
        self._mode_label = "SHM+CH"
        self._backfill_interval_s = backfill_interval_s
        self._last_backfill_ns = 0

    def connect(self) -> None:
        try:
            self._ch.connect()
            self._ch_ok = True
        except Exception as exc:
            self._ch_ok = False
            logger.warning("hybrid_ch_connect_failed", error=str(exc))

        self._shm_ok = self._shm.connected
        self._update_mode_label()

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        if self._shm_ok:
            try:
                result = self._shm.poll(cursors)
                # Periodic CH backfill for sparkline history
                self._maybe_backfill(result, cursors)
                return result
            except Exception:
                self._shm_ok = False
                self._update_mode_label()

        if self._ch_ok:
            return self._ch.poll(cursors)

        return {}

    def _maybe_backfill(
        self,
        result: dict[str, list[RowView]],
        cursors: dict[str, int],
    ) -> None:
        """Best-effort CH backfill for sparkline history."""
        if not self._ch_ok:
            return
        now_ns = timebase.now_ns()
        if now_ns - self._last_backfill_ns < int(self._backfill_interval_s * 1_000_000_000):
            return
        self._last_backfill_ns = now_ns
        try:
            ch_rows = self._ch.poll(cursors)
            for sym, rows in ch_rows.items():
                if rows:
                    existing = result.get(sym)
                    if existing is not None:
                        existing.extend(rows)
                    else:
                        result[sym] = rows
        except Exception as exc:
            logger.warning("hybrid_backfill_failed", error=str(exc))

    def fetch_recent_valid(
        self,
        symbol: str,
        limit: int,
        min_ingest_ts: int = 0,
    ) -> list[RowView]:
        if self._ch_ok:
            return self._ch.fetch_recent_valid(symbol, limit, min_ingest_ts=min_ingest_ts)
        return []

    def try_reconnect(self) -> bool:
        if self._ch_ok:
            return True
        try:
            result = self._ch.try_reconnect()
            if result:
                self._ch_ok = True
                self._update_mode_label()
            return result
        except RuntimeError:
            raise

    @property
    def connected(self) -> bool:
        return self._shm_ok or self._ch_ok

    @property
    def retry_count(self) -> int:
        return self._ch.retry_count

    @property
    def last_error(self) -> str:
        return self._ch.last_error

    def remaining_backoff_seconds(self) -> float:
        return self._ch.remaining_backoff_seconds()

    @property
    def mode_label(self) -> str:
        return self._mode_label

    def _update_mode_label(self) -> None:
        if self._shm_ok and self._ch_ok:
            self._mode_label = "SHM+CH"
        elif self._shm_ok:
            self._mode_label = "SHM"
        elif self._ch_ok:
            self._mode_label = "CH"
        else:
            self._mode_label = "--"


class RedisHybridSource:
    """Redis for live polling + CH for warmup/sparkline history.

    Graceful degradation: Redis fail → CH-only for poll().
    """

    __slots__ = (
        "_redis",
        "_ch",
        "_redis_ok",
        "_ch_ok",
        "_mode_label",
        "_backfill_interval_s",
        "_last_backfill_ns",
    )

    def __init__(
        self,
        redis_poller: Any,
        ch_source: CHDataSource,
        backfill_interval_s: float = 30.0,
    ) -> None:
        self._redis = redis_poller
        self._ch = ch_source
        self._redis_ok = False
        self._ch_ok = False
        self._mode_label = "REDIS+CH"
        self._backfill_interval_s = backfill_interval_s
        self._last_backfill_ns = 0

    def connect(self) -> None:
        try:
            self._redis.connect()
            self._redis_ok = True
        except Exception as exc:
            self._redis_ok = False
            logger.warning("redis_hybrid_redis_connect_failed", error=str(exc))

        try:
            self._ch.connect()
            self._ch_ok = True
        except Exception as exc:
            self._ch_ok = False
            logger.warning("redis_hybrid_ch_connect_failed", error=str(exc))

        self._update_mode_label()

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        if self._redis_ok:
            try:
                result = self._redis.poll(cursors)
                # Periodic CH backfill for sparkline history
                self._maybe_backfill(result, cursors)
                return result
            except Exception as exc:
                self._redis_ok = False
                self._update_mode_label()
                logger.warning("redis_hybrid_redis_poll_failed_fallback_ch", error=str(exc))

        if self._ch_ok:
            return self._ch.poll(cursors)

        return {}

    def _maybe_backfill(
        self,
        result: dict[str, list[RowView]],
        cursors: dict[str, int],
    ) -> None:
        """Best-effort CH backfill for sparkline history."""
        if not self._ch_ok:
            return
        now_ns = timebase.now_ns()
        if now_ns - self._last_backfill_ns < int(self._backfill_interval_s * 1_000_000_000):
            return
        self._last_backfill_ns = now_ns
        try:
            ch_rows = self._ch.poll(cursors)
            for sym, rows in ch_rows.items():
                if rows:
                    existing = result.get(sym)
                    if existing is not None:
                        existing.extend(rows)
                    else:
                        result[sym] = rows
        except Exception as exc:
            logger.warning("hybrid_backfill_failed", error=str(exc))

    def fetch_recent_valid(
        self,
        symbol: str,
        limit: int,
        min_ingest_ts: int = 0,
    ) -> list[RowView]:
        if self._ch_ok:
            return self._ch.fetch_recent_valid(symbol, limit, min_ingest_ts=min_ingest_ts)
        if self._redis_ok:
            return self._redis.fetch_recent_valid(symbol, limit, min_ingest_ts=min_ingest_ts)
        return []

    def try_reconnect(self) -> bool:
        # Try redis first
        if not self._redis_ok:
            try:
                self._redis.connect()
                self._redis_ok = True
                self._update_mode_label()
            except Exception:
                pass

        # Try CH (RuntimeError propagates to caller for max-retry exhaustion)
        if not self._ch_ok:
            try:
                if self._ch.try_reconnect():
                    self._ch_ok = True
                    self._update_mode_label()
            except RuntimeError:
                raise

        return self._redis_ok or self._ch_ok

    @property
    def connected(self) -> bool:
        return self._redis_ok or self._ch_ok

    @property
    def retry_count(self) -> int:
        return self._ch.retry_count

    @property
    def last_error(self) -> str:
        return self._ch.last_error

    def remaining_backoff_seconds(self) -> float:
        return self._ch.remaining_backoff_seconds()

    @property
    def mode_label(self) -> str:
        return self._mode_label

    def _update_mode_label(self) -> None:
        if self._redis_ok and self._ch_ok:
            self._mode_label = "REDIS+CH"
        elif self._redis_ok:
            self._mode_label = "REDIS"
        elif self._ch_ok:
            self._mode_label = "CH"
        else:
            self._mode_label = "--"


def _snapshot_to_row_view(symbol: str, snap: Any) -> RowView:
    """Convert a SnapshotSlot to RowView format.

    SHM uses platform scale (x10000), CH uses x1000000.
    RowView expects CH scale, so multiply by 100.
    """
    lob = snap.lob_fields
    # lob[0] = best_bid (x10000), lob[1] = best_ask (x10000)
    best_bid_ch = lob[0] * _PLATFORM_TO_CH_SCALE if len(lob) > 0 else 0
    best_ask_ch = lob[1] * _PLATFORM_TO_CH_SCALE if len(lob) > 1 else 0
    l1_bid_qty = lob[6] if len(lob) > 6 else 0
    l1_ask_qty = lob[7] if len(lob) > 7 else 0

    return RowView(
        symbol=symbol,
        ingest_ts=snap.ts_ns,
        bids_price=[best_bid_ch],
        asks_price=[best_ask_ch],
        bids_vol=[l1_bid_qty],
        asks_vol=[l1_ask_qty],
        price_scaled=best_bid_ch,
        volume=0,
    )
