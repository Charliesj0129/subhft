"""Round 51: composite promotion_readiness verdict over the lifted axes
(edge / force-flat / dominance / sample-adequacy / blocking) — 驗證標準 §9
kept/killed rationale in one line. Pure-derivation, no relaxation."""

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
                "metrics": {"top_day_contribution_pct": day_dom, "threshold_pct": 25.0},
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


class TestPromotionReadinessFunction:
    def test_all_axes_clear_is_ready(self, _isolated) -> None:
        _record(run_id="ok")
        ready, blockers = audit_cli.promotion_readiness(_row("ok"))
        assert ready is True
        assert blockers == []

    def test_low_edge_blocks(self, _isolated) -> None:
        _record(run_id="low_edge", edge=8.0)
        ready, blockers = audit_cli.promotion_readiness(_row("low_edge"))
        assert ready is False
        assert "edge" in blockers

    def test_edge_exactly_floor_blocks(self, _isolated) -> None:
        # Strictly greater than 10 required (goal §5: > 10).
        _record(run_id="at_floor", edge=10.0)
        _, blockers = audit_cli.promotion_readiness(_row("at_floor"))
        assert "edge" in blockers

    def test_high_force_flat_blocks(self, _isolated) -> None:
        _record(run_id="ff", ff_share=55.0)
        _, blockers = audit_cli.promotion_readiness(_row("ff"))
        assert "force_flat" in blockers

    def test_high_dominance_blocks(self, _isolated) -> None:
        _record(run_id="dom", day_dom=60.0)
        _, blockers = audit_cli.promotion_readiness(_row("dom"))
        assert "dominance" in blockers

    def test_non_adequate_sample_blocks(self, _isolated) -> None:
        _record(run_id="samp", label="promising")
        _, blockers = audit_cli.promotion_readiness(_row("samp"))
        assert "sample" in blockers

    def test_blocking_failed_blocks(self, _isolated) -> None:
        _record(run_id="blk", blocking_passed=False)
        _, blockers = audit_cli.promotion_readiness(_row("blk"))
        assert "blocking" in blockers

    def test_missing_edge_and_dominance_and_sample_are_blockers(self, _isolated) -> None:
        _record(run_id="bare", edge=None, ff_share=None, day_dom=None, label=None)
        ready, blockers = audit_cli.promotion_readiness(_row("bare"))
        assert ready is False
        assert "edge:missing" in blockers
        assert "dominance:missing" in blockers
        assert "sample:missing" in blockers
        # force-flat missing is intentionally NOT a blocker.
        assert not any(b.startswith("force_flat") for b in blockers)

    def test_multiple_blockers_accumulate(self, _isolated) -> None:
        _record(run_id="multi", edge=5.0, day_dom=80.0, label="inconclusive")
        _, blockers = audit_cli.promotion_readiness(_row("multi"))
        assert {"edge", "dominance", "sample"}.issubset(set(blockers))


class TestShowSurfacesVerdict:
    def test_show_ready_line(self, _isolated) -> None:
        _record(run_id="s_ready")
        out = audit_cli.show("s_ready")
        assert "promotion_ready: READY" in out

    def test_show_not_ready_lists_blockers(self, _isolated) -> None:
        _record(run_id="s_block", edge=5.0)
        out = audit_cli.show("s_block")
        line = out.split("promotion_ready")[1].split("\n")[0]
        assert "NOT-READY" in line
        assert "edge" in line


class TestSummaryCountsReady:
    def test_summary_counts_ready_rows(self, _isolated) -> None:
        _record(run_id="r1")  # ready
        _record(run_id="r2")  # ready
        _record(run_id="r3", edge=4.0)  # not ready
        out = audit_cli.summary()
        assert "promotion_readiness" in out
        section = out.split("promotion_readiness")[1]
        assert "READY rows     : 2 / 3" in section
