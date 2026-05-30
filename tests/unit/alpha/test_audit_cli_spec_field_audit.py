"""Round 70: spec_field_audit() reports which 完成狀態 §3 fixed-spec fields the
audit record itself can attest to (strategy_name / instrument / cost_model /
validation_plan) vs which live only in spec.yaml (the behavioural rules),
making the traceability boundary explicit without re-loading the spec or
fabricating evidence. Audit-layer only."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(*, run_id: str, prov: dict | None) -> None:
    advisory = [
        {
            "name": "edge_per_round_trip",
            "passed": True,
            "metrics": {"mean_net_edge_pts_per_trade": 12.0},
            "details": "",
        }
    ]
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument="TXFD6",
        strategy_type="taker",
        profile_name="vm_ul6_strict",
        advisory=advisory,
        blocking={"passed": True, "failing": [], "triage_status": "passed"},
        recorded_at_ns=1,
        spec_provenance=prov,
    )


_FULL = {
    "data_range": "2026-01-01..2026-03-31",
    "cost_model_id": "measured+1.5bp/2.0bp/1.0pts",
    "required_gates": ["A", "B"],
}


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


def _row(run_id: str) -> dict:
    return sub_gate_audit.read_runs(run_id)[0]


class TestSpecFieldAuditFunction:
    def test_full_provenance_attests_four_fields(self, _isolated) -> None:
        _record(run_id="full", prov=dict(_FULL))
        traceable, untraceable = audit_cli.spec_field_audit(_row("full"))
        assert set(traceable) == {
            "strategy_name",
            "instrument",
            "cost_model",
            "validation_plan",
        }
        # The 8 behavioural fields are never recoverable from the record.
        assert "hypothesis" in untraceable
        assert "entry_rule" in untraceable
        assert "risk_control" in untraceable
        assert len(traceable) + len(untraceable) == len(audit_cli._SPEC_FIELDS_3)

    def test_no_provenance_attests_only_top_level(self, _isolated) -> None:
        _record(run_id="bare", prov=None)
        traceable, _ = audit_cli.spec_field_audit(_row("bare"))
        # Without provenance, only the row's own top-level fields attest.
        assert set(traceable) == {"strategy_name", "instrument"}

    def test_incomplete_cost_model_not_attested(self, _isolated) -> None:
        prov = dict(_FULL)
        prov["cost_model_id"] = "measured+Nonebp/2.0bp/1.0pts"
        _record(run_id="badcost", prov=prov)
        traceable, untraceable = audit_cli.spec_field_audit(_row("badcost"))
        assert "cost_model" in untraceable
        assert "cost_model" not in traceable


class TestShowSurfacesSpecFieldAudit:
    def test_show_counts_traceable_fields(self, _isolated) -> None:
        _record(run_id="s_full", prov=dict(_FULL))
        out = audit_cli.show("s_full")
        line = out.split("spec_fields")[1].split("\n")[0]
        assert "4/12 traceable" in line
        assert "hypothesis" in line

    def test_show_bare_record(self, _isolated) -> None:
        _record(run_id="s_bare", prov=None)
        out = audit_cli.show("s_bare")
        line = out.split("spec_fields")[1].split("\n")[0]
        assert "2/12 traceable" in line
