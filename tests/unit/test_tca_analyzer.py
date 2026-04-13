"""Tests for TCAAnalyzer — daily fill cost reporting from ClickHouse."""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from hft_platform.tca.analyzer import TCAAnalyzer, _safe_float
from hft_platform.tca.types import TCADailyReport


def _row(
    strategy: str = "strat_a",
    symbol: str = "TXFD6",
    count: int = 1,
    qty: int = 1,
    notional: int = 500_0000_0000,
    fee: int = 0,
    tax: int = 0,
    delay_mean: float = 0.0,
    delay_p95: float = 0.0,
    exec_mean: float = 0.0,
    exec_p95: float = 0.0,
) -> tuple:
    """Build an 11-column row matching the _DAILY_QUERY result schema."""
    return (
        strategy,
        symbol,
        count,
        qty,
        notional,
        fee,
        tax,
        delay_mean,
        delay_p95,
        exec_mean,
        exec_p95,
    )


@dataclass
class FakeCHClient:
    """Minimal ClickHouse client stub returning pre-configured rows."""

    rows: list[tuple] = field(default_factory=list)

    def execute(self, query: str, params: dict | None = None) -> list[tuple]:  # noqa: ARG002
        return self.rows


class TestDailyReportProducesReports:
    """TCAAnalyzer.daily_report returns TCADailyReport for each group."""

    def test_daily_report_produces_reports(self) -> None:
        rows = [
            _row("strat_a", "TXFD6", 10, 50, 500_0000_0000, 1_000_0000, 200_0000),
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
            _row("strat_a", "TXFD6", 5, 20, 200_0000_0000, 500_0000, 100_0000),
            _row("strat_b", "2330", 3, 10, 100_0000_0000, 300_0000, 50_0000),
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
            _row("strat_a", "TXFD6", 10, 100, 1_000_000_0000, 2000_0000, 500_0000),
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
            _row("strat_a", "TXFD6", 1, 1, 200_0000_0000, 4000_0000, 0),
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
            _row("strat_b", "MXFD6", 1, 1, 100_0000_0000, 10_000_0000, 0),
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
            _row("strat_a", "TXFD6", 1, 1, 1_000_0000_0000, 1000_0000, 0),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-03-25")

        assert len(reports) == 1
        # notional stored is sum_notional_scaled * 1 (unchanged)
        assert reports[0].notional == 1_000_0000_0000


class TestLoadPointValueConfig:
    """load_point_value_config correctly parses fee YAML."""

    def test_load_point_value_config(self, tmp_path: pytest.TempPathFactory) -> None:
        from hft_platform.tca.analyzer import load_point_value_config

        yaml_content = """\
futures:
  TX:
    point_value: 200
  MTX:
    point_value: 50
  XMT:
    point_value: 10
  overrides:
    "2330F":
      commission_per_contract: 25
symbol_map:
  TXF: TX
  MXF: MTX
"""
        p = tmp_path / "futures.yaml"
        p.write_text(yaml_content)
        pv_map, sym_map = load_point_value_config(str(p))
        assert pv_map == {"TX": 200, "MTX": 50, "XMT": 10}
        assert sym_map == {"TXF": "TX", "MXF": "MTX"}

    def test_load_point_value_config_missing_file(self) -> None:
        from hft_platform.tca.analyzer import load_point_value_config

        pv_map, sym_map = load_point_value_config("/nonexistent/path.yaml")
        assert pv_map == {}
        assert sym_map == {}


class TestUnknownSymbolWarning:
    """Unknown symbol logs a warning and defaults point_value to 1."""

    def test_unknown_symbol_defaults_point_value_1(self, caplog: pytest.LogCaptureFixture) -> None:
        rows = [
            _row("strat_a", "UNKNOWN123", 1, 1, 500_0000_0000, 100_0000, 0),
        ]
        client = FakeCHClient(rows=rows)
        # Supply a map that does NOT contain "UNKNOWN123"
        analyzer = TCAAnalyzer(
            ch_client=client,
            point_value_map={"TX": 200},
        )

        reports = analyzer.daily_report("2026-03-25")

        assert len(reports) == 1
        # Notional should be unchanged (x1)
        assert reports[0].notional == 500_0000_0000


# ---------------------------------------------------------------------------
# NEW: Per-fill TCA aggregation tests
# ---------------------------------------------------------------------------


class TestDelayCostBpsFromQuery:
    """delay_cost_bps is populated from ClickHouse per-fill aggregation."""

    def test_delay_cost_positive_buy(self) -> None:
        """BUY fill: arrival drifted up from decision -> positive delay cost."""
        # Simulated CH output: avgIf delay = 5.0 bps, quantileIf delay P95 = 8.0 bps
        rows = [
            _row(delay_mean=5.0, delay_p95=8.0),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-04-13")

        assert len(reports) == 1
        r = reports[0]
        assert r.delay_cost_bps_mean == pytest.approx(5.0)
        assert r.delay_cost_bps_p95 == pytest.approx(8.0)

    def test_negative_delay_cost(self) -> None:
        """Negative delay cost means price moved favorably before submission."""
        rows = [
            _row(delay_mean=-2.5, delay_p95=-1.0),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-04-13")

        assert len(reports) == 1
        r = reports[0]
        assert r.delay_cost_bps_mean == pytest.approx(-2.5)
        assert r.delay_cost_bps_p95 == pytest.approx(-1.0)


class TestExecCostBpsFromQuery:
    """exec_cost_bps is populated from ClickHouse per-fill aggregation."""

    def test_exec_cost_positive(self) -> None:
        """Execution cost = slippage from arrival to fill."""
        rows = [
            _row(exec_mean=3.2, exec_p95=7.5),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-04-13")

        assert len(reports) == 1
        r = reports[0]
        assert r.exec_cost_bps_mean == pytest.approx(3.2)
        assert r.exec_cost_bps_p95 == pytest.approx(7.5)


class TestTotalCostIncludesTca:
    """total_cost_bps_mean includes commission + tax + delay + exec."""

    def test_total_cost_sums_all_components(self) -> None:
        # notional_real = 500_0000_0000 / 10000 = 5_000_000 NTD (point_value=1)
        # fee_scaled = 100_0000 = 1_000_000 → real fee = 100 NTD, tax=0
        # commission_bps = (100 / 5_000_000) * 10000 = 0.2 bps
        # delay_mean=1.0, exec_mean=2.0
        # total = 0.2 + 0.0 + 1.0 + 2.0 = 3.2
        rows = [
            _row(
                notional=500_0000_0000,
                fee=100_0000,
                tax=0,
                delay_mean=1.0,
                delay_p95=1.5,
                exec_mean=2.0,
                exec_p95=4.0,
            ),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-04-13")

        assert len(reports) == 1
        r = reports[0]
        assert r.total_cost_bps_mean == pytest.approx(0.2 + 1.0 + 2.0)
        assert r.total_cost_bps_p95 == pytest.approx(1.5 + 4.0)


class TestNoTcaDataFallsBackToZero:
    """Fills without decision_price/arrival_price produce NaN from CH; coerced to 0.0."""

    def test_nan_delay_exec_coerced_to_zero(self) -> None:
        """When all fills have decision_price=0, avgIf returns NaN."""
        rows = [
            _row(
                notional=500_0000_0000,
                fee=100_0000,
                tax=0,
                delay_mean=float("nan"),
                delay_p95=float("nan"),
                exec_mean=float("nan"),
                exec_p95=float("nan"),
            ),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-04-13")

        assert len(reports) == 1
        r = reports[0]
        assert r.delay_cost_bps_mean == 0.0
        assert r.delay_cost_bps_p95 == 0.0
        assert r.exec_cost_bps_mean == 0.0
        assert r.exec_cost_bps_p95 == 0.0

    def test_none_delay_exec_coerced_to_zero(self) -> None:
        """Some CH drivers return None instead of NaN for empty aggregates."""
        rows = [
            _row(
                notional=500_0000_0000,
                fee=0,
                tax=0,
                delay_mean=None,  # type: ignore[arg-type]
                delay_p95=None,  # type: ignore[arg-type]
                exec_mean=None,  # type: ignore[arg-type]
                exec_p95=None,  # type: ignore[arg-type]
            ),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-04-13")

        assert len(reports) == 1
        r = reports[0]
        assert r.delay_cost_bps_mean == 0.0
        assert r.exec_cost_bps_mean == 0.0


class TestImpactBpsAlwaysZero:
    """market_impact_bps remains 0.0 for single-lot strategies."""

    def test_impact_bps_zero(self) -> None:
        rows = [
            _row(delay_mean=5.0, exec_mean=3.0),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-04-13")

        assert len(reports) == 1
        assert reports[0].impact_bps_mean == 0.0


class TestSafeFloat:
    """_safe_float handles None, NaN, and normal values."""

    def test_none_returns_zero(self) -> None:
        assert _safe_float(None) == 0.0

    def test_nan_returns_zero(self) -> None:
        assert _safe_float(float("nan")) == 0.0

    def test_normal_float_passthrough(self) -> None:
        assert _safe_float(3.14) == pytest.approx(3.14)

    def test_integer_coerced(self) -> None:
        assert _safe_float(42) == 42.0

    def test_negative_float_passthrough(self) -> None:
        assert _safe_float(-1.5) == pytest.approx(-1.5)

    def test_inf_returns_zero(self) -> None:
        assert _safe_float(float("inf")) == 0.0

    def test_neg_inf_returns_zero(self) -> None:
        assert _safe_float(float("-inf")) == 0.0


class TestMultipleStrategiesWithTca:
    """Multiple groups each get independent TCA values."""

    def test_independent_tca_per_group(self) -> None:
        rows = [
            _row("strat_a", "TXFD6", delay_mean=2.0, exec_mean=1.0),
            _row("strat_b", "TMFD6", delay_mean=5.0, exec_mean=3.0),
        ]
        client = FakeCHClient(rows=rows)
        analyzer = TCAAnalyzer(ch_client=client)

        reports = analyzer.daily_report("2026-04-13")

        assert len(reports) == 2
        by_strat = {r.strategy: r for r in reports}
        assert by_strat["strat_a"].delay_cost_bps_mean == pytest.approx(2.0)
        assert by_strat["strat_a"].exec_cost_bps_mean == pytest.approx(1.0)
        assert by_strat["strat_b"].delay_cost_bps_mean == pytest.approx(5.0)
        assert by_strat["strat_b"].exec_cost_bps_mean == pytest.approx(3.0)
