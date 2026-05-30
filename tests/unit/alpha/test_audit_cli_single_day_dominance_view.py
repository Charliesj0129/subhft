"""Round 48: single_day_dominance lifted to a top-level row field and
surfaced in `audit show` (驗證標準 §5: OOS dominated by few dates)."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    day_dom: float | None,
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
    if day_dom is not None:
        advisory.append(
            {
                "name": "single_day_dominance",
                "passed": day_dom <= 25.0,
                "metrics": {"top_day_contribution_pct": day_dom, "threshold_pct": 25.0},
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


class TestBuildRecordLiftsDominance:
    def test_dominance_lifted_to_top_level(self, _isolated) -> None:
        _record(run_id="d_a", day_dom=18.0)
        rows = sub_gate_audit.read_runs()
        assert rows[0]["single_day_dominance_pct"] == pytest.approx(18.0)

    def test_absent_when_gate_didnt_run(self, _isolated) -> None:
        _record(run_id="d_b", day_dom=None)
        rows = sub_gate_audit.read_runs()
        assert "single_day_dominance_pct" not in rows[0]


class TestShowSurfacesDominanceLine:
    def test_show_pass_under_cap(self, _isolated) -> None:
        _record(run_id="s_pass", day_dom=20.0)
        out = audit_cli.show("s_pass")
        assert "single_day_dom : 20.0% of |total|" in out
        assert "PASS" in out.split("single_day_dom")[1].split("\n")[0]

    def test_show_fail_above_cap(self, _isolated) -> None:
        _record(run_id="s_fail", day_dom=60.0)
        out = audit_cli.show("s_fail")
        assert "single_day_dom : 60.0% of |total|" in out
        assert "FAIL" in out.split("single_day_dom")[1].split("\n")[0]

    def test_show_na_when_missing(self, _isolated) -> None:
        _record(run_id="s_na", day_dom=None)
        out = audit_cli.show("s_na")
        assert "single_day_dom : (n/a" in out

    def test_show_line_order_edge_then_dominance(self, _isolated) -> None:
        _record(run_id="s_order", day_dom=15.0, edge=12.0)
        out = audit_cli.show("s_order")
        assert out.index("mean_net_edge") < out.index("single_day_dom")
