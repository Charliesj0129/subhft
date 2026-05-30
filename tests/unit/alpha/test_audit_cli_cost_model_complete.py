"""Round 67: cost_model_complete() parses the declared cost_model_id and
flags §2 cost knobs (latency / fee / tax / slippage) that are absent or
non-numeric, so a silently-omitted cost component can't pass unnoticed
(驗證標準 §2). Reads the cost model, never relaxes it. Audit-layer only."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(*, run_id: str, cost_model_id: str | None) -> None:
    advisory = [
        {
            "name": "edge_per_round_trip",
            "passed": True,
            "metrics": {"mean_net_edge_pts_per_trade": 12.0},
            "details": "",
        }
    ]
    prov = None
    if cost_model_id is not None:
        prov = {
            "data_range": "2026-01-01..2026-03-31",
            "cost_model_id": cost_model_id,
            "required_gates": ["A", "B"],
        }
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


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


def _row(run_id: str) -> dict:
    return sub_gate_audit.read_runs(run_id)[0]


class TestCostModelCompleteFunction:
    def test_full_cost_model_is_complete(self, _isolated) -> None:
        _record(run_id="full", cost_model_id="v2026-04-24_measured+1.5bp/2.0bp/1.0pts")
        ok, missing = audit_cli.cost_model_complete(_row("full"))
        assert ok is True
        assert missing == []

    def test_no_provenance_lists_all_knobs(self, _isolated) -> None:
        _record(run_id="none", cost_model_id=None)
        ok, missing = audit_cli.cost_model_complete(_row("none"))
        assert ok is False
        assert set(missing) == {"latency_profile", "fee_bps", "tax_bps", "slippage_pts"}

    def test_none_fee_knob_is_flagged(self, _isolated) -> None:
        # An unset fee_bps serialises as "None" in the id.
        _record(run_id="nofee", cost_model_id="measured+Nonebp/2.0bp/1.0pts")
        ok, missing = audit_cli.cost_model_complete(_row("nofee"))
        assert ok is False
        assert missing == ["fee_bps"]

    def test_unspecified_latency_is_flagged(self, _isolated) -> None:
        _record(run_id="nolat", cost_model_id="unspecified+1.5bp/2.0bp/1.0pts")
        ok, missing = audit_cli.cost_model_complete(_row("nolat"))
        assert ok is False
        assert missing == ["latency_profile"]


class TestShowSurfacesCostModelCompleteness:
    def test_complete_line(self, _isolated) -> None:
        _record(run_id="s_ok", cost_model_id="measured+1.5bp/2.0bp/1.0pts")
        out = audit_cli.show("s_ok")
        assert "cost_model     : complete" in out

    def test_incomplete_lists_missing(self, _isolated) -> None:
        _record(run_id="s_bad", cost_model_id="measured+Nonebp/2.0bp/1.0pts")
        out = audit_cli.show("s_bad")
        line = out.split("cost_model ")[1].split("\n")[0]
        assert "INCOMPLETE" in line
        assert "fee_bps" in line
