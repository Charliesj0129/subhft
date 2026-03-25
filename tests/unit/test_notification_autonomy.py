"""Tests for autonomy-related notification render functions."""

from __future__ import annotations

import pytest

from hft_platform.notifications import templates


class TestRenderAutonomyTransition:
    def test_contains_scope_and_modes(self) -> None:
        result = templates.render_autonomy_transition(
            scope="platform",
            from_mode="NORMAL",
            to_mode="PLATFORM_REDUCE_ONLY",
            reason="broker_unavailable",
        )
        assert "platform" in result
        assert "NORMAL" in result
        assert "PLATFORM_REDUCE_ONLY" in result
        assert "broker_unavailable" in result

    def test_returns_string(self) -> None:
        result = templates.render_autonomy_transition(
            scope="strategy", from_mode="NORMAL", to_mode="HALT", reason="test"
        )
        assert isinstance(result, str)


class TestRenderFlattenResult:
    def test_no_failures_shows_success_icon(self) -> None:
        result = templates.render_flatten_result(
            scope="all", fully_closed=5, partially_closed=0, failed=0, failed_symbols=[]
        )
        assert "closed=5" in result
        assert "failed=0" in result

    def test_failures_include_symbols(self) -> None:
        result = templates.render_flatten_result(
            scope="all", fully_closed=2, partially_closed=1, failed=1, failed_symbols=["2330"]
        )
        assert "2330" in result
        assert "failed=1" in result


class TestRenderHeartbeat:
    def test_contains_state_and_feed(self) -> None:
        result = templates.render_heartbeat(
            autonomy_state="NORMAL", pnl_scaled=10000, strategies_active=3, feed_status="ok"
        )
        assert "NORMAL" in result
        assert "ok" in result
        assert "10000" in result


class TestRenderSessionPhase:
    def test_contains_track_and_phases(self) -> None:
        result = templates.render_session_phase(
            track="stock", old_phase="OPEN", new_phase="CLOSE_ONLY"
        )
        assert "stock" in result
        assert "OPEN" in result
        assert "CLOSE_ONLY" in result


class TestRenderAutonomyDailySummary:
    def test_contains_date_and_counts(self) -> None:
        result = templates.render_autonomy_daily_summary(
            date_str="2026-03-25", transitions=12, halts=1, flatten_count=2, manual_rearms=1
        )
        assert "2026-03-25" in result
        assert "12" in result
        assert "1" in result
