"""Round 55: median_monthly_net_pnl_pts + worst_monthly_pnl_pts (驗證標準 §6
"需檢查 median_monthly_net_pnl、worst_month") lifted to top-level row fields
and carried in the flat `audit export` schema, so a cross-candidate sweep can
sort/filter on monthly stability. Audit-layer only, no relaxed thresholds."""

from __future__ import annotations

import csv
import io

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    median_month: float | None = 30.0,
    worst_month: float | None = -45.0,
    edge: float | None = 12.0,
) -> None:
    advisory: list[dict] = []
    if edge is not None:
        advisory.append(
            {
                "name": "edge_per_round_trip",
                "passed": edge > 10.0,
                "metrics": {"mean_net_edge_pts_per_trade": edge},
                "details": "",
                "error": False,
            }
        )
    metrics: dict[str, float] = {"n_months": 4.0, "top_month_contribution_pct": 30.0}
    if median_month is not None:
        metrics["median_monthly_net_pnl_pts"] = median_month
    if worst_month is not None:
        metrics["worst_monthly_pnl_pts"] = worst_month
    if median_month is not None or worst_month is not None:
        advisory.append(
            {
                "name": "monthly_distribution",
                "passed": True,
                "metrics": metrics,
                "details": "",
                "error": False,
            }
        )
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument="TXFD6",
        strategy_type="taker",
        profile_name="vm_ul6_strict",
        advisory=advisory,
        blocking={"passed": True, "failing": [], "triage_status": "passed"},
        recorded_at_ns=1,
    )


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestBuildRecordLiftsMonthlyStability:
    def test_median_and_worst_lifted(self, _isolated) -> None:
        _record(run_id="a", median_month=30.0, worst_month=-45.0)
        rows = sub_gate_audit.read_runs()
        assert rows[0]["median_monthly_net_pnl_pts"] == 30.0
        assert rows[0]["worst_monthly_pnl_pts"] == -45.0

    def test_absent_when_gate_didnt_run(self, _isolated) -> None:
        _record(run_id="b", median_month=None, worst_month=None)
        rows = sub_gate_audit.read_runs()
        assert "median_monthly_net_pnl_pts" not in rows[0]
        assert "worst_monthly_pnl_pts" not in rows[0]


class TestExportCarriesMonthlyStability:
    def test_csv_columns_present(self, _isolated) -> None:
        _record(run_id="a", median_month=30.0, worst_month=-45.0)
        out = audit_cli.export(fmt="csv")
        reader = csv.DictReader(io.StringIO(out))
        rows = list(reader)
        assert "median_monthly_net_pnl_pts" in reader.fieldnames
        assert "worst_monthly_pnl_pts" in reader.fieldnames
        assert rows[0]["median_monthly_net_pnl_pts"] == "30.0000"
        assert rows[0]["worst_monthly_pnl_pts"] == "-45.0000"

    def test_csv_empty_cells_when_missing(self, _isolated) -> None:
        _record(run_id="b", median_month=None, worst_month=None)
        out = audit_cli.export(fmt="csv")
        rows = list(csv.DictReader(io.StringIO(out)))
        assert rows[0]["median_monthly_net_pnl_pts"] == ""
        assert rows[0]["worst_monthly_pnl_pts"] == ""
