"""Round 81: leaderboard() ranks candidate runs by promotion-readiness
(驗證標準 §9 比較策略 / 知道策略保留/淘汰原因) — READY first, then fewest
failing axes, then triage precedence, then highest edge. Verdicts come from
promotion_readiness so ranking never drifts. Audit-layer only, no relaxed
thresholds."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    edge: float | None = 12.0,
    day_dom: float | None = 15.0,
    label: str | None = "adequate",
    worst_loss: float | None = None,
    strategy_type: str = "taker",
    profile_name: str = "vm_ul6_strict",
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
    if day_dom is not None:
        advisory.append(
            {
                "name": "single_day_dominance",
                "passed": day_dom <= 25.0,
                "metrics": {"top_day_contribution_pct": day_dom},
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
    if worst_loss is not None:
        advisory.append(
            {
                "name": "trade_concentration",
                "passed": worst_loss <= 50.0,
                "metrics": {"n_trades": 80.0, "worst_loss_share_pct": worst_loss},
                "details": "",
            }
        )
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument="TXFD6",
        strategy_type=strategy_type,
        profile_name=profile_name,
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


def _ranked_run_ids(out: str) -> list[str]:
    lines = out.split("\n")
    # Skip header + separator; stop before the trailing "(... READY ...)" line.
    return [ln.split()[1] for ln in lines[2:] if ln and not ln.startswith("(")]


class TestLeaderboard:
    def test_ready_sorts_before_not_ready(self, _isolated) -> None:
        _record(run_id="fail", edge=5.0)
        _record(run_id="ready", edge=12.0)
        out = audit_cli.leaderboard()
        order = _ranked_run_ids(out)
        assert order.index("ready") < order.index("fail")
        assert "(1 READY of 2 ranked)" in out

    def test_fewer_fails_ranks_higher_among_not_ready(self, _isolated) -> None:
        # one failing axis vs two failing axes
        _record(run_id="one_fail", edge=5.0)  # only edge fails
        _record(run_id="two_fail", edge=5.0, worst_loss=80.0)  # edge + worst_loss
        out = audit_cli.leaderboard()
        order = _ranked_run_ids(out)
        assert order.index("one_fail") < order.index("two_fail")

    def test_higher_edge_breaks_ties_among_ready(self, _isolated) -> None:
        _record(run_id="lo", edge=11.0)
        _record(run_id="hi", edge=40.0)
        out = audit_cli.leaderboard()
        order = _ranked_run_ids(out)
        assert order.index("hi") < order.index("lo")

    def test_ranking_consistent_with_promotion_readiness(self, _isolated) -> None:
        _record(run_id="r1", edge=12.0)
        out = audit_cli.leaderboard()
        line = [ln for ln in out.split("\n") if "r1" in ln][0]
        ready, _ = audit_cli.promotion_readiness(sub_gate_audit.read_runs("r1")[0])
        assert ("READY" in line) == ready

    def test_strategy_type_filter(self, _isolated) -> None:
        _record(run_id="mk", strategy_type="maker")
        _record(run_id="tk", strategy_type="taker")
        out = audit_cli.leaderboard(strategy_type="maker")
        assert "mk" in out
        assert "tk" not in out

    def test_no_rows_matches_filter(self, _isolated) -> None:
        _record(run_id="only")
        out = audit_cli.leaderboard(profile="other")
        assert out == "no audit rows match filter."
