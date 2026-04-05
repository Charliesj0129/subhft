"""Tests for TCAAnalyzer — daily fill cost reporting from ClickHouse."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from hft_platform.tca.analyzer import TCAAnalyzer
from hft_platform.tca.types import TCADailyReport


@dataclass
class FakeCHClient:
    """Minimal ClickHouse client stub returning pre-configured rows."""

    rows: list[tuple] = field(default_factory=list)

    def execute(self, query: str, params: dict | None = None) -> list[tuple]:  # noqa: ARG002
        return self.rows


class TestDailyReportProducesReports:
    """TCAAnalyzer.daily_report returns TCADailyReport for each group."""

    def test_daily_report_produces_reports(self) -> None:
        # Row: strategy_id, symbol, count, sum_qty, sum_notional, sum_fee, sum_tax
        rows = [
            ("strat_a", "TXFD6", 10, 50, 500_0000_0000, 1_000_0000, 200_0000),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-03-25")

        assert len(reports) == 1
        r = reports[0]
        assert isinstance(r, TCADailyReport)
        assert r.date == "2026-03-25"
        assert r.strategy == "strat_a"
        assert r.symbol == "TXFD6"
        assert r.trade_count == 10
        assert r.volume == 50


class TestEmptyDay:
    """No fills returns empty list."""

    def test_empty_day(self) -> None:
        client = FakeCHClient(rows=[])
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-03-25")

        assert reports == []


class TestMultipleStrategies:
    """Multiple (strategy, symbol) groups produce separate reports."""

    def test_multiple_strategies(self) -> None:
        rows = [
            ("strat_a", "TXFD6", 5, 20, 200_0000_0000, 500_0000, 100_0000),
            ("strat_b", "2330", 3, 10, 100_0000_0000, 300_0000, 50_0000),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-03-25")

        assert len(reports) == 2
        strategies = {r.strategy for r in reports}
        assert strategies == {"strat_a", "strat_b"}


class TestChFailureReturnsEmpty:
    """ClickHouse failure returns empty list and logs warning."""

    def test_ch_failure_returns_empty(self) -> None:
        client = MagicMock()
        client.execute.side_effect = Exception("connection refused")
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-03-25")

        assert reports == []


class TestCostBpsComputed:
    """Commission bps is computed correctly from fee/tax/notional."""

    def test_cost_bps_computed(self) -> None:
        # notional = price_scaled * qty summed = 1_000_000_0000 (scaled x10000)
        # real notional = 1_000_000 NTD (point_value defaults to 1 when no map given)
        # fee_scaled = 2000_0000 (scaled x10000) => real fee = 2000 NTD
        # tax_scaled = 500_0000 (scaled x10000) => real tax = 500 NTD
        # commission = fee - tax = 1500 NTD
        # commission_bps = (1500 / 1_000_000) * 10000 = 15.0 bps
        # tax_bps = (500 / 1_000_000) * 10000 = 5.0 bps
        rows = [
            ("strat_a", "TXFD6", 10, 100, 1_000_000_0000, 2000_0000, 500_0000),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-03-25")

        assert len(reports) == 1
        r = reports[0]
        assert r.commission_bps_mean == pytest.approx(15.0)
        assert r.tax_bps_mean == pytest.approx(5.0)
        assert r.total_cost_bps_mean == pytest.approx(20.0)


class TestPointValueMultiplier:
    """Notional is multiplied by point_value before bps calculation."""

    def test_tx_point_value_200_scales_notional(self) -> None:
        # sum_notional_scaled = price_scaled * qty (no point_value in SQL).
        # With point_value=200 for TX, corrected_notional = sum_notional_scaled * 200.
        # fee_scaled / corrected_notional should give much smaller bps than without scaling.
        #
        # sum_notional_scaled = 200_0000_0000 = 20_000_000_000
        # corrected_notional_scaled = 20_000_000_000 * 200 = 4_000_000_000_000
        # notional_real = 4_000_000_000_000 / 10000 = 400_000_000 NTD
        # fee_scaled = 4000_0000 = 40_000_000 → real fee = 4000 NTD; tax_scaled = 0
        # commission_bps = (4000 / 400_000_000) * 10000 = 0.1 bps
        rows = [
            ("strat_a", "TXFD6", 1, 1, 200_0000_0000, 4000_0000, 0),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(
            ch_client=client,
            point_value_map={"TX": 200},
            symbol_to_product={"TXFD6": "TX"},
        )

        reports = analyzer.daily_report("2026-03-25")

        assert len(reports) == 1
        r = reports[0]
        assert r.commission_bps_mean == pytest.approx(0.1, abs=0.001)
        assert r.notional == 200_0000_0000 * 200

    def test_mtx_point_value_50(self) -> None:
        # sum_notional_scaled = 100_0000_0000, point_value=50
        # corrected = 5_000_000_0000 → notional_real = 500_000_000 NTD
        # fee_scaled = 10_000_0000 → fee_real = 100_000 NTD, tax=0
        # commission_bps = (100_000 / 500_000_000) * 10_000 = 2.0 bps
        rows = [
            ("strat_b", "MXFD6", 1, 1, 100_0000_0000, 10_000_0000, 0),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(
            ch_client=client,
            point_value_map={"MTX": 50},
            symbol_to_product={"MXFD6": "MTX"},
        )

        reports = analyzer.daily_report("2026-03-25")

        assert len(reports) == 1
        r = reports[0]
        assert r.commission_bps_mean == pytest.approx(2.0, abs=0.01)

    def test_no_point_value_map_defaults_to_1(self) -> None:
        # Without point_value_map, point_value=1 → notional unchanged.
        rows = [
            ("strat_a", "TXFD6", 1, 1, 1_000_0000_0000, 1000_0000, 0),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-03-25")

        assert len(reports) == 1
        # notional stored is sum_notional_scaled * 1 (unchanged)
        assert reports[0].notional == 1_000_0000_0000


class TestUnknownSymbolWarning:
    """Unknown symbol logs a warning and defaults point_value to 1."""

    def test_unknown_symbol_defaults_point_value_1(self, caplog: pytest.LogCaptureFixture) -> None:
        rows = [
            ("strat_a", "UNKNOWN123", 1, 1, 500_0000_0000, 100_0000, 0),
        ]
        client = FakeCHClient(rows=rows)
        # Supply a map that does NOT contain "UNKNOWN123"
        analyzer = TCAAnalyzer(
            ch_client=client,
            point_value_map={"TX": 200},
        )

        reports = analyzer.daily_report("2026-03-25")

        assert len(reports) == 1
        # Notional should be unchanged (×1)
        assert reports[0].notional == 500_0000_0000
