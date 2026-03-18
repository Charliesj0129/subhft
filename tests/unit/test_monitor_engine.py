from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("rich")

from hft_platform.monitor._engine import SORT_COMPOSITE, SORT_OPPORTUNITY, MonitorEngine
from hft_platform.monitor._types import MonitorConfig, MonitorState, RowView, WatchlistSymbol


class _StubDispatcher:
    def load_alphas(self, *args, **kwargs) -> list[str]:
        return ["queue_imbalance"]

    def bind_symbol(self, sym_state) -> None:
        return None

    def dispatch(self, sym_state, payload) -> None:
        return None

    def reset_symbol(self, sym_state) -> None:
        sym_state.alpha_states.clear()

    @property
    def weights(self) -> dict[str, float]:
        return {}


class _FakeDataSource:
    """Implements the DataSource protocol for testing."""

    replay_rows: dict[str, list[RowView]] = {}
    poll_batches: list[dict[str, list[RowView]] | Exception] = []
    reconnect_success = True

    def __init__(self) -> None:
        self._connected = False
        self._retry_count = 0
        self._last_error = "down"
        self.heartbeat_stale = False

    def connect(self) -> None:
        self._connected = True

    def poll(self, cursors: dict[str, int]) -> dict[str, list[RowView]]:
        batch = self.poll_batches.pop(0)
        if isinstance(batch, Exception):
            self._connected = False
            raise batch
        return batch

    def fetch_recent_valid(self, symbol: str, limit: int, min_ingest_ts: int = 0) -> list[RowView]:
        rows = list(self.replay_rows.get(symbol, []))
        return [row for row in rows if row.ingest_ts >= min_ingest_ts][-limit:]

    def try_reconnect(self) -> bool:
        self._retry_count += 1
        self._connected = self.reconnect_success
        return self.reconnect_success

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


def _config(warmup_ticks: int = 2, stale_threshold_s: float = 6.0) -> MonitorConfig:
    return MonitorConfig(
        symbols=(
            WatchlistSymbol(
                code="TMFC6",
                name="TMF",
                product_type="future",
                alpha_ids=("queue_imbalance",),
            ),
        ),
        poll_interval_s=2.0,
        warmup_ticks=warmup_ticks,
        stale_threshold_s=stale_threshold_s,
        no_data_warn_s=1.0,
        replay_ticks=warmup_ticks,
        max_retries=3,
    )


def _row(ingest_ts: int, bid_qty: float = 10.0, ask_qty: float = 8.0) -> RowView:
    return RowView(
        symbol="TMFC6",
        ingest_ts=ingest_ts,
        bids_price=[210_000_000],
        asks_price=[210_500_000],
        bids_vol=[bid_qty],
        asks_vol=[ask_qty],
        price_scaled=0,
        volume=1,
    )


def _session_start(*args, **kwargs) -> dt.datetime:
    return dt.datetime(1970, 1, 1, 0, 0, tzinfo=dt.timezone.utc)


def test_engine_initializes_with_replay_and_reaches_live(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    fake_ds = _FakeDataSource()
    _FakeDataSource.replay_rows = {"TMFC6": [_row(1), _row(2)]}
    _FakeDataSource.poll_batches = [{"TMFC6": []}]
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (True, "", "Day Session"))
    monkeypatch.setattr(engine_mod, "get_session_start", _session_start)
    monkeypatch.setattr(engine_mod.time, "time_ns", lambda: 2)

    engine = MonitorEngine(_config())
    engine._dispatcher = _StubDispatcher()

    engine.initialize()
    assert engine.state == MonitorState.LIVE
    assert engine._sym_states[0].tick_count == 2


def test_engine_marks_stale_and_handles_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    fake_ds = _FakeDataSource()
    _FakeDataSource.replay_rows = {"TMFC6": [_row(100)]}
    _FakeDataSource.poll_batches = [{"TMFC6": []}]
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (True, "", "Day Session"))
    monkeypatch.setattr(engine_mod, "get_session_start", _session_start)

    now_values = iter([100, 8_000_000_000, 8_000_000_000, 8_000_000_000])
    monkeypatch.setattr(engine_mod.time, "time_ns", lambda: next(now_values))

    engine = MonitorEngine(_config(warmup_ticks=1, stale_threshold_s=6.0))
    engine._dispatcher = _StubDispatcher()

    engine.initialize()
    assert engine.state == MonitorState.LIVE

    engine.poll_and_update()
    assert engine.state == MonitorState.STALE
    assert engine._sym_states[0].is_stale is True

    # Replace data source with a fresh fake that raises on poll
    fake_ds2 = _FakeDataSource()
    fake_ds2._connected = True
    engine._data_source = fake_ds2
    monkeypatch.setattr(fake_ds2, "poll", lambda cursors: (_ for _ in ()).throw(ConnectionError("down")))
    engine._state = MonitorState.LIVE
    engine.poll_and_update()
    assert engine.state == MonitorState.DISCONNECTED

    monkeypatch.setattr(fake_ds2, "try_reconnect", lambda: True)
    engine.poll_and_update()
    assert engine.state == MonitorState.LIVE


def test_engine_pauses_when_all_symbols_are_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    fake_ds = _FakeDataSource()
    _FakeDataSource.replay_rows = {}
    _FakeDataSource.poll_batches = [{"TMFC6": []}]
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (False, "[CLOSED]", "Closed"))
    monkeypatch.setattr(engine_mod, "get_session_start", lambda *args, **kwargs: None)

    engine = MonitorEngine(_config())
    engine._dispatcher = _StubDispatcher()

    engine.initialize()
    engine.poll_and_update()

    assert engine.state == MonitorState.PAUSED


def test_engine_skips_invalid_rows_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    invalid = RowView(
        symbol="TMFC6",
        ingest_ts=101,
        bids_price=[],
        asks_price=[],
        bids_vol=[],
        asks_vol=[],
        price_scaled=0,
        volume=0,
    )

    fake_ds = _FakeDataSource()
    _FakeDataSource.replay_rows = {}
    _FakeDataSource.poll_batches = [{"TMFC6": [invalid, _row(102)]}]
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (True, "", "Day Session"))
    monkeypatch.setattr(engine_mod, "get_session_start", _session_start)
    monkeypatch.setattr(engine_mod.time, "time_ns", lambda: 102)

    engine = MonitorEngine(_config(warmup_ticks=1))
    engine._dispatcher = _StubDispatcher()

    engine.initialize()
    assert engine.state == MonitorState.WARMING_UP

    engine.poll_and_update()

    assert engine.state == MonitorState.LIVE
    assert engine._sym_states[0].invalid_row_count == 1
    assert engine._sym_states[0].cursor_ts_ns == 102


def test_engine_navigation_moves_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    fake_ds = _FakeDataSource()
    _FakeDataSource.replay_rows = {"TMFC6": [_row(1), _row(2)]}
    _FakeDataSource.poll_batches = []
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (True, "", "Day Session"))
    monkeypatch.setattr(engine_mod, "get_session_start", _session_start)
    monkeypatch.setattr(engine_mod.time, "time_ns", lambda: 2)

    engine = MonitorEngine(_config())
    engine._dispatcher = _StubDispatcher()
    engine.initialize()

    assert engine._selected_idx == 0
    engine.move_selection(1)
    # Only 1 symbol, clamped to 0
    assert engine._selected_idx == 0
    engine.move_selection(-1)
    assert engine._selected_idx == 0


def test_engine_initializes_with_redis_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config with source='redis' should show Redis in header status."""
    from dataclasses import replace

    from hft_platform.monitor import _engine as engine_mod

    redis_config = replace(_config(), source="redis", redis_host="127.0.0.1", redis_port=6379)

    fake_ds = _FakeDataSource()
    _FakeDataSource.replay_rows = {"TMFC6": [_row(1), _row(2)]}
    _FakeDataSource.poll_batches = []

    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (True, "", "Day Session"))
    monkeypatch.setattr(engine_mod, "get_session_start", _session_start)
    monkeypatch.setattr(engine_mod.time, "time_ns", lambda: 2)

    engine = MonitorEngine(redis_config)
    engine._dispatcher = _StubDispatcher()
    engine.initialize()

    assert engine.state == MonitorState.LIVE
    header_ctx = engine.get_header_context()
    # source="redis" in config → source_label falls through to "Redis"
    assert "redis" in header_ctx.ch_status.lower()


def test_engine_sort_mode_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    fake_ds = _FakeDataSource()
    _FakeDataSource.replay_rows = {"TMFC6": [_row(1)]}
    _FakeDataSource.poll_batches = []
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (True, "", "Day Session"))
    monkeypatch.setattr(engine_mod, "get_session_start", _session_start)
    monkeypatch.setattr(engine_mod.time, "time_ns", lambda: 1)

    engine = MonitorEngine(_config(warmup_ticks=1))
    engine._dispatcher = _StubDispatcher()
    engine.initialize()

    assert engine._sort_mode == SORT_OPPORTUNITY
    engine.cycle_sort_mode()
    assert engine._sort_mode == SORT_COMPOSITE
    engine.cycle_sort_mode()
    # config order
    assert engine._sort_mode == 2
    engine.cycle_sort_mode()
    assert engine._sort_mode == SORT_OPPORTUNITY


def test_engine_initializes_with_hybrid_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config with source='hybrid' should show hybrid mode label in header status."""
    from dataclasses import replace
    from unittest.mock import MagicMock

    from hft_platform.monitor import _engine as engine_mod
    from hft_platform.monitor._data_source import RedisHybridSource

    hybrid_config = replace(
        _config(),
        source="hybrid",
        redis_host="127.0.0.1",
        redis_port=6379,
    )

    _FakeDataSource.replay_rows = {"TMFC6": [_row(1), _row(2)]}
    _FakeDataSource.poll_batches = []

    # Create a MagicMock that passes isinstance(ds, RedisHybridSource) check
    fake_ds = MagicMock(spec=RedisHybridSource)
    fake_ds.connected = True
    fake_ds.retry_count = 0
    fake_ds.last_error = ""
    fake_ds.mode_label = "REDIS+CH"
    fake_ds.remaining_backoff_seconds.return_value = 0.0

    inner_fake = _FakeDataSource()
    fake_ds.connect.side_effect = lambda: None
    fake_ds.poll.side_effect = inner_fake.poll
    fake_ds.fetch_recent_valid.side_effect = inner_fake.fetch_recent_valid

    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (True, "", "Day Session"))
    monkeypatch.setattr(engine_mod, "get_session_start", _session_start)
    monkeypatch.setattr(engine_mod.time, "time_ns", lambda: 2)

    engine = MonitorEngine(hybrid_config)
    engine._dispatcher = _StubDispatcher()
    engine.initialize()

    assert engine.state == MonitorState.LIVE
    header_ctx = engine.get_header_context()
    assert "REDIS+CH" in header_ctx.ch_status
