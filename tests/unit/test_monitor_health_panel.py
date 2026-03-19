"""Tests for the Monitor TUI System Health Panel."""

from __future__ import annotations

from hft_platform.monitor._health_panel import (
    HealthState,
    _parse_metric,
    build_health_panel,
    is_health_visible,
    toggle_health_visible,
)


class TestParseMetric:
    def test_simple_gauge(self) -> None:
        assert _parse_metric(["pnl 12345.0"], "pnl") == 12345.0

    def test_labelled(self) -> None:
        lines = ['stormguard_mode{strategy="system"} 2.0']
        assert _parse_metric(lines, "stormguard_mode", 'strategy="system"') == 2.0

    def test_missing(self) -> None:
        assert _parse_metric(["other 42"], "pnl") is None

    def test_comments(self) -> None:
        assert _parse_metric(["# pnl 999", "pnl 123.0"], "pnl") == 123.0


class TestToggle:
    def test_toggle(self) -> None:
        v = is_health_visible()
        toggle_health_visible()
        assert is_health_visible() != v
        toggle_health_visible()


class TestPanel:
    def test_unreachable(self) -> None:
        assert "System Health" in str(build_health_panel(HealthState()).title)

    def test_normal(self) -> None:
        h = HealthState(engine_reachable=True, stormguard_state=0)
        assert "NORMAL" in str(build_health_panel(h).renderable)

    def test_halt(self) -> None:
        h = HealthState(engine_reachable=True, stormguard_state=3)
        assert "HALT" in str(build_health_panel(h).renderable)

    def test_drift(self) -> None:
        h = HealthState(engine_reachable=True, recon_discrepancy_count=2)
        assert "DRIFT" in str(build_health_panel(h).renderable)

    def test_blocked(self) -> None:
        h = HealthState(engine_reachable=True, circuit_breaker_state=1)
        assert "BLOCKED" in str(build_health_panel(h).renderable)
