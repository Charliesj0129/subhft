"""Round 74: loss_concentration_offenders() names the run_ids whose loss
distribution is dominated by a few large losses (worst_loss_share_pct over the
strict 50.0% cap), mirroring force_flat_offenders for 驗證標準 §5 (虧損分布/
是否被少數交易支配). Audit-layer only, no relaxed thresholds."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    worst_loss: float | None,
    strategy_type: str = "taker",
    profile_name: str = "vm_ul6_strict",
    edge: float | None = 12.0,
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
    if worst_loss is not None:
        advisory.append(
            {
                "name": "trade_concentration",
                "passed": worst_loss <= 50.0,
                "metrics": {
                    "n_trades": 80.0,
                    "worst_loss_share_pct": worst_loss,
                    "worst_loss_share_max_pct": 50.0,
                    "top_trade_share_pct": 20.0,
                },
                "details": "",
                "error": False,
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


class TestLossConcentrationOffenders:
    def test_lists_only_rows_over_cap_sorted_desc(self, _isolated) -> None:
        _record(run_id="bad", worst_loss=72.0)
        _record(run_id="worst", worst_loss=88.0)
        _record(run_id="ok", worst_loss=40.0)  # under cap → excluded
        out = audit_cli.loss_concentration_offenders()
        body = out.split("\n")
        # First data row is the most concentrated (88%), then 72%.
        assert body[2].startswith("worst")
        assert "88.0%" in body[2]
        assert body[3].startswith("bad")
        assert "72.0%" in body[3]
        assert "ok" not in out
        assert "(2 over cap 50.0%" in out

    def test_exactly_at_cap_not_an_offender(self, _isolated) -> None:
        _record(run_id="edge", worst_loss=50.0)  # strict > cap, 50.0 excluded
        out = audit_cli.loss_concentration_offenders()
        assert "no rows over worst-loss-share cap" in out

    def test_missing_metric_skipped(self, _isolated) -> None:
        _record(run_id="nogate", worst_loss=None)
        out = audit_cli.loss_concentration_offenders()
        assert "no rows over worst-loss-share cap" in out

    def test_strategy_type_filter(self, _isolated) -> None:
        _record(run_id="mk", worst_loss=80.0, strategy_type="maker")
        _record(run_id="tk", worst_loss=80.0, strategy_type="taker")
        out = audit_cli.loss_concentration_offenders(strategy_type="maker")
        assert "mk" in out
        assert "tk" not in out

    def test_custom_min_share(self, _isolated) -> None:
        _record(run_id="mid", worst_loss=60.0)
        # Raise threshold above the row → no offenders.
        out = audit_cli.loss_concentration_offenders(min_share=70.0)
        assert "no rows over worst-loss-share cap (70.0%" in out

    def test_no_rows_matches_filter(self, _isolated) -> None:
        _record(run_id="only", worst_loss=80.0, profile_name="vm_ul6_strict")
        out = audit_cli.loss_concentration_offenders(profile="other_profile")
        assert out == "no audit rows match filter."
