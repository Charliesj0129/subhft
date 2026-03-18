"""Tests for detail panel rendering (Phase 3)."""

from __future__ import annotations

import pytest

pytest.importorskip("rich")

from hft_platform.monitor._detail_panel import build_detail_panel
from hft_platform.monitor._types import AlphaState, MonitorConfig, SymbolState, WatchlistSymbol


def _config() -> MonitorConfig:
    ws = WatchlistSymbol(code="2330", name="台積電", product_type="stock", alpha_ids=("queue_imbalance", "microprice_momentum"))
    return MonitorConfig(symbols=(ws,))


def _ss() -> SymbolState:
    ss = SymbolState(
        symbol=WatchlistSymbol(
            code="2330", name="台積電", product_type="stock",
            alpha_ids=("queue_imbalance", "microprice_momentum"),
        ),
        tick_count=200,
        composite=-2.5,
        last_price=650.0,
        spread_bps=26.0,
        bid_qty=120.0,
        ask_qty=85.0,
        ofi_l1_cum=342.0,
        last_update_ns=1_000_000_000,
    )
    qi = AlphaState(alpha_id="queue_imbalance", signal=-0.31, z_score=2.1)
    mm = AlphaState(alpha_id="microprice_momentum", signal=-0.15, z_score=1.4)
    ss.alpha_states = {"queue_imbalance": qi, "microprice_momentum": mm}
    return ss


def test_detail_panel_none_shows_placeholder() -> None:
    panel = build_detail_panel(None, _config(), {})
    assert "No symbol selected" in panel.renderable.plain


def test_detail_panel_shows_symbol_and_composite() -> None:
    ss = _ss()
    panel = build_detail_panel(ss, _config(), {"queue_imbalance": 0.5})
    text = panel.renderable.plain
    assert "QI" in text
    assert "MM" in text
    assert "Composite" in text


def test_detail_panel_shows_why_line() -> None:
    ss = _ss()
    panel = build_detail_panel(ss, _config(), {})
    text = panel.renderable.plain
    assert "WHY" in text
    assert "bearish" in text


def test_detail_panel_handles_disabled_alpha() -> None:
    ss = _ss()
    ss.alpha_states["queue_imbalance"].disabled = True
    panel = build_detail_panel(ss, _config(), {})
    text = panel.renderable.plain
    assert "OFF" in text
