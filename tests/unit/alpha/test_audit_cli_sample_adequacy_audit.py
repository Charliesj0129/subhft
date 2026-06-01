"""Round 78: sample_adequacy_audit(row) reconciles the §4 'adequate' label
against its evidence (n_fills >= min_fills AND n_days >= min_days), flagging a
label the audit record cannot substantiate so a sample-short candidate can't
slip through (限制 §3 不足樣本不得完成). Audit-layer only, no new threshold."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    label: str | None,
    n_fills: float | None = None,
    min_fills: float | None = None,
    n_days: float | None = None,
    min_days: float | None = None,
    omit_gate: bool = False,
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
    if not omit_gate:
        metrics: dict[str, object] = {}
        if label is not None:
            metrics["sample_adequacy_label"] = label
        if n_fills is not None:
            metrics["n_fills"] = n_fills
        if min_fills is not None:
            metrics["min_fills"] = min_fills
        if n_days is not None:
            metrics["n_days"] = n_days
        if min_days is not None:
            metrics["min_days"] = min_days
        advisory.append(
            {
                "name": "min_sample_size",
                "passed": label == "adequate",
                "metrics": metrics,
                "details": "",
                "error": False,
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


class TestSampleAdequacyAudit:
    def test_no_gate(self, _isolated) -> None:
        _record(run_id="nog", label=None, omit_gate=True)
        verdict, reasons = audit_cli.sample_adequacy_audit(_row("nog"))
        assert verdict == "no_gate"
        assert reasons == []

    def test_non_adequate_label_is_consistent(self, _isolated) -> None:
        _record(run_id="prom", label="promising", n_fills=10, min_fills=80, n_days=2, min_days=20)
        verdict, reasons = audit_cli.sample_adequacy_audit(_row("prom"))
        assert verdict == "consistent"
        assert reasons == []

    def test_adequate_with_backing_evidence_consistent(self, _isolated) -> None:
        _record(run_id="ok", label="adequate", n_fills=120, min_fills=80, n_days=25, min_days=20)
        verdict, reasons = audit_cli.sample_adequacy_audit(_row("ok"))
        assert verdict == "consistent"
        assert reasons == []

    def test_adequate_but_fills_short_discrepant(self, _isolated) -> None:
        _record(run_id="fs", label="adequate", n_fills=40, min_fills=80, n_days=25, min_days=20)
        verdict, reasons = audit_cli.sample_adequacy_audit(_row("fs"))
        assert verdict == "discrepant"
        assert "fills_below_min" in reasons
        assert "days_below_min" not in reasons

    def test_adequate_but_days_short_discrepant(self, _isolated) -> None:
        _record(run_id="ds", label="adequate", n_fills=120, min_fills=80, n_days=5, min_days=20)
        verdict, reasons = audit_cli.sample_adequacy_audit(_row("ds"))
        assert verdict == "discrepant"
        assert "days_below_min" in reasons

    def test_adequate_but_evidence_missing_discrepant(self, _isolated) -> None:
        _record(run_id="em", label="adequate")  # no counts at all
        verdict, reasons = audit_cli.sample_adequacy_audit(_row("em"))
        assert verdict == "discrepant"
        assert "fills_evidence_missing" in reasons
        assert "days_evidence_missing" in reasons


class TestShowSurfacesReconciliation:
    def test_show_flags_discrepant_adequate(self, _isolated) -> None:
        _record(run_id="s_bad", label="adequate", n_fills=40, min_fills=80, n_days=25, min_days=20)
        out = audit_cli.show("s_bad")
        line = out.split("sample_adequacy:")[1].split("\n")[0]
        assert "DISCREPANT" in line
        assert "fills_below_min" in line

    def test_show_clean_for_backed_adequate(self, _isolated) -> None:
        _record(run_id="s_ok", label="adequate", n_fills=120, min_fills=80, n_days=25, min_days=20)
        out = audit_cli.show("s_ok")
        line = out.split("sample_adequacy:")[1].split("\n")[0]
        assert "DISCREPANT" not in line
        assert "READY" in line
