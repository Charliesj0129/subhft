"""Fixed-size monitor snapshots in a versioned Linux shared-memory file.

Publishers skip snapshots when the table is busy. Readers may retry on their
next poll. The writer lease prevents two publishers from sharing a segment,
and an atomic replacement lets an existing reader attach after engine restart.
"""

from __future__ import annotations

import mmap
import os
import stat
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

if sys.platform == "win32":
    fcntl = None
else:
    import fcntl

_SHM_ROOT = Path("/dev/shm")
_MAGIC = b"HFTSHM01"
_HEADER = struct.Struct("<8sQQ")
_VERSION = struct.Struct("<Q")
_GLOBAL_VERSION_OFFSET = 16
_SLOT = struct.Struct("<QqQ25q")
_PAYLOAD = struct.Struct("<qQ25q")
_MAX_SYMBOLS = 10_000


@dataclass(slots=True)
class SnapshotSlot:
    """Decoded snapshot from one symbol slot."""

    version: int
    ts_ns: int
    symbol_hash: int
    lob_fields: tuple[int, ...]
    features: tuple[int, ...]


def _symbol_hash(symbol: str) -> int:
    """FNV-1a 64-bit hash for a symbol string, matching writer convention.

    Encodes as UTF-8, which is ASCII-compatible: every symbol code that hashed
    before hashes to exactly the same value, so segments and readers written
    against the old function stay valid. What changes is that the function can
    no longer *raise*.

    It used to call ``symbol.encode("ascii")``. A hash is asked for on a path
    whose only error handling is to disable itself, so a string it could not
    encode took the whole publisher down::

        shm_publisher_init_failed
          error="'ascii' codec can't encode characters in position 27-30:
                 ordinal not in range(128)"

    That is one warning line, at boot, for a shared-memory monitor feed that
    then stays dead for the life of the process (THESHOW, 2026-09-19T15:19:57Z).
    A hash of a name is never worth refusing to compute.
    """
    h = 0xCBF29CE484222325
    for b in symbol.encode("utf-8", "replace"):
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def _segment_path(name, max_symbols):
    if fcntl is None or not _SHM_ROOT.is_dir():
        raise OSError("shared-memory monitoring requires /dev/shm and flock")
    name = name.removeprefix("/dev/shm/").removeprefix("/")
    if not name or name in {".", ".."} or not all(c.isascii() and (c.isalnum() or c in "-_.") for c in name):
        raise ValueError("invalid shared-memory name")
    if not 1 <= max_symbols <= _MAX_SYMBOLS:
        raise ValueError(f"shared-memory capacity must be between 1 and {_MAX_SYMBOLS}")
    return _SHM_ROOT / f"{name}.hft-py-v1"


def _open_regular(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("shared-memory segment must be a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd, info


def _try_lock(fd, mode):
    try:
        fcntl.flock(fd, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _slot_offset(slot_idx, max_symbols):
    if not 0 <= slot_idx < max_symbols:
        raise IndexError("shared-memory slot index out of range")
    return _HEADER.size + slot_idx * _SLOT.size


class ShmSnapshotWriter:
    """Single-threaded writer that owns the segment until close."""

    __slots__ = ("_fd", "_table", "_lease", "_path", "_max_symbols", "_version", "_payload")

    def __init__(self, name: str = "hft_monitor_snapshot", max_symbols: int = 64) -> None:
        self._fd = self._table = self._lease = None
        self._path = _segment_path(name, max_symbols)
        self._max_symbols = max_symbols
        self._version = 0
        self._payload = bytearray(_PAYLOAD.size)
        lease, info = _open_regular(self._path.with_suffix(".hft-py-v1.writer"), os.O_RDWR | os.O_CREAT)
        try:
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise PermissionError("unsafe shared-memory writer lease")
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fd, temporary = tempfile.mkstemp(dir=_SHM_ROOT, prefix=f".{self._path.name}.")
            table = None
            try:
                size = _HEADER.size + max_symbols * _SLOT.size
                # Reserve backing pages before publishing the segment name.
                # A sparse tmpfs mapping could otherwise SIGBUS on a live write.
                if hasattr(os, "posix_fallocate"):
                    os.posix_fallocate(fd, 0, size)
                elif os.write(fd, bytes(size)) != size:
                    raise OSError("could not allocate the shared-memory segment")
                os.fchmod(fd, 0o644)
                table = mmap.mmap(fd, size, access=mmap.ACCESS_WRITE)
                _HEADER.pack_into(table, 0, _MAGIC, max_symbols, 0)
                os.replace(temporary, self._path)
            except BaseException:
                if table is not None:
                    table.close()
                os.close(fd)
                Path(temporary).unlink(missing_ok=True)
                raise
        except BaseException:
            os.close(lease)
            raise
        self._fd, self._table, self._lease = fd, table, lease

    def publish(
        self,
        slot_idx: int,
        ts_ns: int,
        symbol_hash: int,
        lob_fields: list[int],
        features: list[int],
    ) -> None:
        """Publish one complete snapshot or skip it if a reader holds the table."""
        offset = _slot_offset(slot_idx, self._max_symbols)
        if len(lob_fields) != 9:
            raise ValueError("lob_fields must have 9 values")
        if len(features) != 16:
            raise ValueError("features must have 16 values")
        table, fd = self._table, self._fd
        if table is None or fd is None:
            raise ValueError("shared-memory writer is closed")
        if not _try_lock(fd, fcntl.LOCK_EX):
            return
        try:
            _PAYLOAD.pack_into(self._payload, 0, ts_ns, symbol_hash, *lob_fields, *features)
            version = self._version + 1
            _VERSION.pack_into(table, offset, version * 2 - 1)
            table[offset + _VERSION.size : offset + _SLOT.size] = self._payload
            _VERSION.pack_into(table, offset, version * 2)
            _VERSION.pack_into(table, _GLOBAL_VERSION_OFFSET, version)
            self._version = version
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)

    def close(self) -> None:
        """Release this writer's table and ownership. Safe to call twice."""
        table, fd, lease = self._table, self._fd, self._lease
        if table is None or fd is None or lease is None:
            return
        self._table = self._fd = self._lease = None
        try:
            self._path.unlink(missing_ok=True)
        finally:
            table.close()
            os.close(fd)
            os.close(lease)

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> ShmSnapshotWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def max_symbols(self) -> int:
        return self._max_symbols


class ShmSnapshotReader:
    """Read-only consumer that refreshes its mapping when the writer restarts."""

    __slots__ = ("_fd", "_table", "_lease", "_path", "_max_symbols", "_identity", "_lock", "_generation", "_owner")

    def __init__(self, name: str = "hft_monitor_snapshot", max_symbols: int = 64) -> None:
        self._fd = self._table = self._lease = self._identity = None
        self._lock = Lock()
        self._generation = 0
        self._path = _segment_path(name, max_symbols)
        self._max_symbols = max_symbols
        self._lease, lease_info = _open_regular(self._path.with_suffix(".hft-py-v1.writer"), os.O_RDONLY)
        self._owner = lease_info.st_uid
        try:
            if lease_info.st_mode & 0o022:
                raise PermissionError("unsafe shared-memory writer lease")
            if not self._refresh():
                raise FileNotFoundError("shared-memory publisher is unavailable")
        except BaseException:
            self.close()
            raise

    def _refresh(self):
        lease = self._lease
        if lease is None:
            return False
        if _try_lock(lease, fcntl.LOCK_SH):
            fcntl.flock(lease, fcntl.LOCK_UN)
            return False
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return False
        if self._identity == (info.st_dev, info.st_ino):
            return True
        try:
            fd, info = _open_regular(self._path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        table = None
        try:
            expected_size = _HEADER.size + self._max_symbols * _SLOT.size
            if info.st_size != expected_size or info.st_uid != self._owner or info.st_mode & 0o022:
                raise ValueError("invalid shared-memory segment size or permissions")
            table = mmap.mmap(fd, expected_size, access=mmap.ACCESS_READ)
            magic, max_symbols, _ = _HEADER.unpack_from(table)
            if magic != _MAGIC or max_symbols != self._max_symbols:
                raise ValueError("invalid shared-memory layout or capacity")
        except BaseException:
            if table is not None:
                table.close()
            os.close(fd)
            raise
        self._drop_mapping()
        self._fd, self._table = fd, table
        self._identity = (info.st_dev, info.st_ino)
        self._generation += 1
        return True

    def _drop_mapping(self):
        if self._table is not None:
            self._table.close()
            self._table = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._identity = None

    def read_slot(self, slot_idx: int) -> SnapshotSlot | None:
        """Read a complete snapshot, or return None on contention or writer loss."""
        offset = _slot_offset(slot_idx, self._max_symbols)
        with self._lock:
            if not self._refresh():
                return None
            fd, table = self._fd, self._table
            if fd is None or table is None:
                return None
            if not _try_lock(fd, fcntl.LOCK_SH):
                return None
            try:
                version, ts_ns, symbol_hash, *fields = _SLOT.unpack_from(table, offset)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
            if version == 0 or version & 1:
                return None
            # Prevent a restart's first write from matching an old slot version.
            version += (self._generation - 1) << 64
            return SnapshotSlot(version, ts_ns, symbol_hash, tuple(fields[:9]), tuple(fields[9:]))

    def global_version(self) -> int:
        """Read the current writer's global version counter."""
        with self._lock:
            if not self._refresh():
                return 0
            fd, table = self._fd, self._table
            if fd is None or table is None:
                return 0
            if not _try_lock(fd, fcntl.LOCK_SH):
                return 0
            try:
                return _VERSION.unpack_from(table, _GLOBAL_VERSION_OFFSET)[0]
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def close(self) -> None:
        """Release this reader's mapping. Safe to call twice."""
        with self._lock:
            self._drop_mapping()
            if self._lease is not None:
                os.close(self._lease)
                self._lease = None

    def __del__(self) -> None:
        self.close()

    def __enter__(self) -> ShmSnapshotReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def max_symbols(self) -> int:
        return self._max_symbols
