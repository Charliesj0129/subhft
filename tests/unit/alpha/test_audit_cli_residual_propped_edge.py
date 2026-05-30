"""Round 89: `residual_propped_edge` flags a net-positive edge carried entirely
by the unrealized residual mark (驗證標準 §3 不得忽略殘倉提高 edge).  Threshold-free
derivation over the inventory_mtm realized/residual split; advisory only (no
invented cap, no promotion-blocker change).  Audit-layer scope."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    realized: float | None,
    residual: float | None,
    net: float | None,
) -> None:
    advisory: list[dict] = [
        {
            "name": "edge_per_round_trip",
            "passed": True,
            "metrics": {"mean_net_edge_pts_per_trade": 12.0},
            "details": "",
        }
    ]
    if net is not None:
        metrics: dict[str, float] = {"net_pts": net}
        if realized is not None:
            metrics["realized_pts"] = realized
        if residual is not None:
            metrics["residual_mtm_pts"] = residual
        advisory.append(
            {"name": "inventory_mtm", "passed": True, "metrics": metrics, "details": ""}
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


def _row(run_id: str) -> dict:
    return sub_gate_audit.read_runs(run_id)[0]


class TestRealizedPtsLifted:
    def test_realized_lifted_to_top_level(self, _isolated) -> None:
        _record(run_id="r", realized=8.0, residual=4.0, net=12.0)
        assert _row("r")["inventory_realized_pts"] == 8.0

    def test_absent_when_gate_missing(self, _isolated) -> None:
        _record(run_id="bare", realized=None, residual=None, net=None)
        assert "inventory_realized_pts" not in _row("bare")


class TestResidualProppedEdge:
    def test_na_when_gate_absent(self, _isolated) -> None:
        _record(run_id="bare", realized=None, residual=None, net=None)
        verdict, _detail = audit_cli.residual_propped_edge(_row("bare"))
        assert verdict == "n/a"

    def test_clean_when_realized_positive(self, _isolated) -> None:
        _record(run_id="ok", realized=8.0, residual=4.0, net=12.0)
        verdict, _detail = audit_cli.residual_propped_edge(_row("ok"))
        assert verdict == "clean"

    def test_propped_when_realized_nonpositive_and_net_positive(self, _isolated) -> None:
        # All of net comes from the unrealized residual mark.
        _record(run_id="bad", realized=-3.0, residual=15.0, net=12.0)
        verdict, detail = audit_cli.residual_propped_edge(_row("bad"))
        assert verdict == "propped"
        assert "residual mark" in detail

    def test_clean_when_net_nonpositive(self, _isolated) -> None:
        # net<=0 is not a §3 inflation case (residual isn't propping a win).
        _record(run_id="neg", realized=-5.0, residual=2.0, net=-3.0)
        verdict, _detail = audit_cli.residual_propped_edge(_row("neg"))
        assert verdict == "clean"

    def test_realized_zero_with_positive_net_is_propped(self, _isolated) -> None:
        _record(run_id="zero", realized=0.0, residual=12.0, net=12.0)
        verdict, _detail = audit_cli.residual_propped_edge(_row("zero"))
        assert verdict == "propped"


class TestShowSurfacesResidualPropped:
    def test_show_flags_propped(self, _isolated) -> None:
        _record(run_id="bad", realized=-3.0, residual=15.0, net=12.0)
        out = audit_cli.show("bad")
        assert "residual_propped: !! PROPPED" in out

    def test_show_marks_clean(self, _isolated) -> None:
        _record(run_id="ok", realized=8.0, residual=4.0, net=12.0)
        out = audit_cli.show("ok")
        assert "residual_propped: clean" in out

    def test_show_omits_line_when_gate_absent(self, _isolated) -> None:
        _record(run_id="bare", realized=None, residual=None, net=None)
        out = audit_cli.show("bare")
        assert "residual_propped" not in out
