"""Round 80: scorecard(run_id) renders a one-block promotion decision record
(驗證標準 §9 知道策略保留/淘汰原因 + 回放研究決策) — composite verdict + triage
+ an axis->verdict table sourced from promotion_readiness so it never drifts
from the kept/killed call. Audit-layer only, no relaxed thresholds."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    edge: float | None = 12.0,
    day_dom: float | None = 15.0,
    label: str | None = "adequate",
    worst_loss: float | None = None,
) -> None:
    advisory: list[dict] = []
    if edge is not None:
        advisory.append(
            {
                "name": "edge_per_round_trip",
                "passed": edge > 10.0,
                "metrics": {"mean_net_edge_pts_per_trade": edge},
                "details": "",
            }
        )
    if day_dom is not None:
        advisory.append(
            {
                "name": "single_day_dominance",
                "passed": day_dom <= 25.0,
                "metrics": {"top_day_contribution_pct": day_dom},
                "details": "",
            }
        )
    if label is not None:
        advisory.append(
            {
                "name": "min_sample_size",
                "passed": label == "adequate",
                "metrics": {"sample_adequacy_label": label},
                "details": "",
            }
        )
    if worst_loss is not None:
        advisory.append(
            {
                "name": "trade_concentration",
                "passed": worst_loss <= 50.0,
                "metrics": {"n_trades": 80.0, "worst_loss_share_pct": worst_loss},
                "details": "",
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


def _row(run_id: str) -> dict:
    return sub_gate_audit.read_runs(run_id)[0]


class TestScorecardAxes:
    def test_passing_axes_marked_pass(self, _isolated) -> None:
        _record(run_id="ok")
        axes = dict((a, (v, vd)) for a, v, vd in audit_cli.scorecard_axes(_row("ok")))
        assert axes["edge>10 (§5)"][1] == "PASS"
        assert axes["day_dom<=25 (§5)"][1] == "PASS"
        assert axes["sample (§4)"][1] == "PASS"

    def test_breach_marked_fail(self, _isolated) -> None:
        _record(run_id="bad", worst_loss=80.0)
        axes = dict((a, vd) for a, _v, vd in audit_cli.scorecard_axes(_row("bad")))
        assert axes["worst_loss<=50 (§5)"] == "FAIL"

    def test_missing_required_axis_marked_missing(self, _isolated) -> None:
        _record(run_id="nos", label=None)
        axes = dict((a, vd) for a, _v, vd in audit_cli.scorecard_axes(_row("nos")))
        assert axes["sample (§4)"] == "MISSING"

    def test_inapplicable_axis_marked_na(self, _isolated) -> None:
        _record(run_id="nona")  # no trade_concentration gate
        axes = dict((a, vd) for a, _v, vd in audit_cli.scorecard_axes(_row("nona")))
        assert axes["worst_loss<=50 (§5)"] == "n/a"

    def test_verdict_consistent_with_promotion_readiness(self, _isolated) -> None:
        _record(run_id="low", edge=5.0)
        ready, _ = audit_cli.promotion_readiness(_row("low"))
        axes = dict((a, vd) for a, _v, vd in audit_cli.scorecard_axes(_row("low")))
        assert ready is False
        assert axes["edge>10 (§5)"] == "FAIL"


class TestScorecardRender:
    def test_ready_block(self, _isolated) -> None:
        _record(run_id="rdy")
        out = audit_cli.scorecard("rdy")
        assert "promotion scorecard: rdy" in out
        assert "verdict: READY" in out
        assert "triage=promotable" in out
        assert "edge>10 (§5)" in out

    def test_not_ready_block_lists_blockers(self, _isolated) -> None:
        _record(run_id="nr", edge=5.0)
        out = audit_cli.scorecard("nr")
        assert "verdict: NOT-READY" in out
        assert "triage=failed" in out
        assert "blockers:" in out
        assert "edge" in out.split("blockers:")[1]

    def test_unknown_run_id(self, _isolated) -> None:
        out = audit_cli.scorecard("ghost")
        assert "no audit row for run_id='ghost'" in out
