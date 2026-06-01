"""Round 53: drawdown_to_avg_monthly_ratio (驗證標準 §6) lifted to a
top-level row field and surfaced in `audit show`. max_drawdown must stay
within 2× average monthly net PnL; inf (avg_monthly <= 0) always reads FAIL.
Pure audit-layer surfacing — no production logic, no relaxed thresholds."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    dd_ratio: float | None,
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
    if dd_ratio is not None:
        advisory.append(
            {
                "name": "monthly_distribution",
                "passed": dd_ratio <= 2.0,
                "metrics": {
                    "n_months": 4.0,
                    "avg_monthly_net_pnl_pts": 100.0,
                    "max_drawdown_pts": dd_ratio * 100.0 if dd_ratio != float("inf") else 50.0,
                    "drawdown_to_avg_monthly_ratio": dd_ratio,
                    "drawdown_to_avg_monthly_max_ratio": 2.0,
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


class TestBuildRecordLiftsDrawdownRatio:
    def test_ratio_lifted_to_top_level(self, _isolated) -> None:
        _record(run_id="a", dd_ratio=1.5)
        rows = sub_gate_audit.read_runs()
        assert rows[0]["drawdown_to_avg_monthly_ratio"] == 1.5

    def test_inf_ratio_lifted_and_roundtrips(self, _isolated) -> None:
        _record(run_id="inf", dd_ratio=float("inf"))
        rows = sub_gate_audit.read_runs()
        assert rows[0]["drawdown_to_avg_monthly_ratio"] == float("inf")

    def test_absent_when_gate_didnt_run(self, _isolated) -> None:
        _record(run_id="b", dd_ratio=None)
        rows = sub_gate_audit.read_runs()
        assert "drawdown_to_avg_monthly_ratio" not in rows[0]


class TestShowSurfacesDrawdownRatio:
    def test_within_cap_reads_pass(self, _isolated) -> None:
        _record(run_id="s_ok", dd_ratio=1.5)
        out = audit_cli.show("s_ok")
        line = out.split("drawdown_ratio")[1].split("\n")[0]
        assert "1.50×" in line
        assert "PASS" in line

    def test_over_cap_reads_fail(self, _isolated) -> None:
        _record(run_id="s_bad", dd_ratio=3.0)
        out = audit_cli.show("s_bad")
        line = out.split("drawdown_ratio")[1].split("\n")[0]
        assert "3.00×" in line
        assert "FAIL" in line

    def test_exactly_cap_reads_pass(self, _isolated) -> None:
        _record(run_id="s_edge", dd_ratio=2.0)
        out = audit_cli.show("s_edge")
        line = out.split("drawdown_ratio")[1].split("\n")[0]
        assert "PASS" in line

    def test_inf_reads_fail(self, _isolated) -> None:
        _record(run_id="s_inf", dd_ratio=float("inf"))
        out = audit_cli.show("s_inf")
        line = out.split("drawdown_ratio")[1].split("\n")[0]
        assert "inf×" in line
        assert "FAIL" in line

    def test_na_when_missing(self, _isolated) -> None:
        _record(run_id="s_na", dd_ratio=None)
        out = audit_cli.show("s_na")
        assert "drawdown_ratio : (n/a" in out
