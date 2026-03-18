"""Python wrappers for ShmSnapshotTable (Rust-backed seqlock mmap table).

Writer (engine process):
    writer = ShmSnapshotWriter("hft_monitor_snapshot", max_symbols=64)
    writer.publish(slot_idx=0, ts_ns=..., symbol_hash=..., lob_fields=[...], features=[...])

Reader (monitor process):
    reader = ShmSnapshotReader("hft_monitor_snapshot", max_symbols=64)
    slot = reader.read_slot(0)
    if slot is not None:
        print(slot.ts_ns, slot.lob_fields, slot.features)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from structlog import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger("ipc.shm_snapshot")


@dataclass(slots=True)
class SnapshotSlot:
    """Decoded snapshot from one symbol slot."""

    version: int
    ts_ns: int
    symbol_hash: int
    lob_fields: tuple[int, ...]   # 9 LOB stats (platform x10000 convention)
    features: tuple[int, ...]     # 16 feature values


def _symbol_hash(symbol: str) -> int:
    """FNV-1a 64-bit hash for symbol string, matching writer convention."""
    h = 0xCBF29CE484222325
    for b in symbol.encode("ascii"):
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


class ShmSnapshotWriter:
    """Engine-side writer: creates SHM segment and publishes snapshots.

    Single-writer only — must be called from the same thread/process.
    """

    __slots__ = ("_table", "_name", "_max_symbols")

    def __init__(self, name: str = "hft_monitor_snapshot", max_symbols: int = 64) -> None:
        from hft_platform.rust_core import ShmSnapshotTable

        self._name = name
        self._max_symbols = max_symbols
        self._table = ShmSnapshotTable(name, max_symbols, True)
        logger.info("shm_snapshot_writer_created", name=name, max_symbols=max_symbols)

    def publish(
        self,
        slot_idx: int,
        ts_ns: int,
        symbol_hash: int,
        lob_fields: list[int],
        features: list[int],
    ) -> None:
        """Write a snapshot to the given slot (fire-and-forget, ~50ns)."""
        self._table.write_slot(slot_idx, ts_ns, symbol_hash, lob_fields, features)

    @property
    def max_symbols(self) -> int:
        return self._max_symbols


class ShmSnapshotReader:
    """Monitor-side reader: opens existing SHM segment and reads snapshots.

    Thread-safe for concurrent readers (seqlock protocol).
    """

    __slots__ = ("_table", "_name", "_max_symbols")

    def __init__(self, name: str = "hft_monitor_snapshot", max_symbols: int = 64) -> None:
        from hft_platform.rust_core import ShmSnapshotTable

        self._name = name
        self._max_symbols = max_symbols
        self._table = ShmSnapshotTable(name, max_symbols, False)
        logger.info("shm_snapshot_reader_opened", name=name, max_symbols=max_symbols)

    def read_slot(self, slot_idx: int) -> SnapshotSlot | None:
        """Read a snapshot from the given slot. Returns None if never written or torn."""
        result = self._table.read_slot(slot_idx)
        if result is None:
            return None
        version, ts_ns, symbol_hash, lob_fields, features = result
        return SnapshotSlot(
            version=version,
            ts_ns=ts_ns,
            symbol_hash=symbol_hash,
            lob_fields=tuple(lob_fields),
            features=tuple(features),
        )

    def global_version(self) -> int:
        """Read the global version counter."""
        return self._table.global_version()

    @property
    def max_symbols(self) -> int:
        return self._max_symbols
