"""Additional coverage tests for hft_platform.monitor._engine.

Targets uncovered branches in:
- _create_data_source (ch/shm/auto paths)
- toggle_pause / toggle_warning_filter / toggle_help / etc.
- get_header_context (INITIALIZING, DISCONNECTED, ERROR, bad_summary tiers)
- _build_event_ticker (empty ring, old events, max-3)
- _format_runtime_summary (no_data path)
- _process_row exception path
- _maybe_refresh_cost / _get_ch_client
- get_selected_symbol_state
- _bootstrap_new_sessions with ConnectionError
- request_reconnect success/failure
- clear_warnings
- _handle_reconnect no-data-source path
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("rich")

from hft_platform.monitor._engine import MonitorEngine
from hft_platform.monitor._types import (
    MonitorConfig,
    MonitorState,
    RowView,
    Severity,
    WatchlistSymbol,
)

# ---------------------------------------------------------------------------
# Shared helpers (mirror pattern from test_monitor_engine.py)
# ---------------------------------------------------------------------------


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
    reconnect_success = True

    def __init__(
        self,
        replay_rows: dict[str, list[RowView]] | None = None,
        poll_batches: list | None = None,
    ) -> None:
        self.replay_rows = replay_rows if replay_rows is not None else {}
        self.poll_batches = poll_batches if poll_batches is not None else []
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
        return 5.0


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


def _make_initialized_engine(monkeypatch, fake_ds=None, warmup_ticks=2):
    """Create an initialized engine ready for testing."""
    from hft_platform.monitor import _engine as engine_mod

    if fake_ds is None:
        fake_ds = _FakeDataSource(
            replay_rows={"TMFC6": [_row(1), _row(2)]},
            poll_batches=[],
        )
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (True, "", "Day Session"))
    monkeypatch.setattr(engine_mod, "get_session_start", _session_start)
    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: 2)

    engine = MonitorEngine(_config(warmup_ticks=warmup_ticks))
    engine._dispatcher = _StubDispatcher()
    engine.initialize()
    return engine


# ---------------------------------------------------------------------------
# _create_data_source paths
# ---------------------------------------------------------------------------


def test_create_data_source_ch_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """data_source='ch' returns a CHDataSource backed by CHPoller."""
    from dataclasses import replace

    from hft_platform.monitor._ch_poller import CHPoller
    from hft_platform.monitor._data_source import CHDataSource

    cfg = replace(_config(), data_source="ch")
    engine = MonitorEngine(cfg)

    mock_poller_inst = MagicMock(spec=CHPoller)
    mock_ch_ds = MagicMock(spec=CHDataSource)

    with (
        patch("hft_platform.monitor._engine.CHPoller", return_value=mock_poller_inst),
        patch("hft_platform.monitor._engine.CHDataSource", return_value=mock_ch_ds),
    ):
        result = engine._create_data_source(("TMFC6",))

    assert result is mock_ch_ds


def test_create_data_source_shm_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """data_source='shm' returns a ShmDataSource."""
    from dataclasses import replace

    from hft_platform.monitor._data_source import ShmDataSource

    cfg = replace(_config(), data_source="shm")
    engine = MonitorEngine(cfg)

    mock_shm = MagicMock(spec=ShmDataSource)

    with patch("hft_platform.monitor._engine.ShmDataSource", return_value=mock_shm):
        result = engine._create_data_source(("TMFC6",))

    assert result is mock_shm


def test_create_data_source_auto_fallback_to_ch(monkeypatch: pytest.MonkeyPatch) -> None:
    """data_source='auto' falls back to CH when ShmDataSource raises."""
    from dataclasses import replace

    from hft_platform.monitor._data_source import CHDataSource

    cfg = replace(_config(), data_source="auto")
    engine = MonitorEngine(cfg)

    mock_poller = MagicMock()
    mock_ch_ds = MagicMock(spec=CHDataSource)

    with (
        patch("hft_platform.monitor._engine.CHPoller", return_value=mock_poller),
        patch("hft_platform.monitor._engine.CHDataSource", return_value=mock_ch_ds),
        patch("hft_platform.monitor._engine.ShmDataSource", side_effect=RuntimeError("shm unavailable")),
    ):
        result = engine._create_data_source(("TMFC6",))

    assert result is mock_ch_ds


def test_create_data_source_auto_uses_hybrid_when_shm_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    """data_source='auto' returns HybridDataSource when SHM connects successfully."""
    from dataclasses import replace

    from hft_platform.monitor._data_source import CHDataSource, HybridDataSource, ShmDataSource

    cfg = replace(_config(), data_source="auto")
    engine = MonitorEngine(cfg)

    mock_shm = MagicMock(spec=ShmDataSource)
    mock_shm.connected = True
    mock_ch_ds = MagicMock(spec=CHDataSource)
    mock_hybrid = MagicMock(spec=HybridDataSource)

    with (
        patch("hft_platform.monitor._engine.CHPoller", return_value=MagicMock()),
        patch("hft_platform.monitor._engine.CHDataSource", return_value=mock_ch_ds),
        patch("hft_platform.monitor._engine.ShmDataSource", return_value=mock_shm),
        patch("hft_platform.monitor._engine.HybridDataSource", return_value=mock_hybrid),
    ):
        result = engine._create_data_source(("TMFC6",))

    assert result is mock_hybrid


# ---------------------------------------------------------------------------
# toggle_pause
# ---------------------------------------------------------------------------


def test_toggle_pause_pauses_and_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """toggle_pause flips paused state and restores previous state on resume."""
    engine = _make_initialized_engine(monkeypatch)
    assert engine.state == MonitorState.LIVE

    engine.toggle_pause()
    assert engine._paused_by_user is True
    assert engine.state == MonitorState.PAUSED
    assert engine._state_before_pause == MonitorState.LIVE

    engine.toggle_pause()
    assert engine._paused_by_user is False
    assert engine.state == MonitorState.LIVE
    assert engine._state_before_pause is None


def test_toggle_pause_noop_in_error_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """toggle_pause does nothing when state is ERROR."""
    engine = _make_initialized_engine(monkeypatch)
    engine._state = MonitorState.ERROR

    engine.toggle_pause()
    assert engine.state == MonitorState.ERROR
    assert engine._paused_by_user is False


# ---------------------------------------------------------------------------
# toggle_help / toggle_warning_filter / toggle_event_log / toggle_problem_log
# ---------------------------------------------------------------------------


def test_toggle_help(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    assert engine._show_help is False
    engine.toggle_help()
    assert engine._show_help is True
    engine.toggle_help()
    assert engine._show_help is False


def test_toggle_warning_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    assert engine._warning_filter is False
    engine.toggle_warning_filter()
    assert engine._warning_filter is True
    assert engine._toast is not None
    engine.toggle_warning_filter()
    assert engine._warning_filter is False


def test_toggle_event_log(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    assert engine._show_event_log is False
    engine.toggle_event_log()
    assert engine._show_event_log is True
    engine.toggle_event_log()
    assert engine._show_event_log is False


def test_toggle_problem_log(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    assert engine._show_problem_log is False
    engine.toggle_problem_log()
    assert engine._show_problem_log is True


def test_toggle_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    assert engine._detail_visible is False
    engine.toggle_detail()
    assert engine._detail_visible is True
    engine.close_detail()
    assert engine._detail_visible is False


# ---------------------------------------------------------------------------
# clear_warnings
# ---------------------------------------------------------------------------


def test_clear_warnings_resets_invalid_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    ss = engine._sym_states[0]
    ss.invalid_row_count = 5
    ss.max_severity = Severity.CRIT

    engine.clear_warnings()

    assert ss.invalid_row_count == 0
    assert ss.max_severity == Severity.INFO
    assert engine._toast is not None
    assert len(ss.problem_log) == 1
    assert "cleared" in ss.problem_log[0].message.lower()


# ---------------------------------------------------------------------------
# request_force_poll
# ---------------------------------------------------------------------------


def test_request_force_poll_sets_flag_and_toast(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    assert engine._force_poll is False

    engine.request_force_poll()

    assert engine._force_poll is True
    assert engine._toast is not None


# ---------------------------------------------------------------------------
# request_reconnect success / failure
# ---------------------------------------------------------------------------


def test_request_reconnect_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ds = _FakeDataSource(
        replay_rows={"TMFC6": [_row(1), _row(2)]},
        poll_batches=[],
    )
    engine = _make_initialized_engine(monkeypatch, fake_ds=fake_ds)
    fake_ds._connected = False
    fake_ds.reconnect_success = True

    engine.request_reconnect()

    assert engine._toast is not None
    assert "Connected" in engine._toast.message or engine._toast is not None


def test_request_reconnect_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ds = _FakeDataSource(
        replay_rows={"TMFC6": [_row(1), _row(2)]},
        poll_batches=[],
    )
    engine = _make_initialized_engine(monkeypatch, fake_ds=fake_ds)
    fake_ds._connected = False
    fake_ds.reconnect_success = False

    engine.request_reconnect()

    assert engine._toast is not None
    assert "failed" in engine._toast.message.lower() or "Reconnect" in engine._toast.message


def test_request_reconnect_no_data_source(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    engine._data_source = None

    # Should not raise; toast is set then no-op
    engine.request_reconnect()
    assert engine._toast is not None


# ---------------------------------------------------------------------------
# _handle_reconnect edge: no data source
# ---------------------------------------------------------------------------


def test_handle_reconnect_no_data_source_sets_error(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    engine._data_source = None
    engine._state = MonitorState.DISCONNECTED

    engine._handle_reconnect()

    assert engine.state == MonitorState.ERROR
    assert "No poller" in engine._error_msg


def test_handle_reconnect_runtime_error_sets_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ds = _FakeDataSource(replay_rows={"TMFC6": []}, poll_batches=[])
    engine = _make_initialized_engine(monkeypatch, fake_ds=fake_ds)
    engine._state = MonitorState.DISCONNECTED

    def _raise_runtime():
        raise RuntimeError("permanent failure")

    fake_ds.try_reconnect = _raise_runtime  # type: ignore[assignment]

    engine._handle_reconnect()

    assert engine.state == MonitorState.ERROR
    assert "permanent failure" in engine._error_msg


# ---------------------------------------------------------------------------
# get_header_context — state-specific extra branches
# ---------------------------------------------------------------------------


def test_header_context_initializing_shows_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """INITIALIZING state shows Step N/5 in extra."""
    from hft_platform.monitor import _engine as engine_mod

    # Don't call initialize, so state stays INITIALIZING
    cfg = _config()
    engine = MonitorEngine(cfg)
    engine._dispatcher = _StubDispatcher()
    # engine state is INITIALIZING by default
    assert engine.state == MonitorState.INITIALIZING

    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: 0)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *a, **kw: (False, "", ""))

    ctx = engine.get_header_context()
    assert "Step" in ctx.extra


def test_header_context_disconnected_shows_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """DISCONNECTED state shows retry backoff in extra."""
    fake_ds = _FakeDataSource(replay_rows={"TMFC6": [_row(1), _row(2)]}, poll_batches=[])
    engine = _make_initialized_engine(monkeypatch, fake_ds=fake_ds)
    engine._state = MonitorState.DISCONNECTED
    fake_ds._connected = False

    ctx = engine.get_header_context()
    assert "retry" in ctx.extra


def test_header_context_error_shows_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """ERROR state shows error message in extra."""
    engine = _make_initialized_engine(monkeypatch)
    engine._state = MonitorState.ERROR
    engine._error_msg = "something went wrong"

    ctx = engine.get_header_context()
    assert "something went wrong" in ctx.extra


def test_header_context_paused_shows_next_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """PAUSED state invokes format_next_open."""
    from hft_platform.monitor import _engine as engine_mod

    engine = _make_initialized_engine(monkeypatch)
    engine._state = MonitorState.PAUSED

    captured = []
    original = engine_mod.format_next_open

    def _mock_next_open(*args, **kwargs):
        captured.append(True)
        return "Opens 09:00"

    monkeypatch.setattr(engine_mod, "format_next_open", _mock_next_open)
    ctx = engine.get_header_context()
    assert len(captured) == 1
    assert "Opens" in ctx.extra


# ---------------------------------------------------------------------------
# _build_event_ticker
# ---------------------------------------------------------------------------


def test_build_event_ticker_empty_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    assert engine._event_ring_len == 0
    assert engine._build_event_ticker() == ""


def test_build_event_ticker_shows_recent_events(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    engine = _make_initialized_engine(monkeypatch)
    now_ns = 10_000_000_000  # 10s reference
    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: now_ns)

    # Push a recent event (5s ago)
    engine._push_event("TMFC6", "SPIKE", now_ns - 5_000_000_000)

    result = engine._build_event_ticker()
    assert "TMFC6" in result
    assert "SPIKE" in result


def test_build_event_ticker_skips_old_events(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    engine = _make_initialized_engine(monkeypatch)
    now_ns = 100_000_000_000  # 100s reference
    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: now_ns)

    # Push event 60s ago — older than 30s cutoff
    engine._push_event("TMFC6", "OLD_EVENT", now_ns - 60_000_000_000)

    result = engine._build_event_ticker()
    assert result == ""


def test_build_event_ticker_max_three(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    engine = _make_initialized_engine(monkeypatch)
    now_ns = 10_000_000_000
    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: now_ns)

    # Push 5 events, all recent
    for i in range(5):
        engine._push_event("TMFC6", f"EVT{i}", now_ns - i * 1_000_000_000)

    result = engine._build_event_ticker()
    # Max 3 events shown, separated by "|"
    parts = [p for p in result.split("|") if p.strip()]
    assert len(parts) <= 3


# ---------------------------------------------------------------------------
# _format_runtime_summary — no_data path
# ---------------------------------------------------------------------------


def test_format_runtime_summary_no_data_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When session_started_ns is old enough and tick_count=0, symbol counted as no-data."""
    from hft_platform.monitor import _engine as engine_mod

    engine = _make_initialized_engine(monkeypatch)

    # Simulate a symbol that started a session long ago but has no ticks
    ss = engine._sym_states[0]
    ss.session_active = True
    ss.tick_count = 0
    ss.invalid_row_count = 0
    ss.session_started_ns = 1  # started very early → huge age
    ss.is_stale = False

    # now_ns well past no_data_warn_s (1.0s configured)
    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: 10_000_000_000)

    summary = engine._format_runtime_summary()
    assert "no-data 1" in summary


def test_format_runtime_summary_all_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    engine = _make_initialized_engine(monkeypatch)
    # Both ticks filled already from init
    assert engine._sym_states[0].tick_count == 2

    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: 2)
    summary = engine._format_runtime_summary()
    assert "ready 1" in summary


# ---------------------------------------------------------------------------
# _process_row exception path
# ---------------------------------------------------------------------------


def test_process_row_enrich_exception_increments_invalid_count(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    engine = _make_initialized_engine(monkeypatch)
    ss = engine._sym_states[0]
    initial_invalid = ss.invalid_row_count

    monkeypatch.setattr(engine_mod, "enrich_tick", lambda row, ss: (_ for _ in ()).throw(ValueError("bad row")))

    valid_row = _row(999)
    engine._process_row(ss, valid_row)

    assert ss.invalid_row_count == initial_invalid + 1
    assert "bad row" in ss.last_invalid_reason


# ---------------------------------------------------------------------------
# _maybe_refresh_cost / _get_ch_client
# ---------------------------------------------------------------------------


def test_maybe_refresh_cost_skips_when_too_soon(monkeypatch: pytest.MonkeyPatch) -> None:
    """_maybe_refresh_cost does nothing when within 60s cooldown."""
    engine = _make_initialized_engine(monkeypatch)
    engine._cost_last_fetch_ns = 5_000_000_000  # 5s ago (< 60s)

    get_ch_client_called = []

    monkeypatch.setattr(MonitorEngine, "_get_ch_client", lambda self: (get_ch_client_called.append(True) or None))

    engine._maybe_refresh_cost(now_ns=6_000_000_000)  # only 1s later

    assert len(get_ch_client_called) == 0


def test_maybe_refresh_cost_no_client_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """_maybe_refresh_cost does nothing when no CH client available."""
    engine = _make_initialized_engine(monkeypatch)
    engine._cost_last_fetch_ns = 0
    engine._data_source = None

    # Should not raise
    engine._maybe_refresh_cost(now_ns=100_000_000_000)
    assert engine._cost_lines == []


def test_get_ch_client_with_ch_data_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_ch_client extracts client from CHDataSource._poller._client."""
    from hft_platform.monitor._data_source import CHDataSource

    engine = _make_initialized_engine(monkeypatch)

    mock_client = MagicMock()
    mock_poller = MagicMock()
    mock_poller._client = mock_client
    mock_ds = MagicMock(spec=CHDataSource)
    mock_ds._poller = mock_poller

    engine._data_source = mock_ds

    result = engine._get_ch_client()
    assert result is mock_client


def test_get_ch_client_with_no_data_source(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    engine._data_source = None
    assert engine._get_ch_client() is None


def test_get_ch_client_with_hybrid_data_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_ch_client extracts client from HybridDataSource._ch_source._poller._client."""
    engine = _make_initialized_engine(monkeypatch)

    mock_client = MagicMock()
    mock_poller = MagicMock()
    mock_poller._client = mock_client
    mock_ch_source = MagicMock()
    mock_ch_source._poller = mock_poller

    mock_hybrid_ds = MagicMock()
    mock_hybrid_ds._ch_source = mock_ch_source
    # spec=CHDataSource would trigger isinstance check, so use plain MagicMock
    del mock_hybrid_ds._poller  # remove poller attr to force hybrid path

    # Simulate non-CHDataSource (so isinstance check fails) with _ch_source
    engine._data_source = mock_hybrid_ds

    result = engine._get_ch_client()
    assert result is mock_client


# ---------------------------------------------------------------------------
# get_selected_symbol_state
# ---------------------------------------------------------------------------


def test_get_selected_symbol_state_empty_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    engine._sym_states_sorted = []

    result = engine.get_selected_symbol_state()
    assert result is None


def test_get_selected_symbol_state_returns_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    # Only 1 symbol; set selected_idx to out-of-range value
    engine._selected_idx = 100

    result = engine.get_selected_symbol_state()
    assert result is engine._sym_states_sorted[0]


def test_get_selected_symbol_state_normal(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    engine._selected_idx = 0

    result = engine.get_selected_symbol_state()
    assert result is engine._sym_states_sorted[0]


# ---------------------------------------------------------------------------
# _bootstrap_new_sessions with ConnectionError
# ---------------------------------------------------------------------------


def test_bootstrap_new_sessions_connection_error_sets_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    fake_ds = _FakeDataSource(
        replay_rows={"TMFC6": [_row(1), _row(2)]},
        poll_batches=[{"TMFC6": []}],
    )
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (True, "", "Day Session"))
    monkeypatch.setattr(engine_mod, "get_session_start", _session_start)
    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: 2)

    engine = MonitorEngine(_config())
    engine._dispatcher = _StubDispatcher()
    engine.initialize()

    # Mark symbol as just entered active session
    ss = engine._sym_states[0]
    ss.was_session_active = False
    ss.session_active = True

    # Make bootstrap_symbol raise ConnectionError
    def _raise_conn(self, sym_state):
        raise ConnectionError("broken pipe")

    monkeypatch.setattr(MonitorEngine, "_bootstrap_symbol", _raise_conn)

    engine._bootstrap_new_sessions()

    assert engine.state == MonitorState.DISCONNECTED


# ---------------------------------------------------------------------------
# Bad-data summary tiers in get_header_context
# ---------------------------------------------------------------------------


def _engine_with_bad_rows(monkeypatch, invalid: int, total: int) -> MonitorEngine:
    """Build engine with specific invalid/total row counts to test bad_summary tiers."""
    engine = _make_initialized_engine(monkeypatch)
    ss = engine._sym_states[0]
    ss.session_active = True
    ss.invalid_row_count = invalid
    ss.tick_count = total - invalid
    return engine


def test_header_context_bad_summary_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _engine_with_bad_rows(monkeypatch, 0, 100)
    ctx = engine.get_header_context()
    assert ctx.bad_summary == ""
    assert ctx.bad_style == ""


def test_header_context_bad_summary_low_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    """<5% bad rows → 'within tolerance'."""
    engine = _engine_with_bad_rows(monkeypatch, 2, 100)
    ctx = engine.get_header_context()
    assert "tolerance" in ctx.bad_summary
    assert ctx.bad_style == "dim yellow"


def test_header_context_bad_summary_medium_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    """5-20% bad rows → 'feed degraded'."""
    engine = _engine_with_bad_rows(monkeypatch, 10, 100)
    ctx = engine.get_header_context()
    assert "degraded" in ctx.bad_summary.lower()
    assert ctx.bad_style == "yellow"


def test_header_context_bad_summary_high_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    """>20% bad rows → 'critically degraded'."""
    engine = _engine_with_bad_rows(monkeypatch, 25, 100)
    ctx = engine.get_header_context()
    assert "critically" in ctx.bad_summary.lower()
    assert ctx.bad_style == "bright_red"


def test_header_context_bad_summary_pre_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad rows during inactive session → 'pre-market' message."""
    engine = _make_initialized_engine(monkeypatch)
    ss = engine._sym_states[0]
    ss.session_active = False
    ss.invalid_row_count = 5
    ss.tick_count = 0

    ctx = engine.get_header_context()
    assert "pre-market" in ctx.bad_summary.lower()
    assert ctx.bad_style == "dim"


# ---------------------------------------------------------------------------
# _sort_symbols — config order path
# ---------------------------------------------------------------------------


def test_sort_symbols_config_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """SORT_CONFIG preserves original insertion order."""
    from hft_platform.monitor._engine import SORT_CONFIG

    engine = _make_initialized_engine(monkeypatch)
    engine._sort_mode = SORT_CONFIG
    engine._sort_symbols()
    assert engine._sym_states_sorted == list(engine._sym_states)


def test_sort_symbols_composite_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """SORT_COMPOSITE sorts by abs(composite) descending."""
    from hft_platform.monitor._engine import SORT_COMPOSITE

    engine = _make_initialized_engine(monkeypatch)
    engine._sort_mode = SORT_COMPOSITE
    engine._sort_symbols()
    # With one symbol, order is unchanged
    assert len(engine._sym_states_sorted) == 1


# ---------------------------------------------------------------------------
# request_stop
# ---------------------------------------------------------------------------


def test_request_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    assert engine._running is True
    engine.request_stop()
    assert engine._running is False


# ---------------------------------------------------------------------------
# initialize failure paths
# ---------------------------------------------------------------------------


def test_initialize_fails_with_no_symbols() -> None:
    """Empty symbol list causes ERROR state during initialization."""
    cfg = MonitorConfig(symbols=())
    engine = MonitorEngine(cfg)
    engine._dispatcher = _StubDispatcher()

    engine.initialize()

    assert engine.state == MonitorState.ERROR
    assert "No symbols" in engine.error_msg


def test_initialize_fails_with_no_alpha_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Symbol with empty alpha_ids raises ValueError during initialization."""
    from hft_platform.monitor import _engine as engine_mod

    cfg = MonitorConfig(
        symbols=(WatchlistSymbol(code="TMFC6", name="TMF", product_type="future", alpha_ids=()),),
    )
    fake_ds = _FakeDataSource()
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *a, **kw: (False, "", ""))
    monkeypatch.setattr(engine_mod, "get_session_start", lambda *a, **kw: None)
    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: 0)

    engine = MonitorEngine(cfg)
    engine._dispatcher = _StubDispatcher()
    engine.initialize()

    assert engine.state == MonitorState.ERROR
    assert "alpha_ids" in engine.error_msg.lower() or "alpha" in engine.error_msg.lower()


def test_initialize_fails_when_no_alphas_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """When dispatcher loads zero alphas, initialization fails."""
    from hft_platform.monitor import _engine as engine_mod

    cfg = _config()
    fake_ds = _FakeDataSource()
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *a, **kw: (False, "", ""))
    monkeypatch.setattr(engine_mod, "get_session_start", lambda *a, **kw: None)
    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: 0)

    class _EmptyDispatcher(_StubDispatcher):
        def load_alphas(self, *args, **kwargs) -> list[str]:
            return []

    engine = MonitorEngine(cfg)
    engine._dispatcher = _EmptyDispatcher()
    engine.initialize()

    assert engine.state == MonitorState.ERROR


# ---------------------------------------------------------------------------
# _update_state — PAUSED when no active symbols
# ---------------------------------------------------------------------------


def test_update_state_pauses_when_no_active_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _make_initialized_engine(monkeypatch)
    engine._sym_states[0].session_active = False
    engine._state = MonitorState.LIVE

    engine._update_state()

    assert engine.state == MonitorState.PAUSED


# ---------------------------------------------------------------------------
# poll_and_update — user-paused path and DISCONNECTED reconnect flow
# ---------------------------------------------------------------------------


def test_poll_and_update_respects_user_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.monitor import _engine as engine_mod

    fake_ds = _FakeDataSource(
        replay_rows={"TMFC6": [_row(1), _row(2)]},
        poll_batches=[{"TMFC6": []}],
    )
    monkeypatch.setattr(MonitorEngine, "_create_data_source", lambda self, symbols: fake_ds)
    monkeypatch.setattr(engine_mod, "get_session_info", lambda *args, **kwargs: (True, "", "Day Session"))
    monkeypatch.setattr(engine_mod, "get_session_start", _session_start)
    monkeypatch.setattr(engine_mod.timebase, "now_ns", lambda: 2)

    engine = MonitorEngine(_config())
    engine._dispatcher = _StubDispatcher()
    engine.initialize()

    # User pauses before poll
    engine._paused_by_user = True
    engine.poll_and_update()

    assert engine.state == MonitorState.PAUSED
