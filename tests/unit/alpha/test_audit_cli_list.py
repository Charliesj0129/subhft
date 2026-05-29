"""Round 27: ``audit list`` subcommand for goal §5 / §9 visibility."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    strategy: str = "demo",
    instrument: str = "TXFD6",
    strategy_type: str = "maker",
    profile: str = "vm_ul6_strict",
    edge: float | None,
    blocking_passed: bool | None = True,
    triage: str = "passed",
) -> None:
    advisory: list[dict] = []
    if edge is not None:
        advisory.append(
            {
                "name": "edge_per_round_trip",
                "passed": edge > 10.0,
                "metrics": {"mean_net_edge_pts_per_trade": edge},
                "details": "stub",
            }
        )
    blocking: dict | None
    if blocking_passed is None:
        blocking = None
    else:
        blocking = {
            "passed": blocking_passed,
            "failing": [],
            "triage_status": triage,
        }
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=strategy,
        instrument=instrument,
        strategy_type=strategy_type,
        profile_name=profile,
        advisory=advisory,
        blocking=blocking,
        recorded_at_ns=1,
    )


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestAuditCliList:
    def test_empty_audit_returns_no_match_message(self, _isolated) -> None:
        out = audit_cli.list_runs()
        assert "no audit rows match" in out

    def test_lists_all_rows_with_edge_column(self, _isolated) -> None:
        _record(run_id="r27_a", strategy="alpha_one", edge=12.5)
        _record(run_id="r27_b", strategy="alpha_two", edge=6.5, blocking_passed=False, triage="killed")
        out = audit_cli.list_runs()
        # Header + separator + 2 rows + footer.
        assert "run_id" in out and "edge" in out and "triage" in out
        assert "12.50" in out
        assert "6.50" in out
        assert "(2 rows)" in out

    def test_edge_min_drops_below_floor_and_no_metric_rows(self, _isolated) -> None:
        _record(run_id="r27_pass", edge=14.0)
        _record(run_id="r27_fail", edge=8.0)
        _record(run_id="r27_no_edge", edge=None)
        out = audit_cli.list_runs(edge_min=10.0)
        assert "r27_pass" in out
        assert "r27_fail" not in out
        assert "r27_no_edge" not in out
        assert "(1 row)" in out

    def test_only_passing_filters_blocking_failed(self, _isolated) -> None:
        _record(run_id="r27_p", edge=15.0, blocking_passed=True)
        _record(run_id="r27_f", edge=15.0, blocking_passed=False, triage="killed")
        _record(run_id="r27_l", edge=15.0, blocking_passed=None)
        out = audit_cli.list_runs(only_passing=True)
        assert "r27_p" in out
        assert "r27_f" not in out
        assert "r27_l" not in out

    def test_profile_filter_exact_match(self, _isolated) -> None:
        _record(run_id="r27_strict", edge=12.0, profile="vm_ul6_strict")
        _record(run_id="r27_loose", edge=12.0, profile="loose")
        out = audit_cli.list_runs(profile="vm_ul6_strict")
        assert "r27_strict" in out
        assert "r27_loose" not in out

    def test_strategy_type_filter(self, _isolated) -> None:
        _record(run_id="r27_m", edge=12.0, strategy_type="maker")
        _record(run_id="r27_t", edge=12.0, strategy_type="taker")
        out = audit_cli.list_runs(strategy_type="maker")
        assert "r27_m" in out
        assert "r27_t" not in out

    def test_main_dispatches_list_subcommand(self, _isolated, capsys) -> None:
        _record(run_id="r27_main", edge=11.5)
        rc = audit_cli.main(["list", "--edge-min", "10"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "r27_main" in captured
        assert "11.50" in captured

    def test_loose_rows_show_loose_block_marker(self, _isolated) -> None:
        _record(run_id="r27_loose", edge=12.0, blocking_passed=None)
        out = audit_cli.list_runs()
        assert "(loose)" in out
