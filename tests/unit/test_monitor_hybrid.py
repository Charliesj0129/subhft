"""Tests for RedisHybridSource and hybrid engine wiring."""

from __future__ import annotations

import pytest

pytest.importorskip("rich")

from hft_platform.monitor._data_source import CHDataSource, RedisHybridSource
from hft_platform.monitor._types import RowView


class _FakeCHPoller:
    __slots__ = ("connected", "retry_count", "last_error", "_poll_data", "_recent_data")

    def __init__(self) -> None:
        self.connected = False
        self.retry_count = 0
        self.last_error = ""
        self._poll_data: dict[str, list[RowView]] = {}
        self._recent_data: dict[str, list[RowView]] = {}

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        return self._poll_data

    def fetch_recent_valid(self, symbol: str, limit: int, min_ingest_ts: int = 0) -> list[RowView]:
        return self._recent_data.get(symbol, [])[:limit]

    def try_reconnect(self) -> bool:
        self.connected = True
        return True

    def remaining_backoff_seconds(self) -> float:
        return 0.0


class _FakeRedisPoller:
    __slots__ = ("connected", "_poll_data", "_should_fail")

    def __init__(self) -> None:
        self.connected = False
        self._poll_data: dict[str, list[RowView]] = {}
        self._should_fail = False

    def connect(self) -> None:
        if self._should_fail:
            raise ConnectionError("redis down")
        self.connected = True

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        if self._should_fail:
            raise ConnectionError("redis poll failed")
        return self._poll_data

    def fetch_recent_valid(self, symbol: str, limit: int, min_ingest_ts: int = 0) -> list[RowView]:
        return []


def _row(symbol: str, ts: int) -> RowView:
    return RowView(
        symbol=symbol,
        ingest_ts=ts,
        bids_price=[210_000_000],
        asks_price=[210_500_000],
        bids_vol=[10],
        asks_vol=[8],
        price_scaled=0,
        volume=1,
    )


def test_hybrid_connects_both_sources() -> None:
    redis = _FakeRedisPoller()
    ch = CHDataSource(_FakeCHPoller())
    hybrid = RedisHybridSource(redis_poller=redis, ch_source=ch)

    hybrid.connect()
    assert hybrid.connected is True
    assert hybrid.mode_label == "REDIS+CH"


def test_hybrid_poll_delegates_to_redis() -> None:
    redis = _FakeRedisPoller()
    redis._poll_data = {"TMFC6": [_row("TMFC6", 100)]}
    ch_poller = _FakeCHPoller()
    ch_poller._poll_data = {"TMFC6": [_row("TMFC6", 50)]}
    ch = CHDataSource(ch_poller)
    hybrid = RedisHybridSource(redis_poller=redis, ch_source=ch)
    hybrid.connect()

    result = hybrid.poll({"TMFC6": 0})
    assert result["TMFC6"][0].ingest_ts == 100  # Redis data, not CH


def test_hybrid_fetch_recent_delegates_to_ch() -> None:
    redis = _FakeRedisPoller()
    ch_poller = _FakeCHPoller()
    ch_poller._recent_data = {"TMFC6": [_row("TMFC6", 1), _row("TMFC6", 2)]}
    ch = CHDataSource(ch_poller)
    hybrid = RedisHybridSource(redis_poller=redis, ch_source=ch)
    hybrid.connect()

    rows = hybrid.fetch_recent_valid("TMFC6", limit=2)
    assert len(rows) == 2
    assert rows[0].ingest_ts == 1


def test_hybrid_falls_back_to_ch_on_redis_failure() -> None:
    redis = _FakeRedisPoller()
    redis._should_fail = True
    ch_poller = _FakeCHPoller()
    ch_poller._poll_data = {"TMFC6": [_row("TMFC6", 99)]}
    ch = CHDataSource(ch_poller)
    hybrid = RedisHybridSource(redis_poller=redis, ch_source=ch)
    hybrid.connect()

    # Redis connect failed, but CH OK
    assert hybrid.connected is True
    assert hybrid.mode_label == "CH"

    result = hybrid.poll({"TMFC6": 0})
    assert result["TMFC6"][0].ingest_ts == 99


def test_hybrid_mode_label_updates_on_degradation() -> None:
    redis = _FakeRedisPoller()
    ch_poller = _FakeCHPoller()
    ch = CHDataSource(ch_poller)
    hybrid = RedisHybridSource(redis_poller=redis, ch_source=ch)
    hybrid.connect()
    assert hybrid.mode_label == "REDIS+CH"

    # Simulate Redis poll failure
    redis._should_fail = True
    hybrid.poll({"TMFC6": 0})  # Redis fails, falls back to CH
    assert hybrid.mode_label == "CH"


def test_hybrid_try_reconnect_restores_redis() -> None:
    redis = _FakeRedisPoller()
    ch_poller = _FakeCHPoller()
    ch = CHDataSource(ch_poller)
    hybrid = RedisHybridSource(redis_poller=redis, ch_source=ch)
    hybrid.connect()

    # Simulate Redis down
    hybrid._redis_ok = False
    hybrid._update_mode_label()
    assert hybrid.mode_label == "CH"

    # Reconnect restores Redis
    redis._should_fail = False
    result = hybrid.try_reconnect()
    assert result is True
    assert hybrid.mode_label == "REDIS+CH"
