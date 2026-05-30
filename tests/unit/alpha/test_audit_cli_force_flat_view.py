"""Round 45: force_flat_residual surfaced in audit show + summary."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    ff_share: float | None,
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
    if ff_share is not None:
        advisory.append(
            {
                "name": "force_flat_residual",
                "passed": ff_share <= 30.0,
                "metrics": {"force_flat_trip_share_pct": ff_share},
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


class TestBuildRecordLifsForceFlatShare:
    def test_share_lifted_to_top_level_when_gate_present(self, _isolated) -> None:
        _record(run_id="ff_a", ff_share=12.5)
        rows = sub_gate_audit.read_runs()
        assert rows[0]["force_flat_trip_share_pct"] == pytest.approx(12.5)

    def test_share_absent_when_gate_didnt_run(self, _isolated) -> None:
        _record(run_id="ff_b", ff_share=None)
        rows = sub_gate_audit.read_runs()
        assert "force_flat_trip_share_pct" not in rows[0]


class TestShowSurfacesForceFlatLine:
    def test_show_displays_pass_marker_under_cap(self, _isolated) -> None:
        _record(run_id="s_pass", ff_share=10.0)
        out = audit_cli.show("s_pass")
        assert "force_flat_share: 10.0% of trips" in out
        assert "PASS" in out.split("force_flat_share")[1].split("\n")[0]

    def test_show_displays_fail_marker_above_cap(self, _isolated) -> None:
        _record(run_id="s_fail", ff_share=45.0)
        out = audit_cli.show("s_fail")
        assert "force_flat_share: 45.0% of trips" in out
        assert "FAIL" in out.split("force_flat_share")[1].split("\n")[0]

    def test_show_displays_na_when_metric_missing(self, _isolated) -> None:
        _record(run_id="s_na", ff_share=None)
        out = audit_cli.show("s_na")
        assert "force_flat_share: (n/a" in out

    def test_show_line_order_edge_then_force_flat(self, _isolated) -> None:
        # Reviewer expectation: edge metric immediately above the
        # force-flat metric so the relationship is visible at a glance.
        _record(run_id="s_order", ff_share=15.0, edge=12.0)
        out = audit_cli.show("s_order")
        i_edge = out.index("mean_net_edge")
        i_ff = out.index("force_flat_share")
        assert i_edge < i_ff


class TestSummaryAggregatesForceFlat:
    def test_summary_reports_share_block(self, _isolated) -> None:
        _record(run_id="sum_a", ff_share=10.0)
        _record(run_id="sum_b", ff_share=40.0)
        _record(run_id="sum_c", ff_share=25.0)
        out = audit_cli.summary()
        assert "force_flat_residual" in out
        assert "rows with metric: 3 / 3" in out
        assert "rows over cap   : 1" in out  # only 40 % exceeds 30 %
        assert "share min/max" in out

    def test_summary_handles_rows_without_metric(self, _isolated) -> None:
        _record(run_id="sum_x", ff_share=12.0)
        _record(run_id="sum_y", ff_share=None)
        out = audit_cli.summary()
        assert "rows with metric: 1 / 2" in out

    def test_summary_share_block_absent_metrics_still_renders_header(self, _isolated) -> None:
        # Even when no row has the metric, the header should be present
        # so reviewers know the gate wasn't run (vs gate ran and passed).
        _record(run_id="empty_a", ff_share=None)
        _record(run_id="empty_b", ff_share=None)
        out = audit_cli.summary()
        assert "force_flat_residual" in out
        assert "rows with metric: 0 / 2" in out
        assert "rows over cap   : 0" in out
