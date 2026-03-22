"""Core unit tests for DataSource implementations.

Tests CHDataSource, ShmDataSource, and _snapshot_to_row_view
using stub objects (no real SHM/Redis/ClickHouse).
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
        ds._next_retry_at = 0.0

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
        ds._next_retry_at = 0.0

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
        assert ds.connected is True


# ── Constants ───────────────────────────────────────────────────────────────


class TestConstants:
    def test_platform_to_ch_scale(self) -> None:
        from hft_platform.monitor._data_source import _PLATFORM_TO_CH_SCALE
        from hft_platform.monitor._types import CH_PRICE_SCALE, PLATFORM_SCALE

        assert _PLATFORM_TO_CH_SCALE == 100
        assert CH_PRICE_SCALE // PLATFORM_SCALE == _PLATFORM_TO_CH_SCALE
