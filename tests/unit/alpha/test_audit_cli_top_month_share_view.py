"""Round 54: top_month_contribution_pct (驗證標準 §6 "單月收益支配性") lifted
to a top-level row field and surfaced in `audit show` + `summary`. The
monthly analogue of single-day dominance: a candidate whose net PnL leans on
one calendar month is not a durable monthly-income stream. Audit-layer only,
no relaxed thresholds."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    top_month: float | None,
    edge: float | None = 12.0,
    strategy_type: str = "taker",
    profile: str = "vm_ul6_strict",
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
    if top_month is not None:
        advisory.append(
            {
                "name": "monthly_distribution",
                "passed": top_month <= 50.0,
                "metrics": {
                    "n_months": 4.0,
                    "top_month_contribution_pct": top_month,
                    "top_month_contribution_max_pct": 50.0,
                    "drawdown_to_avg_monthly_ratio": 1.0,
                },
                "details": "",
                "error": False,
            }
        )
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument="TXFD6",
        strategy_type=strategy_type,
        profile_name=profile,
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


class TestBuildRecordLiftsTopMonthShare:
    def test_share_lifted_to_top_level(self, _isolated) -> None:
        _record(run_id="a", top_month=40.0)
        rows = sub_gate_audit.read_runs()
        assert rows[0]["top_month_contribution_pct"] == 40.0

    def test_absent_when_gate_didnt_run(self, _isolated) -> None:
        _record(run_id="b", top_month=None)
        rows = sub_gate_audit.read_runs()
        assert "top_month_contribution_pct" not in rows[0]


class TestShowSurfacesTopMonthShare:
    def test_within_cap_reads_pass(self, _isolated) -> None:
        _record(run_id="s_ok", top_month=40.0)
        out = audit_cli.show("s_ok")
        line = out.split("top_month_share")[1].split("\n")[0]
        assert "40.0%" in line
        assert "PASS" in line

    def test_over_cap_reads_fail(self, _isolated) -> None:
        _record(run_id="s_bad", top_month=75.0)
        out = audit_cli.show("s_bad")
        line = out.split("top_month_share")[1].split("\n")[0]
        assert "75.0%" in line
        assert "FAIL" in line

    def test_exactly_cap_reads_pass(self, _isolated) -> None:
        _record(run_id="s_edge", top_month=50.0)
        out = audit_cli.show("s_edge")
        line = out.split("top_month_share")[1].split("\n")[0]
        assert "PASS" in line

    def test_na_when_missing(self, _isolated) -> None:
        _record(run_id="s_na", top_month=None)
        out = audit_cli.show("s_na")
        assert "top_month_share: (n/a" in out


class TestSummaryAggregatesTopMonth:
    def test_summary_counts_over_cap(self, _isolated) -> None:
        _record(run_id="m1", top_month=20.0)
        _record(run_id="m2", top_month=60.0)
        _record(run_id="m3", top_month=90.0)
        out = audit_cli.summary()
        assert "top_month_dominance (strict cap 50.0% of net):" in out
        section = out.split("top_month_dominance")[1]
        assert "rows with metric: 3 / 3" in section
        assert "rows over cap   : 2" in section

    def test_summary_header_renders_without_metric(self, _isolated) -> None:
        _record(run_id="m_none", top_month=None)
        out = audit_cli.summary()
        section = out.split("top_month_dominance")[1]
        assert "rows with metric: 0 / 1" in section
