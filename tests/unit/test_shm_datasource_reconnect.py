"""Unit tests for ShmDataSource.try_reconnect() with exponential backoff."""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from hft_platform.monitor._data_source import (
    _SHM_BACKOFF_FACTOR,
    _SHM_BACKOFF_MAX_S,
    _SHM_BACKOFF_MIN_S,
    ShmDataSource,
)

# Patch targets: the imports inside try_reconnect() come from hft_platform.ipc.shm_snapshot
_READER_CLS = "hft_platform.ipc.shm_snapshot.ShmSnapshotReader"
_SYM_HASH_FN = "hft_platform.ipc.shm_snapshot._symbol_hash"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snap(symbol_hash: int, version: int = 1, ts_ns: int = 1_000_000) -> SimpleNamespace:
    """Return a minimal snapshot-like object."""
    return SimpleNamespace(
        symbol_hash=symbol_hash,
        version=version,
        ts_ns=ts_ns,
        lob_fields=[1_000_000, 1_010_000, 0, 0, 0, 0, 100, 200],
    )


def _symbol_hash_stub(sym: str) -> int:
    """Deterministic stub: hash is ordinal sum of chars."""
    return sum(ord(c) for c in sym)


def _make_reader(snaps_by_slot: dict[int, Any], max_symbols: int = 4) -> MagicMock:
    """Return a mock ShmSnapshotReader."""
    reader = MagicMock()
    reader.max_symbols = max_symbols

    def read_slot(idx: int) -> Any:
        return snaps_by_slot.get(idx)

    reader.read_slot.side_effect = read_slot
    return reader


def _make_disconnected_src(
    symbols: tuple[str, ...] = ("SYM1",),
    retry_count: int = 0,
    last_error: str = "",
    next_retry_at: float = 0.0,
    shm_name: str = "test_shm",
    max_symbols: int = 4,
) -> ShmDataSource:
    """Build a ShmDataSource that starts in a disconnected state (bypasses __init__)."""
    src = ShmDataSource.__new__(ShmDataSource)
    src._reader = None
    src._shm_name = shm_name
    src._max_symbols = max_symbols
    src._symbols = symbols
    src._symbol_to_slot = {}
    src._slot_versions = {}
    src._rows_by_symbol = {s: [] for s in symbols}
    src._connected = False
    src._retry_count = retry_count
    src._last_error = last_error
    src._next_retry_at = next_retry_at
    return src


# ---------------------------------------------------------------------------
# Tests: already-connected source
# ---------------------------------------------------------------------------


class TestTryReconnectWhenAlreadyConnected:
    """Already-connected source should return True immediately without touching SHM."""

    def test_returns_true_without_attempting_shm(self) -> None:
        src = ShmDataSource.__new__(ShmDataSource)
        src._reader = MagicMock()
        src._shm_name = "test"
        src._max_symbols = 4
        src._symbols = ()
        src._symbol_to_slot = {}
        src._slot_versions = {}
        src._rows_by_symbol = {}
        src._connected = True
        src._retry_count = 0
        src._last_error = ""
        src._next_retry_at = 0.0

        call_count = 0

        class _NeverCalled:
            def __init__(self, *a: Any, **kw: Any) -> None:
                nonlocal call_count
                call_count += 1
                raise AssertionError("ShmSnapshotReader should not be called")

        with patch(_READER_CLS, _NeverCalled):
            result = src.try_reconnect()

        assert result is True
        assert call_count == 0
        assert src.retry_count == 0


# ---------------------------------------------------------------------------
# Tests: successful reconnect
# ---------------------------------------------------------------------------


class TestTryReconnectSuccess:
    """Successful reconnect updates all state fields correctly."""

    def _src(self) -> ShmDataSource:
        src = _make_disconnected_src(retry_count=2, last_error="old error")
        src._slot_versions = {0: 5, 1: 3}  # stale versions should be cleared
        return src

    def test_connected_set_to_true(self) -> None:
        src = self._src()
        sym_hash = _symbol_hash_stub("SYM1")
        reader = _make_reader({0: _make_snap(sym_hash)})
        with (
            patch(_READER_CLS, return_value=reader),
            patch(_SYM_HASH_FN, side_effect=_symbol_hash_stub),
        ):
            result = src.try_reconnect()
        assert result is True
        assert src.connected is True

    def test_retry_count_reset_to_zero(self) -> None:
        src = self._src()
        sym_hash = _symbol_hash_stub("SYM1")
        reader = _make_reader({0: _make_snap(sym_hash)})
        with (
            patch(_READER_CLS, return_value=reader),
            patch(_SYM_HASH_FN, side_effect=_symbol_hash_stub),
        ):
            src.try_reconnect()
        assert src.retry_count == 0

    def test_last_error_cleared(self) -> None:
        src = self._src()
        sym_hash = _symbol_hash_stub("SYM1")
        reader = _make_reader({0: _make_snap(sym_hash)})
        with (
            patch(_READER_CLS, return_value=reader),
            patch(_SYM_HASH_FN, side_effect=_symbol_hash_stub),
        ):
            src.try_reconnect()
        assert src.last_error == ""

    def test_slot_versions_cleared(self) -> None:
        src = self._src()
        sym_hash = _symbol_hash_stub("SYM1")
        reader = _make_reader({0: _make_snap(sym_hash)})
        with (
            patch(_READER_CLS, return_value=reader),
            patch(_SYM_HASH_FN, side_effect=_symbol_hash_stub),
        ):
            src.try_reconnect()
        assert src._slot_versions == {}

    def test_symbol_to_slot_remapped_to_new_slot(self) -> None:
        src = self._src()
        sym_hash = _symbol_hash_stub("SYM1")
        # Place symbol in slot 2 (different from any prior mapping)
        reader = _make_reader({2: _make_snap(sym_hash)}, max_symbols=4)
        with (
            patch(_READER_CLS, return_value=reader),
            patch(_SYM_HASH_FN, side_effect=_symbol_hash_stub),
        ):
            src.try_reconnect()
        assert src._symbol_to_slot == {"SYM1": 2}

    def test_reader_replaced(self) -> None:
        src = self._src()
        old_reader = src._reader
        sym_hash = _symbol_hash_stub("SYM1")
        new_reader = _make_reader({0: _make_snap(sym_hash)})
        with (
            patch(_READER_CLS, return_value=new_reader),
            patch(_SYM_HASH_FN, side_effect=_symbol_hash_stub),
        ):
            src.try_reconnect()
        assert src._reader is new_reader
        assert src._reader is not old_reader

    def test_remaining_backoff_is_zero_after_success(self) -> None:
        src = self._src()
        sym_hash = _symbol_hash_stub("SYM1")
        reader = _make_reader({0: _make_snap(sym_hash)})
        with (
            patch(_READER_CLS, return_value=reader),
            patch(_SYM_HASH_FN, side_effect=_symbol_hash_stub),
        ):
            src.try_reconnect()
        assert src.remaining_backoff_seconds() == 0.0


# ---------------------------------------------------------------------------
# Tests: failed reconnect
# ---------------------------------------------------------------------------


class TestTryReconnectFailure:
    """Failed reconnect increments retry_count, records error, applies backoff."""

    def _src(self) -> ShmDataSource:
        return _make_disconnected_src()

    def test_returns_false(self) -> None:
        src = self._src()
        with patch(_READER_CLS, side_effect=OSError("shm not found")):
            result = src.try_reconnect()
        assert result is False

    def test_retry_count_incremented(self) -> None:
        src = self._src()
        with patch(_READER_CLS, side_effect=OSError("shm not found")):
            src.try_reconnect()
        assert src.retry_count == 1

    def test_last_error_populated(self) -> None:
        src = self._src()
        with patch(_READER_CLS, side_effect=OSError("shm not found")):
            src.try_reconnect()
        assert "shm not found" in src.last_error

    def test_connected_remains_false(self) -> None:
        src = self._src()
        with patch(_READER_CLS, side_effect=OSError("shm not found")):
            src.try_reconnect()
        assert src.connected is False

    def test_multiple_failures_increment_retry_count(self) -> None:
        src = self._src()
        for _ in range(3):
            src._next_retry_at = 0.0  # bypass backoff guard for each iteration
            with patch(_READER_CLS, side_effect=OSError("shm not found")):
                src.try_reconnect()
        assert src.retry_count == 3

    def test_does_not_raise(self) -> None:
        """try_reconnect must never propagate exceptions."""
        src = self._src()
        with patch(_READER_CLS, side_effect=RuntimeError("unexpected crash")):
            result = src.try_reconnect()
        assert result is False


# ---------------------------------------------------------------------------
# Tests: backoff behaviour
# ---------------------------------------------------------------------------


class TestBackoffBehavior:
    """Verify exponential backoff prevents rapid reconnect attempts."""

    def _src(self, retry_count: int = 0) -> ShmDataSource:
        return _make_disconnected_src(symbols=(), retry_count=retry_count)

    def test_first_failure_sets_1s_backoff(self) -> None:
        src = self._src()
        with patch(_READER_CLS, side_effect=OSError("fail")):
            src.try_reconnect()
        backoff = src.remaining_backoff_seconds()
        # retry_count=1 → backoff = 1.0 * 2^0 = 1.0s
        assert 0.9 <= backoff <= _SHM_BACKOFF_MIN_S + 0.1, (
            f"Expected ~1s backoff, got {backoff:.3f}s"
        )

    def test_second_failure_sets_2s_backoff(self) -> None:
        src = self._src(retry_count=1)  # first failure already counted
        with patch(_READER_CLS, side_effect=OSError("fail")):
            src.try_reconnect()
        # retry_count becomes 2 → backoff = 1.0 * 2^1 = 2.0s
        backoff = src.remaining_backoff_seconds()
        expected = _SHM_BACKOFF_MIN_S * (_SHM_BACKOFF_FACTOR**1)
        assert abs(backoff - expected) < 0.1, (
            f"Expected ~{expected}s backoff, got {backoff:.3f}s"
        )

    def test_backoff_capped_at_max(self) -> None:
        src = self._src(retry_count=100)  # large count → would overflow without cap
        with patch(_READER_CLS, side_effect=OSError("fail")):
            src.try_reconnect()
        backoff = src.remaining_backoff_seconds()
        assert backoff <= _SHM_BACKOFF_MAX_S + 0.1, (
            f"Backoff {backoff:.1f}s exceeds max {_SHM_BACKOFF_MAX_S}s"
        )

    def test_try_reconnect_skipped_during_backoff(self) -> None:
        src = self._src()
        # Pre-set backoff window to far future
        src._next_retry_at = time.monotonic() + 100.0

        called = []

        class _TrackCalls:
            def __init__(self, *a: Any, **kw: Any) -> None:
                called.append(1)

        with patch(_READER_CLS, _TrackCalls):
            result = src.try_reconnect()

        assert result is False
        assert len(called) == 0, "ShmSnapshotReader should not be instantiated during backoff"

    def test_backoff_increases_between_consecutive_failures(self) -> None:
        src = self._src()
        backoffs: list[float] = []
        for _ in range(4):
            src._next_retry_at = 0.0  # bypass the gate each time
            with patch(_READER_CLS, side_effect=OSError("fail")):
                src.try_reconnect()
            backoffs.append(src.remaining_backoff_seconds())

        # Each backoff must be >= previous (monotonically non-decreasing)
        for i in range(1, len(backoffs)):
            assert backoffs[i] >= backoffs[i - 1] - 0.05, (
                f"Backoff[{i}]={backoffs[i]:.3f}s < Backoff[{i-1}]={backoffs[i-1]:.3f}s"
            )


# ---------------------------------------------------------------------------
# Tests: retry_count resets to zero on success
# ---------------------------------------------------------------------------


class TestReconnectResetsRetryOnSuccess:
    """After a successful reconnect, retry_count must be zero."""

    def test_retry_count_reset_after_prior_failures(self) -> None:
        src = _make_disconnected_src(retry_count=5, last_error="prior error")
        sym_hash = _symbol_hash_stub("SYM1")
        reader = _make_reader({0: _make_snap(sym_hash)})
        with (
            patch(_READER_CLS, return_value=reader),
            patch(_SYM_HASH_FN, side_effect=_symbol_hash_stub),
        ):
            result = src.try_reconnect()
        assert result is True
        assert src.retry_count == 0
        assert src.last_error == ""
        assert src.connected is True

    def test_subsequent_poll_works_after_reconnect(self) -> None:
        """After reconnect, poll() uses the new reader and returns data."""
        src = _make_disconnected_src(retry_count=1, last_error="old error")
        sym_hash = _symbol_hash_stub("SYM1")
        snap = _make_snap(sym_hash, version=10, ts_ns=5_000_000)
        reader = _make_reader({0: snap})
        with (
            patch(_READER_CLS, return_value=reader),
            patch(_SYM_HASH_FN, side_effect=_symbol_hash_stub),
        ):
            src.try_reconnect()
        # Poll with cursor older than snap.ts_ns → expect 1 row returned
        result = src.poll({"SYM1": 0})
        assert "SYM1" in result
        assert len(result["SYM1"]) == 1
