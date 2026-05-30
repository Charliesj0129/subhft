"""Round 86: `audit decision-trail` replays one strategy's run history in
decision order (驗證標準 §9 回放研究決策).  Verdicts come from promotion_readiness /
triage_reason so the trail never drifts from the live kept/killed call.
Read-only; audit-layer scope."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    strategy_name: str,
    recorded_at_ns: int,
    edge: float = 12.0,
    n_trades: float = 80.0,
    profile: str = "vm_ul6_strict",
) -> None:
    # A full gate set so promotion_readiness yields a clean verdict:
    # edge>10 + adequate sample -> promotable; edge<=10 -> failed; thin -> not.
    sample = "adequate" if n_trades >= 30.0 else "thin"
    advisory = [
        {
            "name": "edge_per_round_trip",
            "passed": edge > 10.0,
            "metrics": {"mean_net_edge_pts_per_trade": edge},
            "details": "",
        },
        {
            "name": "force_flat_residual",
            "passed": True,
            "metrics": {"force_flat_trip_share_pct": 10.0},
            "details": "",
        },
        {
            "name": "single_day_dominance",
            "passed": True,
            "metrics": {"top_day_contribution_pct": 15.0},
            "details": "",
        },
        {
            "name": "min_sample_size",
            "passed": n_trades >= 30.0,
            "metrics": {
                "sample_adequacy_label": sample,
                "n_fills": n_trades,
                "min_fills": 30.0,
                "n_days": 25.0,
                "min_days": 20.0,
            },
            "details": "",
        },
        {
            "name": "monthly_distribution",
            "passed": True,
            "metrics": {
                "drawdown_to_avg_monthly_ratio": 1.5,
                "top_month_contribution_pct": 40.0,
                "median_monthly_net_pnl_pts": 30.0,
                "worst_monthly_pnl_pts": 5.0,
            },
            "details": "",
        },
        {
            "name": "trade_concentration",
            "passed": True,
            "metrics": {
                "n_trades": 80.0,
                "worst_loss_share_pct": 30.0,
                "top_trade_share_pct": 20.0,
            },
            "details": "",
        },
        {
            "name": "replay_parity",
            "passed": True,
            "metrics": {"match_pct": 99.0, "threshold": 95.0},
            "details": "",
        },
    ]
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=strategy_name,
        instrument="TXFD6",
        strategy_type="taker",
        profile_name=profile,
        advisory=advisory,
        blocking={"passed": True, "failing": [], "triage_status": "passed"},
        recorded_at_ns=recorded_at_ns,
    )


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestDecisionTrail:
    def test_empty_when_no_matching_strategy(self, _isolated) -> None:
        _record(run_id="r1", strategy_name="alpha_a", recorded_at_ns=1, edge=12.0)
        out = audit_cli.decision_trail("alpha_missing")
        assert "no audit rows for strategy_name='alpha_missing'" in out

    def test_orders_by_recorded_at_ns_ascending(self, _isolated) -> None:
        # Insert out of chronological order; trail must sort by timestamp.
        _record(run_id="late", strategy_name="alpha_a", recorded_at_ns=300, edge=12.0)
        _record(run_id="early", strategy_name="alpha_a", recorded_at_ns=100, edge=8.0)
        _record(run_id="mid", strategy_name="alpha_a", recorded_at_ns=200, edge=11.0)
        out = audit_cli.decision_trail("alpha_a")
        # Body row order = early, mid, late.
        i_early = out.index("early")
        i_mid = out.index("mid")
        i_late = out.index("late")
        assert i_early < i_mid < i_late
        # Sequence numbers present.
        assert "1  " in out and "2  " in out and "3  " in out

    def test_filters_to_named_strategy_only(self, _isolated) -> None:
        _record(run_id="a1", strategy_name="alpha_a", recorded_at_ns=1, edge=12.0)
        _record(run_id="b1", strategy_name="alpha_b", recorded_at_ns=2, edge=12.0)
        out = audit_cli.decision_trail("alpha_a")
        assert "a1" in out
        assert "b1" not in out
        assert "(1 runs;" in out

    def test_profile_filter(self, _isolated) -> None:
        _record(
            run_id="p1", strategy_name="alpha_a", recorded_at_ns=1, edge=12.0, profile="vm_ul6_strict"
        )
        _record(
            run_id="p2", strategy_name="alpha_a", recorded_at_ns=2, edge=12.0, profile="other"
        )
        out = audit_cli.decision_trail("alpha_a", profile="vm_ul6_strict")
        assert "p1" in out
        assert "p2" not in out

    def test_trailing_line_flags_triage_change(self, _isolated) -> None:
        # Below-floor edge first (failed), then above-floor edge (promotable).
        _record(run_id="v1", strategy_name="alpha_a", recorded_at_ns=1, edge=8.0)
        _record(run_id="v2", strategy_name="alpha_a", recorded_at_ns=2, edge=12.0)
        out = audit_cli.decision_trail("alpha_a")
        assert "triage changed across trail" in out
        # Latest verdict is the last (chronological) run's triage.
        last_reason = audit_cli.triage_reason(sub_gate_audit.read_runs("v2")[0])
        assert f"latest triage={last_reason}" in out

    def test_trailing_line_flags_stable_triage(self, _isolated) -> None:
        _record(run_id="s1", strategy_name="alpha_a", recorded_at_ns=1, edge=12.0)
        _record(run_id="s2", strategy_name="alpha_a", recorded_at_ns=2, edge=12.0)
        out = audit_cli.decision_trail("alpha_a")
        assert "triage stable across trail" in out

    def test_verdict_sourced_from_promotion_readiness(self, _isolated) -> None:
        _record(run_id="r1", strategy_name="alpha_a", recorded_at_ns=1, edge=12.0)
        row = sub_gate_audit.read_runs("r1")[0]
        ready, _ = audit_cli.promotion_readiness(row)
        out = audit_cli.decision_trail("alpha_a")
        assert ("READY" if ready else "no") in out

    def test_cli_dispatch(self, _isolated, capsys) -> None:
        _record(run_id="r1", strategy_name="alpha_a", recorded_at_ns=1, edge=12.0)
        rc = audit_cli.main(["decision-trail", "alpha_a"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "decision trail: alpha_a" in captured
        assert "r1" in captured
