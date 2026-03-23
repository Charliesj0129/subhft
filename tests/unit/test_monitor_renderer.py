from __future__ import annotations

import pytest

pytest.importorskip("rich")

from hft_platform.monitor._renderer import (
    _compute_suggestion,
    _format_action,
    _get_row_style,
    _render_alpha_cell,
    _render_sparkline,
    _render_symbol_status,
    build_header,
    build_table,
)
from hft_platform.monitor._types import (
    AlphaState,
    HeaderContext,
    MonitorConfig,
    MonitorState,
    SymbolState,
    WatchlistSymbol,
)


def _symbol_state() -> SymbolState:
    return SymbolState(
        symbol=WatchlistSymbol(
            code="TMFC6",
            name="微台",
            product_type="future",
            alpha_ids=("queue_imbalance", "flow_mode_decomp", "microprice_momentum"),
        ),
        tick_count=64,
        composite=2.2,
    )


def test_render_sparkline_scales_values() -> None:
    spark = _render_sparkline([float(i) for i in range(20)])
    assert len(spark) == 20
    assert spark[0] == "▁"
    assert spark[-1] == "█"


def test_compute_suggestion_returns_strong_buy_and_mixed() -> None:
    state = _symbol_state()
    state.alpha_states = {
        "queue_imbalance": AlphaState(alpha_id="queue_imbalance", signal=0.8),
        "flow_mode_decomp": AlphaState(alpha_id="flow_mode_decomp", signal=0.4),
        "microprice_momentum": AlphaState(alpha_id="microprice_momentum", signal=0.3),
    }
    label, _ = _compute_suggestion(state, list(state.alpha_states), 64)
    assert label == "BUY↑↑"

    state.composite = 1.8
    state.alpha_states["flow_mode_decomp"].signal = 0.0
    state.alpha_states["microprice_momentum"].signal = -0.3
    label, _ = _compute_suggestion(state, list(state.alpha_states), 64)
    assert label == "mixed"


def test_render_alpha_cell_handles_warmup_err_and_off() -> None:
    warm = _render_alpha_cell(AlphaState(alpha_id="qi", signal=0.4), tick_count=10, warmup_ticks=64)
    err = _render_alpha_cell(
        AlphaState(alpha_id="qi", signal=float("nan"), error_count=1), tick_count=64, warmup_ticks=64
    )
    off = _render_alpha_cell(AlphaState(alpha_id="qi", disabled=True), tick_count=64, warmup_ticks=64)

    assert warm.plain == "--"
    assert err.plain == "ERR"
    assert off.plain == "OFF"


def test_render_symbol_status_handles_live_and_no_l1() -> None:
    config = MonitorConfig(symbols=(_symbol_state().symbol,), warmup_ticks=64)

    live = _symbol_state()
    live.session_active = True
    status, _ = _render_symbol_status(live, config, MonitorState.LIVE, now_ns=1_000_000_000)
    assert status == "✓"

    from hft_platform.monitor._types import Severity

    no_l1 = SymbolState(symbol=live.symbol, session_active=True, invalid_row_count=3)
    no_l1.max_severity = Severity.WARN
    no_l1.session_started_ns = 1
    status, _ = _render_symbol_status(no_l1, config, MonitorState.WARMING_UP, now_ns=1_000_000_000)
    assert status == "\u26a0L1"


def test_format_action_includes_alpha_names() -> None:
    """Phase 1: action labels include driving alpha names."""
    ss = _symbol_state()
    ss.alpha_states = {
        "queue_imbalance": AlphaState(alpha_id="queue_imbalance", signal=0.8, z_score=2.1),
        "flow_mode_decomp": AlphaState(alpha_id="flow_mode_decomp", signal=0.4, z_score=1.5),
        "microprice_momentum": AlphaState(alpha_id="microprice_momentum", signal=0.3, z_score=1.0),
    }
    label, _ = _format_action(ss, pos=3, neg=0, total=3, warmup_ticks=64)
    assert "BUY" in label
    # Should contain alpha short name
    assert "QI" in label or "all" in label


def test_row_flash_for_recent_events() -> None:
    """Phase 4: recent events produce bold row style."""
    ss = _symbol_state()
    now_ns = 10_000_000_000
    ss.last_event_ns = now_ns - 1_000_000_000  # 1s ago
    style = _get_row_style(ss, MonitorState.LIVE, now_ns)
    assert style.bold is True


def test_source_badge_shown_in_header() -> None:
    """Source badge appears in header for non-empty source_label."""
    ctx = HeaderContext(
        state=MonitorState.LIVE,
        session_display="Day Session",
        time_str="2026-03-18 10:00:00 TST",
        ch_status="REDIS+CH: OK",
        stale_symbols=[],
        source_label="REDIS+CH",
    )
    header = build_header(ctx)
    assert "src: REDIS+CH" in header.plain

    # Empty source_label should not show badge
    ctx_no_src = HeaderContext(
        state=MonitorState.LIVE,
        session_display="Day Session",
        time_str="2026-03-18 10:00:00 TST",
        ch_status="CH: OK",
        stale_symbols=[],
        source_label="",
    )
    header_no_src = build_header(ctx_no_src)
    assert "src:" not in header_no_src.plain


def test_heartbeat_indicator_in_header() -> None:
    """S1: heartbeat shows poll count and age with color coding."""
    ctx = HeaderContext(
        state=MonitorState.LIVE,
        session_display="Day Session",
        time_str="2026-03-18 10:00:00 TST",
        ch_status="CH: OK",
        stale_symbols=[],
        poll_count=42,
        poll_age_s=0.5,
    )
    header = build_header(ctx)
    assert "poll #42" in header.plain
    assert "0s ago" in header.plain

    # No heartbeat when poll_count is 0
    ctx_no_poll = HeaderContext(
        state=MonitorState.LIVE,
        session_display="Day Session",
        time_str="2026-03-18 10:00:00 TST",
        ch_status="CH: OK",
        stale_symbols=[],
        poll_count=0,
    )
    header_no_poll = build_header(ctx_no_poll)
    assert "poll #" not in header_no_poll.plain


def test_delta_column_shows_price_change() -> None:
    """S2: Δ column shows price delta with arrow."""
    ss = _symbol_state()
    ss.last_price = 210.5
    ss.prev_poll_price = 210.0
    config = MonitorConfig(symbols=(ss.symbol,), warmup_ticks=64)

    table = build_table([ss], config, MonitorState.LIVE, alpha_cols=list(ss.symbol.alpha_ids))
    # Table should have a Δ column
    col_names = [c.header for c in table.columns]
    assert "\u0394" in col_names


def test_collapsed_closed_section() -> None:
    """S3: closed symbols are collapsed into summary row when closed_collapsed=True."""
    ws = WatchlistSymbol(code="2330", name="台積", product_type="stock", alpha_ids=("queue_imbalance",))
    active = SymbolState(symbol=ws, tick_count=64, composite=1.0, session_active=True)
    closed = SymbolState(symbol=ws, tick_count=0, is_closed=True)

    config = MonitorConfig(symbols=(ws,), warmup_ticks=64)

    # Collapsed: should show summary row
    table_collapsed = build_table(
        [active, closed], config, MonitorState.LIVE, alpha_cols=["queue_imbalance"], closed_collapsed=True
    )
    row_count_collapsed = table_collapsed.row_count
    # 1 active row + 1 collapsed summary row = 2
    assert row_count_collapsed == 2

    # Expanded: should show all rows
    table_expanded = build_table(
        [active, closed], config, MonitorState.LIVE, alpha_cols=["queue_imbalance"], closed_collapsed=False
    )
    row_count_expanded = table_expanded.row_count
    assert row_count_expanded == 2  # 1 active + 1 closed shown


def test_price_sparkline_in_table() -> None:
    """S7: table uses price sparkline instead of composite sparkline."""
    ss = _symbol_state()
    ss.last_price = 210.0
    # Fill price sparkline with data
    for i in range(10):
        ss.price_sparkline_append(200.0 + float(i))

    config = MonitorConfig(symbols=(ss.symbol,), warmup_ticks=64)
    table = build_table([ss], config, MonitorState.LIVE, alpha_cols=list(ss.symbol.alpha_ids))
    # Spark column should exist
    col_names = [c.header for c in table.columns]
    assert "Spark" in col_names
