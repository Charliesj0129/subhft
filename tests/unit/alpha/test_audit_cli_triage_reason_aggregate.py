"""Round 65: triage_reason aggregated in `audit summary` (cohort split across
the 迭代規則 §5 vocabulary) and carried as a column in `audit export`, so a
cross-candidate sweep can group kept/killed by category (驗證標準 §9 比較策略).
Audit-layer only."""

from __future__ import annotations

import csv
import io

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    edge: float | None = 12.0,
    label: str | None = "adequate",
    replay_match: float | None = None,
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
    advisory.append(
        {
            "name": "single_day_dominance",
            "passed": True,
            "metrics": {"top_day_contribution_pct": 15.0},
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
        blocking={"passed": True, "failing": [], "triage_status": "passed"},
        recorded_at_ns=1,
    )


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestSummaryAggregatesTriageReason:
    def test_summary_counts_categories(self, _isolated) -> None:
        _record(run_id="ok1")  # promotable
        _record(run_id="ok2")  # promotable
        _record(run_id="bad", edge=5.0)  # failed
        _record(run_id="rp", replay_match=80.0)  # blocked_by_parity
        out = audit_cli.summary()
        assert "triage_reason distribution (迭代規則 §5 vocabulary):" in out
        section = out.split("triage_reason distribution")[1]
        assert "promotable        : 2" in section
        assert "failed            : 1" in section
        assert "blocked_by_parity : 1" in section


class TestExportCarriesTriageReason:
    def test_csv_column_values(self, _isolated) -> None:
        _record(run_id="ok")
        _record(run_id="rp", replay_match=80.0)
        out = audit_cli.export(fmt="csv")
        reader = csv.DictReader(io.StringIO(out))
        rows = {r["run_id"]: r for r in reader}
        assert "triage_reason" in reader.fieldnames
        assert rows["ok"]["triage_reason"] == "promotable"
        assert rows["rp"]["triage_reason"] == "blocked_by_parity"
