"""Unit tests for notification message templates.

Verifies that each render function produces output containing the
expected content, correct number formatting, and correct sign handling.
"""

from __future__ import annotations

from hft_platform.notifications.templates import (
    render_daily_loss,
    render_daily_report,
    render_halt,
    render_pre_market_fail,
    render_pre_market_pass,
    render_process_restart,
    render_reconciliation_mismatch,
    render_reconnect_alert,
    render_stormguard_change,
    render_weekly_summary,
)

# ---------------------------------------------------------------------------
# render_halt
# ---------------------------------------------------------------------------


class TestRenderHalt:
    def test_reason_present(self) -> None:
        result = render_halt("circuit breaker triggered")
        assert "circuit breaker triggered" in result

    def test_halt_keyword(self) -> None:
        result = render_halt("feed gap")
        assert "HALT" in result

    def test_manual_recovery_instruction(self) -> None:
        result = render_halt("feed gap")
        assert "Manual recovery required" in result

    def test_red_icon(self) -> None:
        result = render_halt("test")
        assert "🔴" in result


# ---------------------------------------------------------------------------
# render_daily_loss
# ---------------------------------------------------------------------------


class TestRenderDailyLoss:
    def test_pnl_present_with_comma_formatting(self) -> None:
        result = render_daily_loss(-50000, -100000)
        assert "-50,000" in result

    def test_limit_present_with_comma_formatting(self) -> None:
        result = render_daily_loss(-50000, -100000)
        assert "-100,000" in result

    def test_halt_activated_in_message(self) -> None:
        result = render_daily_loss(-50000, -100000)
        assert "HALT activated" in result

    def test_red_icon(self) -> None:
        result = render_daily_loss(-1000, -5000)
        assert "🔴" in result

    def test_zero_pnl(self) -> None:
        result = render_daily_loss(0, -100000)
        assert "0" in result


# ---------------------------------------------------------------------------
# render_daily_report
# ---------------------------------------------------------------------------


class TestRenderDailyReport:
    def _make_report(self, **overrides: object) -> str:
        defaults: dict[str, object] = dict(
            date_str="2026-03-23",
            pnl_ntd=12345,
            buys=10,
            sells=8,
            fills=18,
            position_status="FLAT",
            reconciliation_status="OK",
            latency_p95_ms=2.75,
            reconnect_count=0,
            storm_guard_state="NORMAL",
            memory_gb=1.23,
            memory_max_gb=1.45,
        )
        defaults.update(overrides)
        return render_daily_report(**defaults)  # type: ignore[arg-type]

    def test_date_present(self) -> None:
        assert "2026-03-23" in self._make_report()

    def test_pnl_with_plus_sign_when_positive(self) -> None:
        result = self._make_report(pnl_ntd=12345)
        assert "+12,345" in result

    def test_pnl_with_minus_sign_when_negative(self) -> None:
        result = self._make_report(pnl_ntd=-5000)
        assert "-5,000" in result

    def test_fills_count_present(self) -> None:
        result = self._make_report(fills=18, buys=10, sells=8)
        assert "18" in result

    def test_position_status_present(self) -> None:
        result = self._make_report(position_status="LONG 100")
        assert "LONG 100" in result

    def test_reconciliation_status_present(self) -> None:
        result = self._make_report(reconciliation_status="MISMATCH")
        assert "MISMATCH" in result

    def test_latency_formatted(self) -> None:
        result = self._make_report(latency_p95_ms=2.75)
        assert "2.75" in result

    def test_storm_guard_state_present(self) -> None:
        result = self._make_report(storm_guard_state="HALT")
        assert "HALT" in result

    def test_memory_formatted(self) -> None:
        result = self._make_report(memory_gb=1.23, memory_max_gb=1.45)
        assert "1.23" in result
        assert "1.45" in result

    def test_reconnect_count_present(self) -> None:
        result = self._make_report(reconnect_count=3)
        assert "3" in result


# ---------------------------------------------------------------------------
# render_stormguard_change
# ---------------------------------------------------------------------------


class TestRenderStormguardChange:
    def test_old_state_present(self) -> None:
        result = render_stormguard_change("NORMAL", "HALT", "feed gap 35s")
        assert "NORMAL" in result

    def test_new_state_present(self) -> None:
        result = render_stormguard_change("NORMAL", "HALT", "feed gap 35s")
        assert "HALT" in result

    def test_reason_present(self) -> None:
        result = render_stormguard_change("NORMAL", "HALT", "feed gap 35s")
        assert "feed gap 35s" in result

    def test_arrow_separator(self) -> None:
        result = render_stormguard_change("NORMAL", "DEGRADED", "high loss rate")
        assert "→" in result

    def test_yellow_icon(self) -> None:
        result = render_stormguard_change("NORMAL", "HALT", "test")
        assert "🟡" in result


# ---------------------------------------------------------------------------
# render_pre_market_pass
# ---------------------------------------------------------------------------


class TestRenderPreMarketPass:
    def test_pass_keyword(self) -> None:
        assert "PASS" in render_pre_market_pass()

    def test_green_icon(self) -> None:
        assert "🟢" in render_pre_market_pass()

    def test_start_time_mentioned(self) -> None:
        result = render_pre_market_pass()
        assert "08:45" in result


# ---------------------------------------------------------------------------
# render_pre_market_fail
# ---------------------------------------------------------------------------


class TestRenderPreMarketFail:
    def test_fail_keyword(self) -> None:
        result = render_pre_market_fail(["ClickHouse unreachable"])
        assert "FAIL" in result

    def test_red_icon(self) -> None:
        result = render_pre_market_fail(["Redis timeout"])
        assert "🔴" in result

    def test_single_failed_check_present(self) -> None:
        result = render_pre_market_fail(["ClickHouse unreachable"])
        assert "ClickHouse unreachable" in result

    def test_multiple_failed_checks_all_present(self) -> None:
        checks = ["ClickHouse unreachable", "Redis timeout", "Broker login failed"]
        result = render_pre_market_fail(checks)
        for check in checks:
            assert check in result


# ---------------------------------------------------------------------------
# render_reconciliation_mismatch
# ---------------------------------------------------------------------------


class TestRenderReconciliationMismatch:
    def test_platform_pnl_formatted(self) -> None:
        result = render_reconciliation_mismatch(100000, 99000, 100500)
        assert "100,000" in result

    def test_broker_pnl_formatted(self) -> None:
        result = render_reconciliation_mismatch(100000, 99000, 100500)
        assert "99,000" in result

    def test_ch_pnl_formatted(self) -> None:
        result = render_reconciliation_mismatch(100000, 99000, 100500)
        assert "100,500" in result

    def test_mismatch_keyword(self) -> None:
        result = render_reconciliation_mismatch(1, 2, 3)
        assert "不符" in result or "mismatch" in result.lower()

    def test_warning_icon(self) -> None:
        result = render_reconciliation_mismatch(1, 2, 3)
        assert "⚠️" in result


# ---------------------------------------------------------------------------
# render_reconnect_alert
# ---------------------------------------------------------------------------


class TestRenderReconnectAlert:
    def test_count_present(self) -> None:
        result = render_reconnect_alert(3, "OK")
        assert "3" in result

    def test_flap_status_present(self) -> None:
        result = render_reconnect_alert(5, "FLAPPING")
        assert "FLAPPING" in result

    def test_yellow_icon(self) -> None:
        result = render_reconnect_alert(1, "OK")
        assert "🟡" in result


# ---------------------------------------------------------------------------
# render_process_restart
# ---------------------------------------------------------------------------


class TestRenderProcessRestart:
    def test_attempt_present(self) -> None:
        result = render_process_restart(2, 5)
        assert "2" in result

    def test_max_attempts_present(self) -> None:
        result = render_process_restart(2, 5)
        assert "5" in result

    def test_restart_keyword(self) -> None:
        result = render_process_restart(1, 3)
        assert "restart" in result.lower()

    def test_yellow_icon(self) -> None:
        result = render_process_restart(1, 3)
        assert "🟡" in result


# ---------------------------------------------------------------------------
# render_weekly_summary
# ---------------------------------------------------------------------------


class TestRenderWeeklySummary:
    def _make_summary(self, **overrides: object) -> str:
        defaults: dict[str, object] = dict(
            week_label="2026-W12",
            date_range="2026-03-16 ~ 2026-03-20",
            total_pnl_ntd=75000,
            trading_days=5,
            avg_trades=120.5,
            best_day_ntd=30000,
            worst_day_ntd=-5000,
            reconciliation_match=True,
            halt_count=0,
            reconnect_count=2,
            latency_p95_avg_ms=3.14,
            rss_peak_gb=1.80,
            uptime_pct=99.8,
        )
        defaults.update(overrides)
        return render_weekly_summary(**defaults)  # type: ignore[arg-type]

    def test_week_label_present(self) -> None:
        assert "2026-W12" in self._make_summary()

    def test_date_range_present(self) -> None:
        assert "2026-03-16" in self._make_summary()

    def test_total_pnl_with_plus_sign(self) -> None:
        result = self._make_summary(total_pnl_ntd=75000)
        assert "+75,000" in result

    def test_total_pnl_with_minus_sign(self) -> None:
        result = self._make_summary(total_pnl_ntd=-10000)
        assert "-10,000" in result

    def test_trading_days_present(self) -> None:
        assert "5" in self._make_summary()

    def test_best_day_formatted(self) -> None:
        result = self._make_summary(best_day_ntd=30000)
        assert "30,000" in result

    def test_worst_day_formatted(self) -> None:
        result = self._make_summary(worst_day_ntd=-5000)
        assert "-5,000" in result

    def test_reconciliation_match_shows_ok_icon(self) -> None:
        result = self._make_summary(reconciliation_match=True)
        assert "✅" in result

    def test_reconciliation_mismatch_shows_fail_icon(self) -> None:
        result = self._make_summary(reconciliation_match=False)
        assert "❌" in result

    def test_halt_count_present(self) -> None:
        result = self._make_summary(halt_count=2)
        assert "2" in result

    def test_reconnect_count_present(self) -> None:
        result = self._make_summary(reconnect_count=7)
        assert "7" in result

    def test_latency_formatted(self) -> None:
        result = self._make_summary(latency_p95_avg_ms=3.14)
        assert "3.14" in result

    def test_uptime_pct_formatted(self) -> None:
        result = self._make_summary(uptime_pct=99.8)
        assert "99.8" in result

    def test_rss_peak_formatted(self) -> None:
        result = self._make_summary(rss_peak_gb=1.80)
        assert "1.80" in result
