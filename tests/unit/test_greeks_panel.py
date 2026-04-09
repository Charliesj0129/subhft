"""Tests for PortfolioGreeksSnapshot and Greeks panel rendering."""

import pytest


def test_snapshot_from_dict():
    from hft_platform.monitor._greeks_panel import PortfolioGreeksSnapshot

    data = {
        "ts": 1000000000,
        "net_delta_lots": 12.3,
        "net_gamma_lots": 3.1,
        "net_theta_ntd": -45200.0,
        "net_vega_ntd": 82000.0,
        "worst_pnl_ntd": -156000.0,
        "eye_state": "QUOTING",
    }
    snap = PortfolioGreeksSnapshot.from_dict(data)
    assert snap.net_delta_lots == pytest.approx(12.3)
    assert snap.eye_state == "QUOTING"


def test_snapshot_from_empty_dict():
    from hft_platform.monitor._greeks_panel import PortfolioGreeksSnapshot

    snap = PortfolioGreeksSnapshot.from_dict({})
    assert snap.net_delta_lots == 0.0 and snap.eye_state == "UNKNOWN"


def test_render_greeks_panel():
    from hft_platform.monitor._greeks_panel import PortfolioGreeksSnapshot, render_greeks_panel

    snap = PortfolioGreeksSnapshot(
        ts=1000000000,
        net_delta_lots=12.3,
        net_gamma_lots=3.1,
        net_theta_ntd=-45200.0,
        net_vega_ntd=82000.0,
        worst_pnl_ntd=-156000.0,
        eye_state="QUOTING",
    )
    lines = render_greeks_panel(snap, delta_limit=50, gamma_limit=20)
    assert isinstance(lines, list) and len(lines) >= 5
    text = "\n".join(str(line) for line in lines)
    assert "12.3" in text and "QUOTING" in text


def test_render_greeks_panel_none():
    from hft_platform.monitor._greeks_panel import render_greeks_panel

    lines = render_greeks_panel(None)
    text = "\n".join(str(line) for line in lines)
    assert "unavailable" in text.lower()
