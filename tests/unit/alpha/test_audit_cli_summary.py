"""Round 28: ``audit summary`` aggregate counts (goal §5 / §9 dashboard)."""

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


class TestAuditCliSummary:
    def test_empty_audit_returns_no_match_message(self, _isolated) -> None:
        assert "no audit rows match" in audit_cli.summary()

    def test_counts_total_type_and_blocking(self, _isolated) -> None:
        _record(run_id="s_a", edge=12.0, strategy_type="maker", blocking_passed=True)
        _record(run_id="s_b", edge=8.0, strategy_type="maker", blocking_passed=False, triage="killed")
        _record(run_id="s_c", edge=15.0, strategy_type="taker", blocking_passed=True)
        out = audit_cli.summary()
        assert "total rows     : 3" in out
        assert "maker=2 taker=1" in out
        assert "PASS=2 FAIL=1" in out

    def test_edge_floor_and_percentiles(self, _isolated) -> None:
        # 5 edges: 6, 9, 11, 14, 22 -> above floor: 3; p50=11, p95~20.4
        for i, e in enumerate([6.0, 9.0, 11.0, 14.0, 22.0]):
            _record(run_id=f"s_p_{i}", edge=e)
        out = audit_cli.summary()
        assert "rows with edge : 5 / 5" in out
        assert "rows > floor   : 3" in out
        assert "edge p50/p95" in out
        assert "11.000" in out  # p50

    def test_rows_without_edge_count_separately(self, _isolated) -> None:
        _record(run_id="s_e", edge=12.0)
        _record(run_id="s_no", edge=None)
        out = audit_cli.summary()
        assert "rows with edge : 1 / 2" in out
        assert "rows > floor   : 1" in out

    def test_triage_distribution(self, _isolated) -> None:
        _record(run_id="s_t1", edge=12.0, triage="passed")
        _record(run_id="s_t2", edge=12.0, triage="killed", blocking_passed=False)
        _record(run_id="s_t3", edge=12.0, triage="sample_promising", blocking_passed=False)
        out = audit_cli.summary()
        assert "passed" in out
        assert "killed" in out
        assert "sample_promising" in out

    def test_filters_apply(self, _isolated) -> None:
        _record(run_id="s_m_str", strategy_type="maker", profile="vm_ul6_strict", edge=12.0)
        _record(run_id="s_t_str", strategy_type="taker", profile="vm_ul6_strict", edge=12.0)
        _record(run_id="s_m_loose", strategy_type="maker", profile="loose", edge=12.0)
        out = audit_cli.summary(strategy_type="maker", profile="vm_ul6_strict")
        assert "total rows     : 1" in out

    def test_main_dispatches_summary(self, _isolated, capsys) -> None:
        _record(run_id="s_main", edge=11.5)
        rc = audit_cli.main(["summary"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "audit summary" in captured
        assert "rows > floor   : 1" in captured
