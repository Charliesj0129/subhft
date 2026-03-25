"""Tests for DataSource protocol implementations (Phase 3-4)."""

from __future__ import annotations

import os
import time

import pytest

from hft_platform.monitor._types import RowView

# ── Helpers ──────────────────────────────────────────────────────────────────


def _unique_shm_name() -> str:
    return f"test_ds_{os.getpid()}_{int(time.monotonic_ns())}"


@pytest.fixture()
def _cleanup_shm():
    """Remove /dev/shm test segments after each test."""
    names: list[str] = []
    yield names
    for name in names:
        path = f"/dev/shm/{name}"
        if os.path.exists(path):
            os.unlink(path)


class _FakeCHSource:
    """Stub CHDataSource for testing HybridDataSource."""

    def __init__(self, *, fail_connect: bool = False) -> None:
        self._fail_connect = fail_connect
        self._connected = False
        self._retry_count = 0
        self._last_error = ""
        self._poll_result: dict[str, list[RowView]] = {}
        self._replay_result: list[RowView] = []

    def connect(self) -> None:
        if self._fail_connect:
            raise ConnectionError("CH unavailable")
        self._connected = True

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        return self._poll_result

    def fetch_recent_valid(self, symbol: str, limit: int, min_ingest_ts: int = 0) -> list[RowView]:
        return self._replay_result

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


# ── CHDataSource ─────────────────────────────────────────────────────────────


class TestCHDataSource:
    def test_delegates_to_poller(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource

        fake = _FakeCHSource()
        ds = CHDataSource(fake)
        ds.connect()
        assert ds.connected is True
        assert ds.retry_count == 0
        assert ds.last_error == ""
        assert ds.remaining_backoff_seconds() == 0.0
        assert ds.poll({}) == {}

    def test_fetch_recent_valid_delegates(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource

        fake = _FakeCHSource()
        fake._connected = True
        row = RowView("2330", 100, [100], [200], [10], [20], 100, 1)
        fake._replay_result = [row]
        ds = CHDataSource(fake)
        result = ds.fetch_recent_valid("2330", 10)
        assert len(result) == 1
        assert result[0].symbol == "2330"


# ── ShmDataSource ────────────────────────────────────────────────────────────


class TestShmDataSource:
    def test_shm_data_source_poll_reads_snapshots(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.ipc.shm_snapshot import ShmSnapshotWriter, _symbol_hash
        from hft_platform.monitor._data_source import ShmDataSource

        name = _unique_shm_name()
        _cleanup_shm.append(name)

        writer = ShmSnapshotWriter(name, max_symbols=4)
        sym_hash = _symbol_hash("2330")
        lob = [1000, 2000, 3000, 100, 500, 600, 10, 20, 3100]
        feat = list(range(16))
        writer.publish(0, 5000, sym_hash, lob, feat)

        ds = ShmDataSource(shm_name=name, max_symbols=4, symbols=("2330",))
        assert ds.connected is True

        # First poll — should return data
        result = ds.poll({"2330": 0})
        rows = result.get("2330", [])
        assert len(rows) == 1
        assert rows[0].symbol == "2330"
        assert rows[0].ingest_ts == 5000

        # Second poll with same cursor — version unchanged, should skip
        result = ds.poll({"2330": 0})
        rows = result.get("2330", [])
        assert len(rows) == 0

        # Write new data, poll again
        writer.publish(0, 6000, sym_hash, lob, feat)
        result = ds.poll({"2330": 0})
        rows = result.get("2330", [])
        assert len(rows) == 1
        assert rows[0].ingest_ts == 6000

    def test_shm_data_source_fetch_recent_not_supported(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.ipc.shm_snapshot import ShmSnapshotWriter
        from hft_platform.monitor._data_source import ShmDataSource

        name = _unique_shm_name()
        _cleanup_shm.append(name)

        ShmSnapshotWriter(name, max_symbols=4)
        ds = ShmDataSource(shm_name=name, max_symbols=4, symbols=("2330",))
        with pytest.raises(NotImplementedError):
            ds.fetch_recent_valid("2330", 10)

    def test_shm_data_source_graceful_when_missing(self) -> None:
        from hft_platform.monitor._data_source import ShmDataSource

        ds = ShmDataSource(shm_name="nonexistent_segment_xyz", max_symbols=4, symbols=("2330",))
        assert ds.connected is False
        assert ds.poll({"2330": 0}) == {}

    def test_shm_scale_conversion(self, _cleanup_shm: list[str]) -> None:
        """SHM uses x10000, RowView expects x1000000 — verify conversion."""
        from hft_platform.ipc.shm_snapshot import ShmSnapshotWriter, _symbol_hash
        from hft_platform.monitor._data_source import ShmDataSource

        name = _unique_shm_name()
        _cleanup_shm.append(name)

        writer = ShmSnapshotWriter(name, max_symbols=4)
        # best_bid=2100000 (x10000 = 210.0 NTD), best_ask=2105000 (x10000 = 210.5 NTD)
        sym_hash = _symbol_hash("2330")
        lob = [2100000, 2105000, 0, 0, 0, 0, 10, 20, 0]
        feat = [0] * 16
        writer.publish(0, 1000, sym_hash, lob, feat)

        ds = ShmDataSource(shm_name=name, max_symbols=4, symbols=("2330",))
        result = ds.poll({"2330": 0})
        row = result["2330"][0]

        # CH scale = x1000000, platform scale = x10000, conversion = *100
        assert row.bids_price[0] == 2100000 * 100  # 210_000_000
        assert row.asks_price[0] == 2105000 * 100  # 210_500_000


# ── HybridDataSource ────────────────────────────────────────────────────────


class TestHybridDataSource:
    def test_hybrid_poll_uses_shm_fetch_uses_ch(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.ipc.shm_snapshot import ShmSnapshotWriter, _symbol_hash
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource, ShmDataSource

        name = _unique_shm_name()
        _cleanup_shm.append(name)

        # Setup SHM with data
        writer = ShmSnapshotWriter(name, max_symbols=4)
        sym_hash = _symbol_hash("2330")
        lob = [1000, 2000, 0, 0, 0, 0, 10, 20, 0]
        writer.publish(0, 5000, sym_hash, lob, [0] * 16)

        shm = ShmDataSource(shm_name=name, max_symbols=4, symbols=("2330",))
        ch = _FakeCHSource()
        ch._connected = True
        replay_row = RowView("2330", 1000, [100], [200], [10], [20], 100, 1)
        ch._replay_result = [replay_row]

        hybrid = HybridDataSource(shm, CHDataSource(ch))
        hybrid.connect()

        assert hybrid.connected is True
        assert hybrid.mode_label == "SHM+CH"

        # poll → SHM
        result = hybrid.poll({"2330": 0})
        assert "2330" in result
        assert len(result["2330"]) == 1

        # fetch_recent_valid → CH
        rows = hybrid.fetch_recent_valid("2330", 10)
        assert len(rows) == 1
        assert rows[0].symbol == "2330"

    def test_hybrid_graceful_degradation_ch_only(self) -> None:
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource, ShmDataSource

        # SHM fails
        shm = ShmDataSource(shm_name="nonexistent_xyz_abc", max_symbols=4, symbols=("2330",))
        ch = _FakeCHSource()
        ch._poll_result = {"2330": [RowView("2330", 100, [100], [200], [10], [20], 100, 1)]}

        hybrid = HybridDataSource(shm, CHDataSource(ch))
        hybrid.connect()

        # SHM not connected → falls back to CH for poll
        assert hybrid.connected is True
        assert hybrid.mode_label == "CH"
        result = hybrid.poll({"2330": 0})
        assert len(result.get("2330", [])) == 1

    def test_hybrid_graceful_degradation_shm_only(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.ipc.shm_snapshot import ShmSnapshotWriter, _symbol_hash
        from hft_platform.monitor._data_source import CHDataSource, HybridDataSource, ShmDataSource

        name = _unique_shm_name()
        _cleanup_shm.append(name)

        writer = ShmSnapshotWriter(name, max_symbols=4)
        writer.publish(0, 5000, _symbol_hash("2330"), [1000, 2000, 0, 0, 0, 0, 10, 20, 0], [0] * 16)

        shm = ShmDataSource(shm_name=name, max_symbols=4, symbols=("2330",))
        ch = _FakeCHSource(fail_connect=True)

        hybrid = HybridDataSource(shm, CHDataSource(ch))
        hybrid.connect()

        assert hybrid.connected is True
        assert hybrid.mode_label == "SHM"

        # fetch_recent_valid returns empty (CH down)
        rows = hybrid.fetch_recent_valid("2330", 10)
        assert rows == []

    def test_auto_fallback_in_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When data_source=auto and no SHM exists, engine falls back to CH."""
        from hft_platform.monitor import _engine as engine_mod
        from hft_platform.monitor._types import MonitorConfig, MonitorState, WatchlistSymbol

        class _StubDispatcher:
            def load_alphas(self, *a, **kw):
                return ["qi"]

            def bind_symbol(self, ss):
                pass

            def dispatch(self, ss, p):
                pass

            def reset_symbol(self, ss):
                ss.alpha_states.clear()

            @property
            def weights(self):
                return {}

        class FakePoller:
            def __init__(self, **kw):
                self.connected = False
                self.retry_count = 0
                self.last_error = ""

            def connect(self):
                self.connected = True

            def poll(self, c):
                return {}

            def fetch_recent_valid(self, s, limit, min_ingest_ts=0):
                return []

            def try_reconnect(self):
                return True

            def remaining_backoff_seconds(self):
                return 0.0

        monkeypatch.setattr(engine_mod, "CHPoller", lambda **kw: FakePoller(**kw))
        monkeypatch.setattr(engine_mod, "get_session_info", lambda *a, **kw: (True, "", "Day"))
        monkeypatch.setattr(engine_mod, "get_session_start", lambda *a, **kw: None)

        # Force ShmDataSource to always fail connecting (simulates no SHM segment available)
        class _FailingShmDataSource:
            def __init__(self, **kw):
                self._connected = False

            @property
            def connected(self):
                return False

            def connect(self):
                pass

        monkeypatch.setattr(engine_mod, "ShmDataSource", lambda **kw: _FailingShmDataSource(**kw))

        config = MonitorConfig(
            symbols=(WatchlistSymbol(code="2330", name="TSMC", product_type="stock", alpha_ids=("qi",)),),
            data_source="auto",
        )
        engine = engine_mod.MonitorEngine(config)
        engine._dispatcher = _StubDispatcher()
        engine.initialize()

        assert engine.state != MonitorState.ERROR
        # Should have created CHDataSource (auto fallback since no SHM)
        from hft_platform.monitor._data_source import CHDataSource

        assert isinstance(engine._data_source, CHDataSource)
