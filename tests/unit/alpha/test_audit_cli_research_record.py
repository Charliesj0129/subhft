"""Round 87: `audit record` auto-generates one run's full research record as
Markdown (驗證標準 §9 自動產生研究紀錄 + §4 可追溯紀錄).  Distinct from scorecard
(compact axes) and export (cohort table): a single self-contained document with
header + provenance + scorecard + full sub-gate metrics + kept/killed verdict.
Read-only; audit-layer scope."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str = "r1",
    edge: float = 12.0,
    with_provenance: bool = True,
    inventory: tuple[float, float, float] | None = None,
) -> None:
    advisory = [
        {
            "name": "edge_per_round_trip",
            "passed": edge > 10.0,
            "metrics": {"mean_net_edge_pts_per_trade": edge},
            "details": "",
        },
        {
            "name": "single_day_dominance",
            "passed": True,
            "metrics": {"top_day_contribution_pct": 15.0},
            "details": "",
        },
    ]
    if inventory is not None:
        realized, residual, net = inventory
        advisory.append(
            {
                "name": "inventory_mtm",
                "passed": True,
                "metrics": {
                    "realized_pts": realized,
                    "residual_mtm_pts": residual,
                    "net_pts": net,
                },
                "details": "",
            }
        )
    prov = (
        {
            "data_range": "2026-Q1",
            "cost_model_id": "measured+1bp/2bp/1pts",
            "required_gates": ["edge_per_round_trip"],
        }
        if with_provenance
        else None
    )
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name="alpha_demo",
        instrument="TXFD6",
        strategy_type="taker",
        profile_name="vm_ul6_strict",
        advisory=advisory,
        blocking={"passed": True, "failing": [], "triage_status": "passed"},
        recorded_at_ns=42,
        spec_provenance=prov,
    )


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestResearchRecord:
    def test_missing_run_reported(self, _isolated) -> None:
        out = audit_cli.research_record("nope")
        assert "no audit row for run_id='nope'" in out

    def test_header_and_identity_fields(self, _isolated) -> None:
        _record()
        out = audit_cli.research_record("r1")
        assert "# Research Record — alpha_demo" in out
        assert "run_id: `r1`" in out
        assert "instrument: TXFD6" in out
        assert "strategy_type: taker" in out
        assert "profile: vm_ul6_strict" in out
        assert "recorded_at_ns: 42" in out

    def test_verdict_sourced_from_promotion_readiness(self, _isolated) -> None:
        _record(edge=12.0)
        row = sub_gate_audit.read_runs("r1")[0]
        ready, _ = audit_cli.promotion_readiness(row)
        reason = audit_cli.triage_reason(row)
        out = audit_cli.research_record("r1")
        assert ("READY" if ready else "NOT-READY") in out
        assert f"triage: `{reason}`" in out

    def test_provenance_section_present(self, _isolated) -> None:
        _record(with_provenance=True)
        out = audit_cli.research_record("r1")
        assert "## Spec provenance" in out
        assert "data_range: 2026-Q1" in out
        assert "cost_model_id: measured+1bp/2bp/1pts" in out
        assert "required_gates: ['edge_per_round_trip']" in out

    def test_provenance_absent_flagged(self, _isolated) -> None:
        _record(with_provenance=False)
        out = audit_cli.research_record("r1")
        assert "no spec_provenance recorded" in out
        assert "provenance incomplete" in out

    def test_scorecard_table_rendered(self, _isolated) -> None:
        _record()
        out = audit_cli.research_record("r1")
        assert "## Credibility scorecard" in out
        assert "| axis | value | verdict |" in out

    def test_subgate_metrics_table_rendered(self, _isolated) -> None:
        _record()
        out = audit_cli.research_record("r1")
        assert "## Sub-gate metrics" in out
        assert "| gate | passed | metrics |" in out
        assert "edge_per_round_trip" in out
        assert "mean_net_edge_pts_per_trade=12.0" in out

    def test_blockers_listed_when_failed(self, _isolated) -> None:
        _record(edge=8.0)  # below floor -> edge blocker
        out = audit_cli.research_record("r1")
        assert "NOT-READY" in out
        assert "edge" in out  # blocker name surfaces

    def test_propped_flag_in_record(self, _isolated) -> None:
        # net>0 but realized<=0 -> §3 residual-propped edge.
        _record(inventory=(-3.0, 15.0, 12.0))
        out = audit_cli.research_record("r1")
        assert "residual_propped (§3): **PROPPED**" in out

    def test_clean_residual_flag_in_record(self, _isolated) -> None:
        _record(inventory=(8.0, 4.0, 12.0))
        out = audit_cli.research_record("r1")
        assert "residual_propped (§3): clean" in out

    def test_residual_line_omitted_when_gate_absent(self, _isolated) -> None:
        _record()  # no inventory_mtm gate
        out = audit_cli.research_record("r1")
        assert "residual_propped" not in out

    def test_cli_dispatch(self, _isolated, capsys) -> None:
        _record()
        rc = audit_cli.main(["record", "r1"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "# Research Record — alpha_demo" in captured
