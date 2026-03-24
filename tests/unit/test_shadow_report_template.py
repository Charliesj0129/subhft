"""Tests for shadow daily report template."""

from __future__ import annotations


class TestShadowDailyReport:
    def test_shadow_daily_report_renders_all_fields(self) -> None:
        from hft_platform.notifications.templates import render_shadow_daily_report

        msg = render_shadow_daily_report(
            date_str="2026-04-22 (二)",
            intent_count=18,
            buys=10,
            sells=8,
            simulated_pnl_ntd=850,
            latency_p50_ms=1.1,
            latency_p95_ms=3.2,
            latency_p99_ms=8.7,
            reconnect_count=0,
            queue_peak_pct=12,
            rss_gb=1.7,
            storm_guard_state="NORMAL",
        )
        assert "Shadow" in msg or "shadow" in msg
        assert "18" in msg
        assert "850" in msg
        assert "NORMAL" in msg

    def test_shadow_daily_report_zero_signals(self) -> None:
        from hft_platform.notifications.templates import render_shadow_daily_report

        msg = render_shadow_daily_report(
            date_str="2026-04-23",
            intent_count=0,
            buys=0,
            sells=0,
            simulated_pnl_ntd=0,
            latency_p50_ms=0,
            latency_p95_ms=0,
            latency_p99_ms=0,
            reconnect_count=0,
            queue_peak_pct=0,
            rss_gb=0,
            storm_guard_state="NORMAL",
        )
        assert "0" in msg

    def test_shadow_daily_report_date_present(self) -> None:
        from hft_platform.notifications.templates import render_shadow_daily_report

        msg = render_shadow_daily_report(
            date_str="2026-04-22",
            intent_count=5,
            buys=3,
            sells=2,
            simulated_pnl_ntd=500,
            latency_p50_ms=0.8,
            latency_p95_ms=2.1,
            latency_p99_ms=5.3,
            reconnect_count=0,
            queue_peak_pct=8,
            rss_gb=1.5,
            storm_guard_state="NORMAL",
        )
        assert "2026-04-22" in msg

    def test_shadow_daily_report_buy_sell_split(self) -> None:
        from hft_platform.notifications.templates import render_shadow_daily_report

        msg = render_shadow_daily_report(
            date_str="2026-04-22",
            intent_count=15,
            buys=9,
            sells=6,
            simulated_pnl_ntd=1200,
            latency_p50_ms=1.0,
            latency_p95_ms=2.5,
            latency_p99_ms=7.0,
            reconnect_count=0,
            queue_peak_pct=10,
            rss_gb=1.6,
            storm_guard_state="NORMAL",
        )
        assert "9" in msg
        assert "6" in msg

    def test_shadow_daily_report_latency_percentiles(self) -> None:
        from hft_platform.notifications.templates import render_shadow_daily_report

        msg = render_shadow_daily_report(
            date_str="2026-04-22",
            intent_count=10,
            buys=5,
            sells=5,
            simulated_pnl_ntd=600,
            latency_p50_ms=1.5,
            latency_p95_ms=4.2,
            latency_p99_ms=9.8,
            reconnect_count=0,
            queue_peak_pct=15,
            rss_gb=1.8,
            storm_guard_state="NORMAL",
        )
        assert "1.5" in msg
        assert "4.2" in msg
        assert "9.8" in msg

    def test_shadow_daily_report_reconnect_count(self) -> None:
        from hft_platform.notifications.templates import render_shadow_daily_report

        msg = render_shadow_daily_report(
            date_str="2026-04-22",
            intent_count=8,
            buys=4,
            sells=4,
            simulated_pnl_ntd=400,
            latency_p50_ms=0.9,
            latency_p95_ms=2.0,
            latency_p99_ms=6.5,
            reconnect_count=2,
            queue_peak_pct=5,
            rss_gb=1.4,
            storm_guard_state="NORMAL",
        )
        assert "2" in msg

    def test_shadow_daily_report_negative_pnl(self) -> None:
        from hft_platform.notifications.templates import render_shadow_daily_report

        msg = render_shadow_daily_report(
            date_str="2026-04-22",
            intent_count=12,
            buys=6,
            sells=6,
            simulated_pnl_ntd=-300,
            latency_p50_ms=1.2,
            latency_p95_ms=3.0,
            latency_p99_ms=7.5,
            reconnect_count=1,
            queue_peak_pct=20,
            rss_gb=1.9,
            storm_guard_state="CAUTION",
        )
        assert "-300" in msg

    def test_shadow_daily_report_memory_usage(self) -> None:
        from hft_platform.notifications.templates import render_shadow_daily_report

        msg = render_shadow_daily_report(
            date_str="2026-04-22",
            intent_count=7,
            buys=4,
            sells=3,
            simulated_pnl_ntd=350,
            latency_p50_ms=0.7,
            latency_p95_ms=1.8,
            latency_p99_ms=5.0,
            reconnect_count=0,
            queue_peak_pct=6,
            rss_gb=2.3,
            storm_guard_state="NORMAL",
        )
        assert "2.3" in msg

    def test_shadow_daily_report_queue_peak(self) -> None:
        from hft_platform.notifications.templates import render_shadow_daily_report

        msg = render_shadow_daily_report(
            date_str="2026-04-22",
            intent_count=20,
            buys=12,
            sells=8,
            simulated_pnl_ntd=1500,
            latency_p50_ms=1.3,
            latency_p95_ms=3.5,
            latency_p99_ms=8.2,
            reconnect_count=0,
            queue_peak_pct=45,
            rss_gb=1.7,
            storm_guard_state="NORMAL",
        )
        assert "45" in msg

    def test_shadow_daily_report_storm_guard_halt(self) -> None:
        from hft_platform.notifications.templates import render_shadow_daily_report

        msg = render_shadow_daily_report(
            date_str="2026-04-22",
            intent_count=5,
            buys=2,
            sells=3,
            simulated_pnl_ntd=250,
            latency_p50_ms=0.8,
            latency_p95_ms=2.0,
            latency_p99_ms=6.0,
            reconnect_count=0,
            queue_peak_pct=10,
            rss_gb=1.5,
            storm_guard_state="HALT",
        )
        assert "HALT" in msg
