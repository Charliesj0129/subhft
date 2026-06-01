"""Round 64: triage_reason() collapses a row's promotion blockers onto the
迭代規則 §5 fixed vocabulary (promotable / failed / needs_more_sample /
blocked_by_parity / blocked_by_risk / blocked_by_audit), closing 驗證標準 §9
(知道策略保留/淘汰原因). Pure derivation, no relaxation."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    edge: float | None = 12.0,
    ff_share: float | None = 10.0,
    day_dom: float | None = 15.0,
    label: str | None = "adequate",
    dd_ratio: float | None = None,
    worst_loss: float | None = None,
    replay_match: float | None = None,
    blocking_passed: bool = True,
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
    if ff_share is not None:
        advisory.append(
            {
                "name": "force_flat_residual",
                "passed": ff_share <= 30.0,
                "metrics": {"force_flat_trip_share_pct": ff_share},
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
    if dd_ratio is not None:
        advisory.append(
            {
                "name": "monthly_distribution",
                "passed": dd_ratio <= 2.0,
                "metrics": {"drawdown_to_avg_monthly_ratio": dd_ratio},
                "details": "",
            }
        )
    if worst_loss is not None:
        advisory.append(
            {
                "name": "trade_concentration",
                "passed": worst_loss <= 50.0,
                "metrics": {"worst_loss_share_pct": worst_loss},
                "details": "",
            }
        )
    if replay_match is not None:
        advisory.append(
            {
                "name": "replay_parity",
                "passed": replay_match >= 95.0,
                "metrics": {"match_pct": replay_match},
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
        blocking={
            "passed": blocking_passed,
            "failing": [],
            "triage_status": "passed" if blocking_passed else "killed",
        },
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


class TestTriageReason:
    def test_all_clear_is_promotable(self, _isolated) -> None:
        _record(run_id="ok")
        assert audit_cli.triage_reason(_row("ok")) == "promotable"

    def test_low_edge_is_failed(self, _isolated) -> None:
        _record(run_id="edge", edge=5.0)
        assert audit_cli.triage_reason(_row("edge")) == "failed"

    def test_non_adequate_sample_is_needs_more_sample(self, _isolated) -> None:
        _record(run_id="samp", label="promising")
        assert audit_cli.triage_reason(_row("samp")) == "needs_more_sample"

    def test_replay_break_is_blocked_by_parity(self, _isolated) -> None:
        _record(run_id="rp", replay_match=80.0)
        assert audit_cli.triage_reason(_row("rp")) == "blocked_by_parity"

    def test_drawdown_breach_is_blocked_by_risk(self, _isolated) -> None:
        _record(run_id="dd", dd_ratio=3.0)
        assert audit_cli.triage_reason(_row("dd")) == "blocked_by_risk"

    def test_missing_axis_is_blocked_by_audit(self, _isolated) -> None:
        # No edge metric at all -> edge:missing -> audit gap.
        _record(run_id="bare", edge=None)
        assert audit_cli.triage_reason(_row("bare")) == "blocked_by_audit"

    def test_precedence_audit_over_parity_over_risk(self, _isolated) -> None:
        # Missing edge (audit) + parity break + drawdown breach all present;
        # audit wins.
        _record(run_id="multi", edge=None, replay_match=80.0, dd_ratio=3.0)
        assert audit_cli.triage_reason(_row("multi")) == "blocked_by_audit"

    def test_precedence_parity_over_risk(self, _isolated) -> None:
        _record(run_id="pr", replay_match=80.0, dd_ratio=3.0)
        assert audit_cli.triage_reason(_row("pr")) == "blocked_by_parity"


class TestShowSurfacesTriageReason:
    def test_show_promotable(self, _isolated) -> None:
        _record(run_id="s_ok")
        out = audit_cli.show("s_ok")
        assert "triage_reason  : promotable" in out

    def test_show_blocked_by_parity(self, _isolated) -> None:
        _record(run_id="s_rp", replay_match=80.0)
        out = audit_cli.show("s_rp")
        assert "triage_reason  : blocked_by_parity" in out
