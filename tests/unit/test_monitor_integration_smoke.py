"""Smoke test: verify all new monitor TUI components work together."""

from hft_platform.monitor._detail_panel import build_detail_panel as build_detail_dashboard
from hft_platform.monitor._enrichment import classify_problem
from hft_platform.monitor._renderer import (
    build_footer,
    build_help_overlay,
    compute_column_profile,
    format_contract_name,
    truncate_display,
)
from hft_platform.monitor._types import (
    AlphaState,
    MonitorConfig,
    ProblemEntry,
    Severity,
    SymbolState,
    WatchlistSymbol,
)


def test_full_pipeline():
    """End-to-end: classify problem, build dashboard, render footer."""
    ws = WatchlistSymbol(code="TXFD6", name="臺股期貨06", product_type="future")
    config = MonitorConfig(symbols=(ws,))
    ss = SymbolState(symbol=ws)

    # Simulate some data
    for i in range(10):
        ss.price_sparkline_append(32450.0 + i)
        ss.vol_sparkline_append(float(100 + i))
        ss.spread_sparkline_append(1.0 + i * 0.1)
        ss.imbal_sparkline_append(0.1 - i * 0.02)

    # Classify a problem
    sev = classify_problem("bids_price_empty", is_active=False, session_label="[PRE]")
    assert sev == Severity.INFO
    ss.problem_log.append(ProblemEntry(ts_ns=1_000_000_000, severity=sev, message="bids_price_empty"))

    # Add alpha with z-score sparkline
    alpha = AlphaState(alpha_id="QI", z_score=1.5)
    for _ in range(5):
        alpha.zscore_sparkline_append(1.5)
    ss.alpha_states["QI"] = alpha

    # Build detail panel
    panel = build_detail_dashboard(ss, config, weights={})
    assert panel is not None

    # Build footer
    footer = build_footer(detail_visible=True, paused=False, has_warnings=False, show_help=False)
    assert "[ESC]" in footer.plain

    # Help overlay
    overlay = build_help_overlay()
    assert overlay is not None

    # Column profile
    cp = compute_column_profile(150)
    assert cp.show_drivers is True

    # Contract name
    name = format_contract_name("TXFD6", "臺股期貨06")
    assert "台指期" in name

    # CJK truncation
    trunc = truncate_display("臺股期貨06月份", 10)
    assert trunc.endswith("…")
