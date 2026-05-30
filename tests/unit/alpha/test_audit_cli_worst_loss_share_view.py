"""Round 60: worst_loss_share_pct (驗證標準 §5 損益分布/虧損分布/是否被少數
交易支配) lifted from the trade_concentration gate to a top-level row field
and surfaced in `audit show`. The trade-level analogue of single-day /
single-month dominance. Audit-layer only, no relaxed thresholds."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    worst_loss: float | None,
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


class TestBuildRecordLiftsWorstLossShare:
    def test_share_lifted_to_top_level(self, _isolated) -> None:
        _record(run_id="a", worst_loss=35.0)
        rows = sub_gate_audit.read_runs()
        assert rows[0]["worst_loss_share_pct"] == 35.0

    def test_absent_when_gate_didnt_run(self, _isolated) -> None:
        _record(run_id="b", worst_loss=None)
        rows = sub_gate_audit.read_runs()
        assert "worst_loss_share_pct" not in rows[0]


class TestShowSurfacesWorstLossShare:
    def test_within_cap_reads_pass(self, _isolated) -> None:
        _record(run_id="s_ok", worst_loss=35.0)
        out = audit_cli.show("s_ok")
        line = out.split("worst_loss_share")[1].split("\n")[0]
        assert "35.0%" in line
        assert "PASS" in line

    def test_over_cap_reads_fail(self, _isolated) -> None:
        _record(run_id="s_bad", worst_loss=80.0)
        out = audit_cli.show("s_bad")
        line = out.split("worst_loss_share")[1].split("\n")[0]
        assert "80.0%" in line
        assert "FAIL" in line

    def test_exactly_cap_reads_pass(self, _isolated) -> None:
        _record(run_id="s_edge", worst_loss=50.0)
        out = audit_cli.show("s_edge")
        line = out.split("worst_loss_share")[1].split("\n")[0]
        assert "PASS" in line

    def test_na_when_missing(self, _isolated) -> None:
        _record(run_id="s_na", worst_loss=None)
        out = audit_cli.show("s_na")
        assert "worst_loss_share: (n/a" in out
