"""Edge case and error path unit tests for DataSource implementations.

Tests HybridDataSource and RedisHybridSource fallback, degradation,
and reconnect behavior using stub objects (no real SHM/Redis/ClickHouse).
"""

from __future__ import annotations

from hft_platform.monitor._types import RowView

# ── Shared stub classes ──────────────────────────────────────────────────────


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
