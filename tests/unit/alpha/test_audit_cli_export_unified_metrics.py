"""Round 84: unified-metrics export parity (完成狀態 §9 輸出統一 metrics) — the
remaining surfaced credibility axes (§6 drawdown/top-month, §5 worst-loss,
§7/§8 replay, §6 monthly_stability verdict) travel as export columns so an
external pivot has the same signal set as scorecard/show. Audit-layer only."""

from __future__ import annotations

import csv
import io

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    edge: float = 12.0,
    dd_ratio: float | None = None,
    top_month: float | None = None,
    median_month: float | None = None,
    worst_month: float | None = None,
    worst_loss: float | None = None,
    replay_match: float | None = None,
    replay_category: str | None = None,
) -> None:
    advisory: list[dict] = [
        {
            "name": "edge_per_round_trip",
            "passed": edge > 10.0,
            "metrics": {"mean_net_edge_pts_per_trade": edge},
            "details": "",
        }
    ]
    mdist: dict[str, float] = {}
    if dd_ratio is not None:
        mdist["drawdown_to_avg_monthly_ratio"] = dd_ratio
    if top_month is not None:
        mdist["top_month_contribution_pct"] = top_month
    if median_month is not None:
        mdist["median_monthly_net_pnl_pts"] = median_month
    if worst_month is not None:
        mdist["worst_monthly_pnl_pts"] = worst_month
    if mdist:
        advisory.append(
            {"name": "monthly_distribution", "passed": True, "metrics": mdist, "details": ""}
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
    if replay_match is not None:
        rm: dict[str, object] = {"match_pct": replay_match, "threshold": 95.0}
        if replay_category is not None:
            rm["dominant_divergence_category"] = replay_category
        advisory.append(
            {
                "name": "replay_parity",
                "passed": replay_match >= 95.0,
                "metrics": rm,
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
        blocking={"passed": True, "failing": [], "triage_status": "passed"},
        recorded_at_ns=1,
    )


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


def _csv_rows(out: str) -> dict[str, dict[str, str]]:
    reader = csv.DictReader(io.StringIO(out))
    return {r["run_id"]: r for r in reader}, reader.fieldnames  # type: ignore[return-value]


class TestUnifiedMetricsColumns:
    def test_new_columns_in_header(self, _isolated) -> None:
        _record(run_id="h")
        _rows, fieldnames = _csv_rows(audit_cli.export("csv"))
        for col in (
            "drawdown_to_avg_monthly_ratio",
            "top_month_contribution_pct",
            "worst_loss_share_pct",
            "replay_match_pct",
            "replay_divergence_category",
            "monthly_stability",
        ):
            assert col in fieldnames

    def test_values_populated_when_gates_ran(self, _isolated) -> None:
        _record(
            run_id="full",
            dd_ratio=1.5,
            top_month=42.0,
            median_month=30.0,
            worst_month=5.0,
            worst_loss=33.0,
            replay_match=80.0,
            replay_category="latency_shift",
        )
        rows, _ = _csv_rows(audit_cli.export("csv"))
        r = rows["full"]
        assert r["drawdown_to_avg_monthly_ratio"] == "1.5000"
        assert r["top_month_contribution_pct"] == "42.0000"
        assert r["worst_loss_share_pct"] == "33.0000"
        assert r["replay_match_pct"] == "80.0000"
        assert r["replay_divergence_category"] == "latency_shift"
        assert r["monthly_stability"] == "stable"

    def test_empty_when_gates_absent(self, _isolated) -> None:
        _record(run_id="bare")  # only edge gate
        rows, _ = _csv_rows(audit_cli.export("csv"))
        r = rows["bare"]
        assert r["drawdown_to_avg_monthly_ratio"] == ""
        assert r["worst_loss_share_pct"] == ""
        assert r["replay_match_pct"] == ""
        assert r["replay_divergence_category"] == ""
        # monthly_stability with no monthly metrics -> "unknown"
        assert r["monthly_stability"] == "unknown"

    def test_drawdown_inf_serialized(self, _isolated) -> None:
        _record(run_id="infdd", dd_ratio=float("inf"))
        rows, _ = _csv_rows(audit_cli.export("csv"))
        assert rows["infdd"]["drawdown_to_avg_monthly_ratio"] == "inf"

    def test_unstable_monthly_verdict_exported(self, _isolated) -> None:
        _record(run_id="uns", worst_month=-12.0)
        rows, _ = _csv_rows(audit_cli.export("csv"))
        assert rows["uns"]["monthly_stability"] == "unstable"
