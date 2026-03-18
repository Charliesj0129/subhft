"""Comprehensive unit tests for DataSource implementations.

Tests CHDataSource, ShmDataSource, HybridDataSource, RedisHybridSource,
and _snapshot_to_row_view using stub objects (no real SHM/Redis/ClickHouse).
"""

from __future__ import annotations

import pytest

from hft_platform.monitor._types import RowView

# ── Stub classes ────────────────────────────────────────────────────────────


class _StubPoller:
    """Minimal stub implementing the poller interface for CHDataSource."""

    __slots__ = (
        "_connected",
        "_retry_count",
        "_last_error",
        "_poll_result",
        "_fetch_result",
        "_fail_connect",
        "_reconnect_result",
    )

    def __init__(
        self,
        *,
        fail_connect: bool = False,
        reconnect_result: bool = True,
    ) -> None:
        self._connected = False
        self._retry_count = 0
        self._last_error = ""
        self._poll_result: dict[str, list[RowView]] = {}
        self._fetch_result: list[RowView] = []
        self._fail_connect = fail_connect
        self._reconnect_result = reconnect_result

    def connect(self) -> None:
        if self._fail_connect:
            raise ConnectionError("stub CH unavailable")
        self._connected = True

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        return self._poll_result

    def fetch_recent_valid(
        self,
        symbol: str,
        limit: int,
        min_ingest_ts: int = 0,
    ) -> list[RowView]:
        return self._fetch_result

    def try_reconnect(self) -> bool:
        if self._reconnect_result:
            self._connected = True
        return self._reconnect_result

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


class _StubSnapshotSlot:
    """Minimal stub for SnapshotSlot."""

    __slots__ = ("version", "ts_ns", "symbol_hash", "lob_fields", "features")

    def __init__(
        self,
        version: int = 1,
        ts_ns: int = 1000,
        symbol_hash: int = 0,
        lob_fields: tuple[int, ...] = (),
        features: tuple[int, ...] = (),
    ) -> None:
        self.version = version
        self.ts_ns = ts_ns
        self.symbol_hash = symbol_hash
        self.lob_fields = lob_fields
        self.features = features


class _StubReader:
    """Minimal stub for ShmSnapshotReader."""

    __slots__ = ("_slots", "max_symbols")

    def __init__(self, max_symbols: int = 4) -> None:
        self.max_symbols = max_symbols
        self._slots: dict[int, _StubSnapshotSlot | None] = {}

    def read_slot(self, slot_idx: int) -> _StubSnapshotSlot | None:
        return self._slots.get(slot_idx)


class _StubRedisPoller:
    """Minimal stub for Redis poller."""

    __slots__ = (
        "_fail_connect",
        "_poll_result",
        "_fetch_result",
        "_poll_raises",
    )

    def __init__(
        self,
        *,
        fail_connect: bool = False,
        poll_raises: bool = False,
    ) -> None:
        self._fail_connect = fail_connect
        self._poll_result: dict[str, list[RowView]] = {}
        self._fetch_result: list[RowView] = []
        self._poll_raises = poll_raises

    def connect(self) -> None:
        if self._fail_connect:
            raise ConnectionError("stub Redis unavailable")

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        if self._poll_raises:
            raise ConnectionError("Redis poll failed")
        return self._poll_result

    def fetch_recent_valid(
        self,
        symbol: str,
        limit: int,
        min_ingest_ts: int = 0,
    ) -> list[RowView]:
        return self._fetch_result


def _row(
    symbol: str = "2330",
    ts: int = 100,
    price: int = 100_000_000,
) -> RowView:
    return RowView(symbol, ts, [price], [price + 1000], [10], [20], price, 1)


# ── _snapshot_to_row_view ───────────────────────────────────────────────────


class TestSnapshotToRowView:
    def test_price_scale_conversion(self) -> None:
        from hft_platform.monitor._data_source import _snapshot_to_row_view

        snap = _StubSnapshotSlot(
            version=1,
            ts_ns=5000,
            symbol_hash=0,
            lob_fields=(2100000, 2105000, 0, 0, 0, 0, 10, 20, 0),
        )
        row = _snapshot_to_row_view("2330", snap)

        # Platform x10000 * 100 = CH x1000000
        assert row.bids_price == [210_000_000]
        assert row.asks_price == [210_500_000]
        assert row.bids_vol == [10]
        assert row.asks_vol == [20]
        assert row.price_scaled == 210_000_000
        assert row.volume == 0
        assert row.symbol == "2330"
        assert row.ingest_ts == 5000

    def test_empty_lob_fields(self) -> None:
        from hft_platform.monitor._data_source import _snapshot_to_row_view

        snap = _StubSnapshotSlot(lob_fields=())
        row = _snapshot_to_row_view("2881", snap)

        assert row.bids_price == [0]
        assert row.asks_price == [0]
        assert row.bids_vol == [0]
        assert row.asks_vol == [0]
        assert row.price_scaled == 0

    def test_partial_lob_fields_only_bid(self) -> None:
        from hft_platform.monitor._data_source import _snapshot_to_row_view

        snap = _StubSnapshotSlot(lob_fields=(5000,))
        row = _snapshot_to_row_view("2317", snap)

        assert row.bids_price == [500_000]
        assert row.asks_price == [0]  # no ask field
        assert row.bids_vol == [0]  # no qty fields
        assert row.asks_vol == [0]

    def test_lob_fields_with_seven_elements(self) -> None:
        """lob[6] exists (bid_qty) but lob[7] (ask_qty) does not."""
        from hft_platform.monitor._data_source import _snapshot_to_row_view

        snap = _StubSnapshotSlot(lob_fields=(100, 200, 0, 0, 0, 0, 50))
        row = _snapshot_to_row_view("2454", snap)

        assert row.bids_vol == [50]
        assert row.asks_vol == [0]


# ── CHDataSource ────────────────────────────────────────────────────────────


class TestCHDataSource:
    def test_delegates_connect(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource

        poller = _StubPoller()
        ds = CHDataSource(poller)
        ds.connect()
        assert ds.connected is True

    def test_delegates_poll(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource

        poller = _StubPoller()
        row = _row()
        poller._poll_result = {"2330": [row]}
        ds = CHDataSource(poller)

        result = ds.poll({"2330": 0})
        assert result["2330"][0] is row

    def test_delegates_fetch_recent_valid(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource

        poller = _StubPoller()
        row = _row()
        poller._fetch_result = [row]
        ds = CHDataSource(poller)

        result = ds.fetch_recent_valid("2330", 10, min_ingest_ts=50)
        assert len(result) == 1
        assert result[0] is row

    def test_delegates_try_reconnect(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource

        poller = _StubPoller(reconnect_result=False)
        ds = CHDataSource(poller)
        assert ds.try_reconnect() is False

    def test_delegates_properties(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource

        poller = _StubPoller()
        poller._retry_count = 3
        poller._last_error = "timeout"
        ds = CHDataSource(poller)

        assert ds.retry_count == 3
        assert ds.last_error == "timeout"
        assert ds.remaining_backoff_seconds() == 0.0


# ── ShmDataSource (stubbed reader) ──────────────────────────────────────────


class TestShmDataSourceStubbed:
    """Tests using injected stub reader — no real SHM needed."""

    def _make_source(
        self,
        reader: _StubReader,
        symbols: tuple[str, ...] = ("2330",),
    ):
        from hft_platform.ipc.shm_snapshot import _symbol_hash
        from hft_platform.monitor._data_source import ShmDataSource

        ds = ShmDataSource.__new__(ShmDataSource)
        ds._reader = reader
        ds._symbols = symbols
        ds._symbol_to_slot = {}
        ds._slot_versions = {}
        ds._rows_by_symbol = {s: [] for s in symbols}
        ds._connected = True
        ds._retry_count = 0
        ds._last_error = ""

        # Build symbol→slot mapping like __init__ does
        sym_hashes = {_symbol_hash(s): s for s in symbols}
        for slot_idx in range(reader.max_symbols):
            snap = reader.read_slot(slot_idx)
            if snap is not None and snap.symbol_hash in sym_hashes:
                ds._symbol_to_slot[sym_hashes[snap.symbol_hash]] = slot_idx

        return ds

    def test_poll_returns_new_data(self) -> None:
        from hft_platform.ipc.shm_snapshot import _symbol_hash

        reader = _StubReader(max_symbols=4)
        h = _symbol_hash("2330")
        reader._slots[0] = _StubSnapshotSlot(
            version=1,
            ts_ns=5000,
            symbol_hash=h,
            lob_fields=(1000, 2000, 0, 0, 0, 0, 10, 20, 0),
        )

        ds = self._make_source(reader, ("2330",))
        result = ds.poll({"2330": 0})
        assert len(result["2330"]) == 1
        assert result["2330"][0].ingest_ts == 5000

    def test_poll_skips_unchanged_version(self) -> None:
        from hft_platform.ipc.shm_snapshot import _symbol_hash

        reader = _StubReader(max_symbols=4)
        h = _symbol_hash("2330")
        reader._slots[0] = _StubSnapshotSlot(
            version=1,
            ts_ns=5000,
            symbol_hash=h,
            lob_fields=(1000, 2000, 0, 0, 0, 0, 10, 20, 0),
        )

        ds = self._make_source(reader, ("2330",))

        # First poll consumes version 1
        ds.poll({"2330": 0})

        # Second poll — same version, should be empty
        result = ds.poll({"2330": 0})
        assert len(result["2330"]) == 0

    def test_poll_skips_data_older_than_cursor(self) -> None:
        from hft_platform.ipc.shm_snapshot import _symbol_hash

        reader = _StubReader(max_symbols=4)
        h = _symbol_hash("2330")
        reader._slots[0] = _StubSnapshotSlot(
            version=1,
            ts_ns=3000,
            symbol_hash=h,
            lob_fields=(1000, 2000, 0, 0, 0, 0, 10, 20, 0),
        )

        ds = self._make_source(reader, ("2330",))

        # Cursor at 5000, data at 3000 — skip
        result = ds.poll({"2330": 5000})
        assert len(result["2330"]) == 0

    def test_poll_returns_data_newer_than_cursor(self) -> None:
        from hft_platform.ipc.shm_snapshot import _symbol_hash

        reader = _StubReader(max_symbols=4)
        h = _symbol_hash("2330")
        reader._slots[0] = _StubSnapshotSlot(
            version=1,
            ts_ns=8000,
            symbol_hash=h,
            lob_fields=(1000, 2000, 0, 0, 0, 0, 10, 20, 0),
        )

        ds = self._make_source(reader, ("2330",))
        result = ds.poll({"2330": 5000})
        assert len(result["2330"]) == 1

    def test_poll_empty_slot_returns_nothing(self) -> None:
        from hft_platform.ipc.shm_snapshot import _symbol_hash

        reader = _StubReader(max_symbols=4)
        h = _symbol_hash("2330")
        # Slot 0 mapped during init, but then becomes None
        reader._slots[0] = _StubSnapshotSlot(
            version=1,
            ts_ns=1000,
            symbol_hash=h,
            lob_fields=(1000, 2000, 0, 0, 0, 0, 10, 20, 0),
        )
        ds = self._make_source(reader, ("2330",))

        # Now remove the slot data
        reader._slots[0] = None
        result = ds.poll({"2330": 0})
        assert len(result["2330"]) == 0

    def test_poll_unknown_symbol_triggers_discover(self) -> None:
        from hft_platform.ipc.shm_snapshot import _symbol_hash

        reader = _StubReader(max_symbols=4)
        h = _symbol_hash("2881")
        # Slot 2 has 2881 but was not mapped at init (init only scanned existing slots)
        ds = self._make_source(reader, ("2881",))
        assert "2881" not in ds._symbol_to_slot

        # Now the slot appears
        reader._slots[2] = _StubSnapshotSlot(
            version=1,
            ts_ns=9000,
            symbol_hash=h,
            lob_fields=(500, 600, 0, 0, 0, 0, 5, 8, 0),
        )

        result = ds.poll({"2881": 0})
        assert len(result["2881"]) == 1
        assert ds._symbol_to_slot["2881"] == 2

    def test_no_reader_returns_empty(self) -> None:
        from hft_platform.monitor._data_source import ShmDataSource

        ds = ShmDataSource.__new__(ShmDataSource)
        ds._reader = None
        ds._symbols = ("2330",)
        ds._symbol_to_slot = {}
        ds._slot_versions = {}
        ds._rows_by_symbol = {"2330": []}
        ds._connected = False
        ds._retry_count = 0
        ds._last_error = ""

        assert ds.poll({"2330": 0}) == {}

    def test_fetch_recent_valid_raises(self) -> None:
        reader = _StubReader()
        ds = self._make_source(reader)
        with pytest.raises(NotImplementedError):
            ds.fetch_recent_valid("2330", 10)

    def test_properties(self) -> None:
        reader = _StubReader()
        ds = self._make_source(reader)
        assert ds.connected is True
        assert ds.retry_count == 0
        assert ds.last_error == ""
        assert ds.remaining_backoff_seconds() == 0.0
        assert ds.try_reconnect() is True

    def test_connect_is_noop(self) -> None:
        reader = _StubReader()
        ds = self._make_source(reader)
        ds.connect()  # should not raise


# ── HybridDataSource ───────────────────────────────────────────────────────


class TestHybridDataSource:
    def _make_shm_stub(self, *, connected: bool = True, poll_result=None, poll_raises: bool = False):
        """Create a minimal ShmDataSource-like stub."""

        class _ShmStub:
            __slots__ = ("_connected", "_poll_result", "_poll_raises")

            def __init__(self):
                self._connected = connected
                self._poll_result = poll_result or {}
                self._poll_raises = poll_raises

            @property
            def connected(self):
                return self._connected

            def poll(self, cursors):
                if self._poll_raises:
                    raise RuntimeError("SHM read failed")
                return self._poll_result

        return _ShmStub()

    def test_mode_label_both_ok(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm = self._make_shm_stub(connected=True)
        ch = CHDataSource(_StubPoller())
        hybrid = HybridDataSource(shm, ch)
        hybrid.connect()

        assert hybrid.mode_label == "SHM+CH"

    def test_mode_label_shm_only(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm = self._make_shm_stub(connected=True)
        ch = CHDataSource(_StubPoller(fail_connect=True))
        hybrid = HybridDataSource(shm, ch)
        hybrid.connect()

        assert hybrid.mode_label == "SHM"
        assert hybrid.connected is True

    def test_mode_label_ch_only(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm = self._make_shm_stub(connected=False)
        ch = CHDataSource(_StubPoller())
        hybrid = HybridDataSource(shm, ch)
        hybrid.connect()

        assert hybrid.mode_label == "CH"
        assert hybrid.connected is True

    def test_mode_label_none(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm = self._make_shm_stub(connected=False)
        ch = CHDataSource(_StubPoller(fail_connect=True))
        hybrid = HybridDataSource(shm, ch)
        hybrid.connect()

        assert hybrid.mode_label == "--"
        assert hybrid.connected is False

    def test_poll_prefers_shm(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm_row = _row(ts=9000)
        shm = self._make_shm_stub(connected=True, poll_result={"2330": [shm_row]})
        ch_poller = _StubPoller()
        ch_poller._poll_result = {"2330": [_row(ts=1000)]}
        ch = CHDataSource(ch_poller)

        hybrid = HybridDataSource(shm, ch)
        hybrid.connect()
        result = hybrid.poll({"2330": 0})

        assert result["2330"][0].ingest_ts == 9000  # SHM data, not CH

    def test_poll_falls_back_to_ch_on_shm_error(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm = self._make_shm_stub(connected=True, poll_raises=True)
        ch_poller = _StubPoller()
        ch_row = _row(ts=2000)
        ch_poller._poll_result = {"2330": [ch_row]}
        ch = CHDataSource(ch_poller)

        hybrid = HybridDataSource(shm, ch)
        hybrid.connect()

        result = hybrid.poll({"2330": 0})
        assert result["2330"][0].ingest_ts == 2000
        assert hybrid.mode_label == "CH"  # degraded

    def test_poll_returns_empty_when_both_down(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm = self._make_shm_stub(connected=False)
        ch = CHDataSource(_StubPoller(fail_connect=True))
        hybrid = HybridDataSource(shm, ch)
        hybrid.connect()

        assert hybrid.poll({"2330": 0}) == {}

    def test_fetch_recent_valid_uses_ch(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm = self._make_shm_stub(connected=True)
        ch_poller = _StubPoller()
        ch_poller._fetch_result = [_row()]
        ch = CHDataSource(ch_poller)

        hybrid = HybridDataSource(shm, ch)
        hybrid.connect()

        rows = hybrid.fetch_recent_valid("2330", 10)
        assert len(rows) == 1

    def test_fetch_recent_valid_empty_when_ch_down(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm = self._make_shm_stub(connected=True)
        ch = CHDataSource(_StubPoller(fail_connect=True))
        hybrid = HybridDataSource(shm, ch)
        hybrid.connect()

        assert hybrid.fetch_recent_valid("2330", 10) == []

    def test_try_reconnect_updates_mode(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm = self._make_shm_stub(connected=True)
        ch_poller = _StubPoller(fail_connect=True, reconnect_result=True)
        ch = CHDataSource(ch_poller)

        hybrid = HybridDataSource(shm, ch)
        hybrid.connect()
        assert hybrid.mode_label == "SHM"

        # Reconnect CH
        assert hybrid.try_reconnect() is True
        assert hybrid.mode_label == "SHM+CH"

    def test_delegates_retry_count_and_last_error(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource

        shm = self._make_shm_stub(connected=True)
        ch_poller = _StubPoller()
        ch_poller._retry_count = 5
        ch_poller._last_error = "connection refused"
        ch = CHDataSource(ch_poller)

        hybrid = HybridDataSource(shm, ch)
        assert hybrid.retry_count == 5
        assert hybrid.last_error == "connection refused"
        assert hybrid.remaining_backoff_seconds() == 0.0


# ── RedisHybridSource ──────────────────────────────────────────────────────


class TestRedisHybridSource:
    def test_mode_label_both_ok(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller()
        ch = CHDataSource(_StubPoller())
        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()

        assert hybrid.mode_label == "REDIS+CH"
        assert hybrid.connected is True

    def test_mode_label_redis_only(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller()
        ch = CHDataSource(_StubPoller(fail_connect=True))
        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()

        assert hybrid.mode_label == "REDIS"

    def test_mode_label_ch_only(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller(fail_connect=True)
        ch = CHDataSource(_StubPoller())
        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()

        assert hybrid.mode_label == "CH"

    def test_mode_label_none(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller(fail_connect=True)
        ch = CHDataSource(_StubPoller(fail_connect=True))
        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()

        assert hybrid.mode_label == "--"
        assert hybrid.connected is False

    def test_poll_prefers_redis(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis_row = _row(ts=7000)
        redis = _StubRedisPoller()
        redis._poll_result = {"2330": [redis_row]}
        ch_poller = _StubPoller()
        ch_poller._poll_result = {"2330": [_row(ts=1000)]}
        ch = CHDataSource(ch_poller)

        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()
        result = hybrid.poll({"2330": 0})

        assert result["2330"][0].ingest_ts == 7000

    def test_poll_falls_back_to_ch_on_redis_connection_error(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller(poll_raises=True)
        ch_poller = _StubPoller()
        ch_row = _row(ts=3000)
        ch_poller._poll_result = {"2330": [ch_row]}
        ch = CHDataSource(ch_poller)

        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()
        result = hybrid.poll({"2330": 0})

        assert result["2330"][0].ingest_ts == 3000
        assert hybrid.mode_label == "CH"

    def test_poll_returns_empty_when_both_down(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller(fail_connect=True)
        ch = CHDataSource(_StubPoller(fail_connect=True))
        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()

        assert hybrid.poll({"2330": 0}) == {}

    def test_fetch_recent_valid_prefers_ch(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller()
        redis._fetch_result = [_row(ts=1)]
        ch_poller = _StubPoller()
        ch_poller._fetch_result = [_row(ts=2)]
        ch = CHDataSource(ch_poller)

        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()

        rows = hybrid.fetch_recent_valid("2330", 10)
        assert rows[0].ingest_ts == 2  # CH preferred

    def test_fetch_recent_valid_falls_back_to_redis(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller()
        redis._fetch_result = [_row(ts=99)]
        ch = CHDataSource(_StubPoller(fail_connect=True))

        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()

        rows = hybrid.fetch_recent_valid("2330", 10)
        assert len(rows) == 1
        assert rows[0].ingest_ts == 99

    def test_fetch_recent_valid_empty_when_both_down(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller(fail_connect=True)
        ch = CHDataSource(_StubPoller(fail_connect=True))
        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()

        assert hybrid.fetch_recent_valid("2330", 10) == []

    def test_try_reconnect_redis_then_ch(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller(fail_connect=True)
        ch = CHDataSource(_StubPoller(fail_connect=True, reconnect_result=True))

        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()
        assert hybrid.mode_label == "--"

        # Redis still fails on reconnect (fail_connect=True), but CH reconnects
        result = hybrid.try_reconnect()
        assert result is True
        assert hybrid.mode_label == "CH"

    def test_try_reconnect_redis_recovers(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller(fail_connect=True)
        ch = CHDataSource(_StubPoller())

        hybrid = RedisHybridSource(redis, ch)
        hybrid.connect()
        assert hybrid.mode_label == "CH"

        # Now Redis recovers
        redis._fail_connect = False
        hybrid.try_reconnect()
        assert hybrid.mode_label == "REDIS+CH"

    def test_delegates_retry_count_last_error(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource

        redis = _StubRedisPoller()
        ch_poller = _StubPoller()
        ch_poller._retry_count = 7
        ch_poller._last_error = "disk full"
        ch = CHDataSource(ch_poller)

        hybrid = RedisHybridSource(redis, ch)
        assert hybrid.retry_count == 7
        assert hybrid.last_error == "disk full"
        assert hybrid.remaining_backoff_seconds() == 0.0


# ── Constants ───────────────────────────────────────────────────────────────


class TestConstants:
    def test_platform_to_ch_scale(self) -> None:
        from hft_platform.monitor._data_source import _PLATFORM_TO_CH_SCALE
        from hft_platform.monitor._types import CH_PRICE_SCALE, PLATFORM_SCALE

        assert _PLATFORM_TO_CH_SCALE == 100
        assert CH_PRICE_SCALE // PLATFORM_SCALE == _PLATFORM_TO_CH_SCALE
