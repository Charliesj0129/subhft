"""Round 66: residual_mtm_pts + inventory_net_pts (驗證標準 §2/§3 — net edge
must fold in residual mark-to-market; un-FIFO'd inventory cannot be dropped to
inflate edge) lifted from the inventory_mtm gate to top-level row fields and
surfaced in `audit show`. Audit-layer only, no relaxed thresholds."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    residual_mtm: float | None,
    net_pts: float | None = None,
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
    if residual_mtm is not None:
        metrics: dict[str, float] = {
            "realized_pts": (net_pts - residual_mtm) if net_pts is not None else 0.0,
            "residual_mtm_pts": residual_mtm,
        }
        if net_pts is not None:
            metrics["net_pts"] = net_pts
        advisory.append(
            {
                "name": "inventory_mtm",
                "passed": True,
                "metrics": metrics,
                "details": "",
                "error": False,
            }
        )
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument="TXFD6",
        strategy_type="maker",
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


class TestBuildRecordLiftsResidualMtM:
    def test_residual_and_net_lifted(self, _isolated) -> None:
        _record(run_id="a", residual_mtm=20.0, net_pts=100.0)
        rows = sub_gate_audit.read_runs()
        assert rows[0]["residual_mtm_pts"] == 20.0
        assert rows[0]["inventory_net_pts"] == 100.0

    def test_absent_when_gate_didnt_run(self, _isolated) -> None:
        _record(run_id="b", residual_mtm=None)
        rows = sub_gate_audit.read_runs()
        assert "residual_mtm_pts" not in rows[0]
        assert "inventory_net_pts" not in rows[0]


class TestShowSurfacesResidualMtM:
    def test_shows_residual_share_of_net(self, _isolated) -> None:
        _record(run_id="s", residual_mtm=20.0, net_pts=100.0)
        out = audit_cli.show("s")
        line = out.split("residual_mtm")[1].split("\n")[0]
        assert "20.0 pts" in line
        assert "20% of net" in line

    def test_net_na_when_net_zero(self, _isolated) -> None:
        _record(run_id="z", residual_mtm=20.0, net_pts=0.0)
        out = audit_cli.show("z")
        line = out.split("residual_mtm")[1].split("\n")[0]
        assert "net n/a" in line

    def test_na_when_missing(self, _isolated) -> None:
        _record(run_id="na", residual_mtm=None)
        out = audit_cli.show("na")
        assert "residual_mtm   : (n/a" in out
