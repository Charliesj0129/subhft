"""Round 75: dominance_offenders() names the run_ids whose edge is dominated
by a single day (single_day_dominance_pct > strict 25.0% cap) or a single
month (top_month_contribution_pct > strict 50.0% cap), completing the 驗證標準
§5 (是否被少數交易/日期支配) offender-view trio alongside force_flat and
loss_concentration. Audit-layer only, no relaxed thresholds."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    day: float | None,
    month: float | None,
    strategy_type: str = "taker",
    profile_name: str = "vm_ul6_strict",
) -> None:
    advisory: list[dict] = [
        {
            "name": "edge_per_round_trip",
            "passed": True,
            "metrics": {"mean_net_edge_pts_per_trade": 12.0},
            "details": "",
            "error": False,
        }
    ]
    if day is not None:
        advisory.append(
            {
                "name": "single_day_dominance",
                "passed": day <= 25.0,
                "metrics": {"top_day_contribution_pct": day},
                "details": "",
                "error": False,
            }
        )
    if month is not None:
        advisory.append(
            {
                "name": "monthly_distribution",
                "passed": month <= 50.0,
                "metrics": {"top_month_contribution_pct": month},
                "details": "",
                "error": False,
            }
        )
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument="TXFD6",
        strategy_type=strategy_type,
        profile_name=profile_name,
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


class TestDominanceOffenders:
    def test_day_breach_listed_with_axis(self, _isolated) -> None:
        _record(run_id="daybad", day=40.0, month=30.0)  # day over 25, month under 50
        res = audit_cli.dominance_offenders()
        line = [r for r in res.split("\n") if r.startswith("daybad")][0]
        assert "40.0%" in line
        assert "day" in line.split()[-1]
        assert "month" not in line.split()[-1]

    def test_month_breach_listed_with_axis(self, _isolated) -> None:
        _record(run_id="monbad", day=10.0, month=70.0)  # month over 50, day under 25
        res = audit_cli.dominance_offenders()
        line = [r for r in res.split("\n") if r.startswith("monbad")][0]
        assert "70.0%" in line
        assert line.split()[-1] == "month"

    def test_both_axes_flagged(self, _isolated) -> None:
        _record(run_id="both", day=40.0, month=70.0)
        res = audit_cli.dominance_offenders()
        line = [r for r in res.split("\n") if r.startswith("both")][0]
        assert line.split()[-1] == "day+month"

    def test_under_both_caps_excluded(self, _isolated) -> None:
        _record(run_id="clean", day=20.0, month=40.0)
        res = audit_cli.dominance_offenders()
        assert "no rows over dominance caps" in res

    def test_exactly_at_caps_not_offenders(self, _isolated) -> None:
        _record(run_id="atcap", day=25.0, month=50.0)  # strict > required
        res = audit_cli.dominance_offenders()
        assert "no rows over dominance caps" in res

    def test_sorted_by_worst_excess_ratio(self, _isolated) -> None:
        # month 70/50 = 1.4 excess; day 40/25 = 1.6 excess → day row first.
        _record(run_id="monrow", day=10.0, month=70.0)
        _record(run_id="dayrow", day=40.0, month=10.0)
        res = audit_cli.dominance_offenders()
        body = [r for r in res.split("\n") if r.startswith(("monrow", "dayrow"))]
        assert body[0].startswith("dayrow")
        assert body[1].startswith("monrow")

    def test_strategy_type_filter(self, _isolated) -> None:
        _record(run_id="mk", day=40.0, month=10.0, strategy_type="maker")
        _record(run_id="tk", day=40.0, month=10.0, strategy_type="taker")
        res = audit_cli.dominance_offenders(strategy_type="maker")
        assert "mk" in res
        assert "tk" not in res

    def test_no_rows_matches_filter(self, _isolated) -> None:
        _record(run_id="only", day=40.0, month=10.0)
        res = audit_cli.dominance_offenders(profile="other")
        assert res == "no audit rows match filter."
