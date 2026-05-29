"""Round 26: mean_net_edge_pts_per_trade surfaced at row + CLI top level."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _advisory_with_edge(value: float | None) -> list[dict]:
    metrics: dict[str, float] = (
        {"mean_net_edge_pts_per_trade": value} if value is not None else {}
    )
    return [
        {
            "name": "some_other_gate",
            "passed": True,
            "metrics": {"foo": 1.0},
            "details": "noise",
        },
        {
            "name": "edge_per_round_trip",
            "passed": (value is not None and value > 10.0),
            "metrics": metrics,
            "details": "stub",
        },
    ]


class TestBuildRecordExtractsEdge:
    def test_top_level_edge_added_when_advisory_carries_it(self) -> None:
        row = sub_gate_audit.build_record(
            run_id="r26_pass",
            strategy_name="demo",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory_with_edge(12.5),
            blocking={"passed": True, "failing": [], "triage_status": "passed"},
            recorded_at_ns=1,
        )
        assert row["mean_net_edge_pts_per_trade"] == 12.5

    def test_field_omitted_when_gate_did_not_run(self) -> None:
        row = sub_gate_audit.build_record(
            run_id="r26_no_gate",
            strategy_name="demo",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=[
                {"name": "sharpe_threshold", "passed": True, "metrics": {}, "details": ""}
            ],
            blocking={"passed": True, "failing": [], "triage_status": "passed"},
            recorded_at_ns=1,
        )
        assert "mean_net_edge_pts_per_trade" not in row

    def test_field_omitted_when_metric_value_missing(self) -> None:
        row = sub_gate_audit.build_record(
            run_id="r26_no_metric",
            strategy_name="demo",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory_with_edge(None),
            blocking={"passed": True, "failing": [], "triage_status": "passed"},
            recorded_at_ns=1,
        )
        assert "mean_net_edge_pts_per_trade" not in row

    def test_field_omitted_when_metric_value_non_numeric(self) -> None:
        row = sub_gate_audit.build_record(
            run_id="r26_bad_value",
            strategy_name="demo",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=[
                {
                    "name": "edge_per_round_trip",
                    "passed": False,
                    "metrics": {"mean_net_edge_pts_per_trade": "oops"},
                    "details": "",
                }
            ],
            blocking={"passed": False, "failing": [], "triage_status": "killed"},
            recorded_at_ns=1,
        )
        assert "mean_net_edge_pts_per_trade" not in row


class TestAuditCliRendersEdge:
    @pytest.fixture
    def _isolated(self, tmp_path, monkeypatch):
        path = tmp_path / "audit.jsonl"
        monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
        sub_gate_audit._reset_cache_for_tests()
        return path

    def _record(self, run_id: str, edge: float | None) -> None:
        sub_gate_audit.record_sub_gate_run(
            run_id=run_id,
            strategy_name="demo",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory_with_edge(edge),
            blocking={
                "passed": edge is not None and edge > 10.0,
                "failing": [],
                "triage_status": "passed" if (edge or 0) > 10.0 else "killed",
            },
            recorded_at_ns=1,
        )

    def test_show_marks_pass_when_edge_above_floor(self, _isolated) -> None:
        self._record("r26_show_pass", 12.0)
        out = audit_cli.show("r26_show_pass")
        assert "mean_net_edge" in out
        assert "12.000" in out
        assert "PASS" in out

    def test_show_marks_fail_when_edge_below_floor(self, _isolated) -> None:
        self._record("r26_show_fail", 6.5)
        out = audit_cli.show("r26_show_fail")
        assert "6.500" in out
        assert "FAIL" in out

    def test_show_displays_na_when_no_edge_metric(self, _isolated) -> None:
        # Record without the edge_per_round_trip gate.
        sub_gate_audit.record_sub_gate_run(
            run_id="r26_show_na",
            strategy_name="demo",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="loose",
            advisory=[{"name": "sharpe_threshold", "passed": True, "metrics": {}, "details": ""}],
            blocking=None,
            recorded_at_ns=1,
        )
        out = audit_cli.show("r26_show_na")
        assert "n/a" in out

    def test_compare_flags_edge_drift(self, _isolated) -> None:
        self._record("r26_cmp_a", 8.0)
        self._record("r26_cmp_b", 14.0)
        out = audit_cli.compare("r26_cmp_a", "r26_cmp_b")
        assert "mean_net_edge" in out
        assert "8.0" in out and "14.0" in out
        assert "goal §5 floor" in out
