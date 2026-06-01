"""Round 76: monthly_stability(row) + monthly_stability_review() implement the
驗證標準 §6 secondary checks ("若月收益不穩，需檢查 median_monthly_net_pnl /
worst_month / 單月收益支配性"). Advisory only, built from factual fields already
on the row — no new threshold, no relaxed bar. Audit-layer only."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    median: float | None = None,
    worst: float | None = None,
    top_month: float | None = None,
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
    metrics: dict[str, float] = {}
    if median is not None:
        metrics["median_monthly_net_pnl_pts"] = median
    if worst is not None:
        metrics["worst_monthly_pnl_pts"] = worst
    if top_month is not None:
        metrics["top_month_contribution_pct"] = top_month
    if metrics:
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


def _row(run_id: str) -> dict:
    return sub_gate_audit.read_runs(run_id)[0]


class TestMonthlyStabilityHelper:
    def test_unknown_when_no_monthly_metrics(self, _isolated) -> None:
        _record(run_id="none")
        verdict, reasons = audit_cli.monthly_stability(_row("none"))
        assert verdict == "unknown"
        assert reasons == []

    def test_stable_when_all_healthy(self, _isolated) -> None:
        _record(run_id="ok", median=30.0, worst=5.0, top_month=40.0)
        verdict, reasons = audit_cli.monthly_stability(_row("ok"))
        assert verdict == "stable"
        assert reasons == []

    def test_negative_worst_month_flagged(self, _isolated) -> None:
        _record(run_id="negw", median=30.0, worst=-12.0, top_month=40.0)
        verdict, reasons = audit_cli.monthly_stability(_row("negw"))
        assert verdict == "unstable"
        assert "negative_worst_month" in reasons

    def test_nonpositive_median_flagged(self, _isolated) -> None:
        _record(run_id="zerom", median=0.0, worst=5.0, top_month=40.0)
        verdict, reasons = audit_cli.monthly_stability(_row("zerom"))
        assert verdict == "unstable"
        assert "nonpositive_median" in reasons

    def test_top_month_dominant_flagged(self, _isolated) -> None:
        _record(run_id="dom", median=30.0, worst=5.0, top_month=70.0)
        verdict, reasons = audit_cli.monthly_stability(_row("dom"))
        assert verdict == "unstable"
        assert "top_month_dominant" in reasons

    def test_multiple_reasons_accumulate(self, _isolated) -> None:
        _record(run_id="multi", median=-1.0, worst=-20.0, top_month=80.0)
        verdict, reasons = audit_cli.monthly_stability(_row("multi"))
        assert verdict == "unstable"
        assert set(reasons) == {
            "negative_worst_month",
            "nonpositive_median",
            "top_month_dominant",
        }


class TestShowSurfacesMonthlyStability:
    def test_show_stable_line(self, _isolated) -> None:
        _record(run_id="s_ok", median=30.0, worst=5.0, top_month=40.0)
        out = audit_cli.show("s_ok")
        assert "monthly_stability: stable" in out

    def test_show_unstable_with_reasons(self, _isolated) -> None:
        _record(run_id="s_bad", median=30.0, worst=-12.0, top_month=40.0)
        out = audit_cli.show("s_bad")
        line = out.split("monthly_stability:")[1].split("\n")[0]
        assert "UNSTABLE" in line
        assert "negative_worst_month" in line

    def test_show_na_when_no_metrics(self, _isolated) -> None:
        _record(run_id="s_na")
        out = audit_cli.show("s_na")
        assert "monthly_stability: (n/a" in out


class TestMonthlyStabilityReview:
    def test_lists_only_unstable_sorted_by_worst_month(self, _isolated) -> None:
        _record(run_id="mild", median=30.0, worst=-5.0, top_month=40.0)
        _record(run_id="deep", median=30.0, worst=-40.0, top_month=40.0)
        _record(run_id="fine", median=30.0, worst=5.0, top_month=40.0)
        out = audit_cli.monthly_stability_review()
        body = [r for r in out.split("\n") if r.startswith(("mild", "deep", "fine"))]
        assert body[0].startswith("deep")  # deepest losing month first
        assert body[1].startswith("mild")
        assert "fine" not in out
        assert "(2 monthly-unstable" in out

    def test_no_unstable_rows_message(self, _isolated) -> None:
        _record(run_id="clean", median=30.0, worst=5.0, top_month=40.0)
        out = audit_cli.monthly_stability_review()
        assert "no rows flagged monthly-unstable" in out

    def test_strategy_type_filter(self, _isolated) -> None:
        _record(run_id="mk", median=30.0, worst=-5.0, strategy_type="maker")
        _record(run_id="tk", median=30.0, worst=-5.0, strategy_type="taker")
        out = audit_cli.monthly_stability_review(strategy_type="maker")
        assert "mk" in out
        assert "tk" not in out

    def test_no_rows_matches_filter(self, _isolated) -> None:
        _record(run_id="only", median=30.0, worst=-5.0)
        out = audit_cli.monthly_stability_review(profile="other")
        assert out == "no audit rows match filter."
