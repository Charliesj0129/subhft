import json
from pathlib import Path

import numpy as np
import pytest

from hft_platform.alpha.experiments import ExperimentTracker


def test_experiment_tracker_log_and_list(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    meta_path = tracker.log_run(
        run_id="run-1",
        alpha_id="ofi_mc",
        config_hash="cfg1",
        data_paths=["/tmp/feed.npy"],
        metrics={"sharpe_oos": 1.2, "max_drawdown": -0.1},
        gate_status={"gate_c": True},
        scorecard_payload={"sharpe_oos": 1.2},
        backtest_report_payload={"gate": "Gate C", "passed": True},
        signals=np.array([0.1, 0.2, 0.3], dtype=np.float64),
        equity=np.array([100.0, 101.0, 102.0], dtype=np.float64),
    )
    assert meta_path.exists()

    rows = tracker.list_runs()
    assert len(rows) == 1
    assert rows[0].run_id == "run-1"
    assert rows[0].alpha_id == "ofi_mc"


def test_experiment_tracker_stamps_edge_metric_semantics(tmp_path: Path):
    from research.registry.schemas import Scorecard

    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    advisory = [
        {
            "name": "edge_per_round_trip",
            "passed": True,
            "metrics": {
                "mean_net_edge_pts_per_trade": 12.5,
                "n_trips": 300.0,
                "total_net_pts": 3750.0,
                "threshold_pts": 10.0,
            },
            "details": "mean_net_edge=12.50 pts/trade",
        }
    ]

    meta_path = tracker.log_run(
        run_id="run-edge",
        alpha_id="edge_alpha",
        config_hash="cfg-edge",
        data_paths=["research/data/processed/edge_alpha/day.npy"],
        metrics={"sharpe_oos": 1.4},
        gate_status={"gate_c": True},
        scorecard_payload={"sharpe_oos": 1.4},
        backtest_report_payload={
            "gate": "Gate C",
            "passed": True,
            "details": {"sub_gates_advisory": advisory},
        },
    )

    scorecard = json.loads((meta_path.parent / "scorecard.json").read_text())
    report = json.loads((meta_path.parent / "backtest_report.json").read_text())

    semantics = scorecard["edge_metric_semantics"]
    assert semantics["metric"] == "mean_net_edge_pts_per_trade"
    assert semantics["source_gate"] == "edge_per_round_trip"
    assert semantics["denominator"] == "completed_fifo_round_trips"
    assert semantics["costs_included"] is True
    assert semantics["residual_mtm_included"] is True
    assert semantics["force_flat_policy"] == "session_end_force_flat_last_mid"
    assert semantics["floor_operator"] == ">"
    assert semantics["floor_pts"] == 10.0
    assert report["details"]["edge_metric_semantics"] == semantics
    assert Scorecard.from_dict({"edge_metric_semantics": semantics}).edge_metric_semantics == semantics
    # Only the source gate ran here, so the label must NOT claim validation: the
    # six supporting gates are recorded as absent and ``validated`` is False.
    assert semantics["validated"] is False
    assert semantics["supporting_gates_status"]["edge_per_round_trip"] == "pass"
    assert semantics["supporting_gates_status"]["inventory_mtm"] == "absent"


_SUPPORTING_EDGE_GATES = (
    "inventory_mtm",
    "cost_uncertainty",
    "force_flat_residual",
    "min_sample_size",
    "single_day_dominance",
    "monthly_distribution",
)


def _edge_advisory(passed_by_gate: dict[str, bool]) -> list[dict[str, object]]:
    """Build a sub_gates_advisory list: source edge gate plus supporting gates."""
    advisory: list[dict[str, object]] = [
        {
            "name": "edge_per_round_trip",
            "passed": passed_by_gate.get("edge_per_round_trip", True),
            "metrics": {
                "mean_net_edge_pts_per_trade": 12.5,
                "n_trips": 300.0,
                "total_net_pts": 3750.0,
                "threshold_pts": 10.0,
            },
            "details": "mean_net_edge=12.50 pts/trade",
        }
    ]
    for gate in _SUPPORTING_EDGE_GATES:
        if gate in passed_by_gate:
            advisory.append({"name": gate, "passed": passed_by_gate[gate], "metrics": {}})
    return advisory


def test_edge_metric_semantics_validated_when_all_supporting_gates_pass(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    all_pass = {gate: True for gate in ("edge_per_round_trip", *_SUPPORTING_EDGE_GATES)}
    meta_path = tracker.log_run(
        run_id="run-edge-validated",
        alpha_id="edge_alpha",
        config_hash="cfg-validated",
        data_paths=["research/data/processed/edge_alpha/day.npy"],
        metrics={"mean_net_edge_pts_per_trade": 12.5},
        gate_status={"gate_c": True},
        scorecard_payload={"sharpe_oos": 1.4},
        backtest_report_payload={
            "gate": "Gate C",
            "passed": True,
            "details": {"sub_gates_advisory": _edge_advisory(all_pass)},
        },
    )

    scorecard = json.loads((meta_path.parent / "scorecard.json").read_text())
    semantics = scorecard["edge_metric_semantics"]
    assert semantics["validated"] is True
    assert all(status == "pass" for status in semantics["supporting_gates_status"].values())

    # A fully-validated edge is the only one allowed onto the trustworthy board.
    best = tracker.best_by_metric("mean_net_edge_pts_per_trade", n=5)
    assert [row["run_id"] for row in best] == ["run-edge-validated"]
    assert best[0]["edge_metric_semantics_status"] == "complete"


def test_edge_metric_semantics_unvalidated_when_supporting_gate_fails(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    gates = {gate: True for gate in ("edge_per_round_trip", *_SUPPORTING_EDGE_GATES)}
    gates["inventory_mtm"] = False  # residual-propped edge: supporting gate fails
    meta_path = tracker.log_run(
        run_id="run-edge-unvalidated",
        alpha_id="edge_alpha",
        config_hash="cfg-unvalidated",
        data_paths=["research/data/processed/edge_alpha/day.npy"],
        metrics={"mean_net_edge_pts_per_trade": 12.5},
        gate_status={"gate_c": False},
        scorecard_payload={"sharpe_oos": 1.4},
        backtest_report_payload={
            "gate": "Gate C",
            "passed": False,
            "details": {"sub_gates_advisory": _edge_advisory(gates)},
        },
    )

    scorecard = json.loads((meta_path.parent / "scorecard.json").read_text())
    semantics = scorecard["edge_metric_semantics"]
    assert semantics["validated"] is False
    assert semantics["supporting_gates_status"]["inventory_mtm"] == "fail"

    [row] = tracker.list_runs()
    [compared] = tracker.compare([row.run_id])
    assert compared["edge_metric_semantics_status"] == "gates_unvalidated"
    assert compared["edge_metric_semantics_failing_gates"] == ["inventory_mtm"]

    # An edge above the floor but with a failed supporting gate must NOT surface
    # as a trustworthy candidate.
    assert tracker.best_by_metric("mean_net_edge_pts_per_trade", n=5) == []


def test_experiment_tracker_compare_and_best(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    tracker.log_run(
        run_id="run-a",
        alpha_id="ofi_mc",
        config_hash="a",
        data_paths=["d1.npy"],
        metrics={"sharpe_oos": 0.8},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([0.1, 0.2], dtype=np.float64),
        equity=np.array([100.0, 101.0], dtype=np.float64),
    )
    tracker.log_run(
        run_id="run-b",
        alpha_id="ofi_mc",
        config_hash="b",
        data_paths=["d2.npy"],
        metrics={"sharpe_oos": 1.5},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([0.3, 0.4], dtype=np.float64),
        equity=np.array([100.0, 102.0], dtype=np.float64),
    )

    compared = tracker.compare(["run-b", "run-a"])
    assert [row["run_id"] for row in compared] == ["run-b", "run-a"]

    best = tracker.best_by_metric("sharpe_oos", n=1)
    assert len(best) == 1
    assert best[0]["run_id"] == "run-b"


def test_explicit_keep_downgraded_when_edge_unvalidated(tmp_path: Path):
    # Goal §3: an explicit keep may not override an unvalidated edge. Under a
    # non-strict profile inventory_mtm can fail as advisory while blocking passes.
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    gates = {gate: True for gate in ("edge_per_round_trip", *_SUPPORTING_EDGE_GATES)}
    gates["inventory_mtm"] = False
    meta_path = tracker.log_run(
        run_id="run-keep-blocked",
        alpha_id="edge_alpha",
        config_hash="cfg-keep-blocked",
        data_paths=["d.npy"],
        metrics={"mean_net_edge_pts_per_trade": 12.5},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={
            "gate": "Gate C",
            "passed": True,
            "details": {"sub_gates_advisory": _edge_advisory(gates)},
        },
        research_decision={
            "status": "keep",
            "reason": "researcher_keep",
            "evidence": ["manual_review"],
            "decided_by": "researcher",
        },
    )

    decision = json.loads(meta_path.read_text())["research_decision"]
    assert decision["status"] == "blocked_by_audit"
    assert decision["reason"] == "edge_metric_unvalidated:inventory_mtm"
    assert decision["decided_by"] == "edge_validation_guard"
    assert "inventory_mtm" in decision["evidence"]


def test_auto_keep_preserved_when_edge_validated(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    advisory = _edge_advisory({gate: True for gate in ("edge_per_round_trip", *_SUPPORTING_EDGE_GATES)})
    meta_path = tracker.log_run(
        run_id="run-keep-validated",
        alpha_id="edge_alpha",
        config_hash="cfg-keep-validated",
        data_paths=["d.npy"],
        metrics={"mean_net_edge_pts_per_trade": 12.5},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={
            "gate": "Gate C",
            "passed": True,
            "details": {
                "sub_gates_blocking": {"passed": True, "triage_status": "passed"},
                "sub_gates_advisory": advisory,
            },
        },
    )

    decision = json.loads(meta_path.read_text())["research_decision"]
    assert decision["status"] == "keep"
    assert decision["reason"] == "gate_c_blocking_passed"


def test_experiment_tracker_logs_replayable_research_decision(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    meta_path = tracker.log_run(
        run_id="run-sample-gated",
        alpha_id="edge_alpha",
        config_hash="cfg-sample",
        data_paths=["research/data/processed/edge_alpha/oos.npy"],
        metrics={"mean_net_edge_pts_per_trade": 12.5},
        gate_status={"gate_c": False},
        scorecard_payload={},
        backtest_report_payload={},
        research_decision={
            "status": "needs_more_sample",
            "reason": "oos_trading_days_below_minimum",
            "evidence": ["min_sample_size", "oos_day_count"],
            "decided_by": "gate_c",
        },
    )

    meta = json.loads(meta_path.read_text())
    assert meta["research_decision"] == {
        "status": "needs_more_sample",
        "reason": "oos_trading_days_below_minimum",
        "evidence": ["min_sample_size", "oos_day_count"],
        "decided_by": "gate_c",
    }

    [row] = tracker.list_runs()
    assert row.research_decision["status"] == "needs_more_sample"

    compared = tracker.compare(["run-sample-gated"])
    assert compared == [
        {
            "run_id": "run-sample-gated",
            "alpha_id": "edge_alpha",
            "config_hash": "cfg-sample",
            "timestamp": row.timestamp,
            "mean_net_edge_pts_per_trade": 12.5,
            "research_decision_status": "needs_more_sample",
            "research_decision_reason": "oos_trading_days_below_minimum",
            "research_decision_evidence": ["min_sample_size", "oos_day_count"],
            "research_decision": meta["research_decision"],
        }
    ]


def test_experiment_tracker_derives_research_decision_from_gate_c_blocking_sample(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    meta_path = tracker.log_run(
        run_id="run-auto-sample-gated",
        alpha_id="edge_alpha",
        config_hash="cfg-auto-sample",
        data_paths=["research/data/processed/edge_alpha/oos.npy"],
        metrics={"mean_net_edge_pts_per_trade": 12.5},
        gate_status={"gate_c": False},
        scorecard_payload={},
        backtest_report_payload={
            "gate": "Gate C",
            "passed": False,
            "details": {
                "sub_gates_blocking": {
                    "passed": False,
                    "triage_status": "sample_needs_more_sample",
                    "triage_reasons": ["min_sample_size"],
                    "failing": [
                        {
                            "name": "min_sample_size",
                            "metrics": {"sample_adequacy_label": "needs_more_sample"},
                        }
                    ],
                }
            },
        },
    )

    meta = json.loads(meta_path.read_text())
    assert meta["research_decision"] == {
        "status": "needs_more_sample",
        "reason": "gate_c_sample_needs_more_sample",
        "evidence": ["min_sample_size"],
        "decided_by": "gate_c",
    }

    [row] = tracker.list_runs()
    compared = tracker.compare(["run-auto-sample-gated"])
    assert compared == [
        {
            "run_id": "run-auto-sample-gated",
            "alpha_id": "edge_alpha",
            "config_hash": "cfg-auto-sample",
            "timestamp": row.timestamp,
            "mean_net_edge_pts_per_trade": 12.5,
            "research_decision_status": "needs_more_sample",
            "research_decision_reason": "gate_c_sample_needs_more_sample",
            "research_decision_evidence": ["min_sample_size"],
            "research_decision": meta["research_decision"],
        }
    ]


@pytest.mark.parametrize(
    ("gate_name", "expected_status", "expected_reason"),
    [
        ("replay_parity", "blocked_by_parity", "gate_c_parity_blocker:replay_parity"),
        ("monthly_distribution", "blocked_by_risk", "gate_c_risk_blocker:monthly_distribution"),
        ("cost_uncertainty", "blocked_by_audit", "gate_c_audit_blocker:cost_uncertainty"),
        ("inventory_mtm", "blocked_by_audit", "gate_c_audit_blocker:inventory_mtm"),
        ("edge_per_round_trip", "failed", "gate_c_blocking_failed:edge_per_round_trip"),
    ],
)
def test_experiment_tracker_derives_research_decision_from_gate_c_killed_blockers(
    tmp_path: Path,
    gate_name: str,
    expected_status: str,
    expected_reason: str,
):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    run_id = f"run-auto-killed-{gate_name}"
    meta_path = tracker.log_run(
        run_id=run_id,
        alpha_id="edge_alpha",
        config_hash="cfg-auto-killed",
        data_paths=["research/data/processed/edge_alpha/oos.npy"],
        metrics={},
        gate_status={"gate_c": False},
        scorecard_payload={},
        backtest_report_payload={
            "gate": "Gate C",
            "passed": False,
            "details": {
                "sub_gates_blocking": {
                    "passed": False,
                    "triage_status": "killed",
                    "triage_reasons": [gate_name],
                    "failing": [{"name": gate_name, "passed": False, "metrics": {}, "details": "strict fail"}],
                }
            },
        },
    )

    meta = json.loads(meta_path.read_text())
    assert meta["research_decision"] == {
        "status": expected_status,
        "reason": expected_reason,
        "evidence": [gate_name],
        "decided_by": "gate_c",
    }

    [compared] = tracker.compare([run_id])
    assert compared["research_decision_status"] == expected_status
    assert compared["research_decision_reason"] == expected_reason
    assert compared["research_decision_evidence"] == [gate_name]


def test_experiment_tracker_compare_marks_legacy_edge_semantics(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    advisory = _edge_advisory({gate: True for gate in ("edge_per_round_trip", *_SUPPORTING_EDGE_GATES)})
    tracker.log_run(
        run_id="run-complete-edge",
        alpha_id="edge_alpha",
        config_hash="complete",
        data_paths=["d1.npy"],
        metrics={"mean_net_edge_pts_per_trade": 12.5},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={"details": {"sub_gates_advisory": advisory}},
    )

    legacy_dir = tracker.runs_dir / "run-legacy-edge"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "scorecard.json").write_text(json.dumps({}), encoding="utf-8")
    (legacy_dir / "backtest_report.json").write_text(
        json.dumps({"details": {"sub_gates_advisory": advisory}}),
        encoding="utf-8",
    )
    (legacy_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "run-legacy-edge",
                "alpha_id": "edge_alpha",
                "config_hash": "legacy",
                "timestamp": "2026-06-02T00:00:00+00:00",
                "data_paths": ["d2.npy"],
                "metrics": {"mean_net_edge_pts_per_trade": 12.5},
                "gate_status": {"gate_c": True},
                "scorecard_path": str(legacy_dir / "scorecard.json"),
                "backtest_report_path": str(legacy_dir / "backtest_report.json"),
            }
        ),
        encoding="utf-8",
    )

    compared = tracker.compare(["run-complete-edge", "run-legacy-edge"])
    by_run = {row["run_id"]: row for row in compared}

    assert by_run["run-complete-edge"]["edge_metric_semantics_status"] == "complete"
    assert by_run["run-legacy-edge"]["edge_metric_semantics_status"] == "legacy_missing"
    assert by_run["run-legacy-edge"]["edge_metric_semantics_missing"] == [
        "scorecard.edge_metric_semantics",
        "report.edge_metric_semantics",
    ]


def test_experiment_tracker_best_by_edge_metric_requires_semantics(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    advisory = _edge_advisory({gate: True for gate in ("edge_per_round_trip", *_SUPPORTING_EDGE_GATES)})
    tracker.log_run(
        run_id="run-complete-edge",
        alpha_id="edge_alpha",
        config_hash="complete",
        data_paths=["d1.npy"],
        metrics={"mean_net_edge_pts_per_trade": 12.5},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={"details": {"sub_gates_advisory": advisory}},
    )

    legacy_dir = tracker.runs_dir / "run-legacy-edge"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "scorecard.json").write_text(json.dumps({}), encoding="utf-8")
    (legacy_dir / "backtest_report.json").write_text(
        json.dumps({"details": {"sub_gates_advisory": advisory}}),
        encoding="utf-8",
    )
    (legacy_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "run-legacy-edge",
                "alpha_id": "edge_alpha",
                "config_hash": "legacy",
                "timestamp": "2026-06-02T00:00:00+00:00",
                "data_paths": ["d2.npy"],
                "metrics": {"mean_net_edge_pts_per_trade": 50.0},
                "gate_status": {"gate_c": True},
                "scorecard_path": str(legacy_dir / "scorecard.json"),
                "backtest_report_path": str(legacy_dir / "backtest_report.json"),
            }
        ),
        encoding="utf-8",
    )

    best = tracker.best_by_metric("mean_net_edge_pts_per_trade", n=5)

    assert [row["run_id"] for row in best] == ["run-complete-edge"]
    assert best[0]["value"] == 12.5
    assert best[0]["edge_metric_semantics_status"] == "complete"


def test_experiment_tracker_latest_equity_and_proxy_returns(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    tracker.log_run(
        run_id="run-a",
        alpha_id="alpha_a",
        config_hash="a",
        data_paths=["d1.npy"],
        metrics={},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([0.1, 0.2, 0.3, 0.4], dtype=np.float64),
        equity=np.array([100.0, 101.0, 102.0, 103.0], dtype=np.float64),
    )
    tracker.log_run(
        run_id="run-b",
        alpha_id="alpha_b",
        config_hash="b",
        data_paths=["d2.npy"],
        metrics={},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([0.4, 0.3, 0.2, 0.1], dtype=np.float64),
        equity=np.array([100.0, 99.0, 99.5, 100.0], dtype=np.float64),
    )

    eq = tracker.latest_equity_by_alpha()
    assert sorted(eq) == ["alpha_a", "alpha_b"]
    returns = tracker.proxy_returns()
    assert returns is not None
    assert returns.size == 3


def test_experiment_tracker_paper_trade_summary(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    tracker.log_paper_trade_session(
        alpha_id="ofi_mc",
        started_at="2026-02-17T01:00:00+00:00",
        ended_at="2026-02-17T04:00:00+00:00",
        trading_day="2026-02-17",
        fills=10,
        pnl_bps=2.5,
        drift_alerts=0,
        execution_reject_rate=0.001,
    )
    tracker.log_paper_trade_session(
        alpha_id="ofi_mc",
        started_at="2026-02-24T01:00:00+00:00",
        ended_at="2026-02-24T03:30:00+00:00",
        trading_day="2026-02-24",
        fills=7,
        pnl_bps=-1.0,
        drift_alerts=1,
        execution_reject_rate=0.003,
    )

    summary = tracker.summarize_paper_trade("ofi_mc")
    assert summary["session_count"] == 2
    assert summary["distinct_trading_days"] == 2
    assert summary["calendar_span_days"] == 8
    assert summary["total_fills"] == 17
    assert summary["drift_alerts_total"] == 1
    assert summary["worst_daily_pnl_bps"] <= summary["mean_daily_pnl_bps"]
    assert summary["min_session_duration_seconds"] == 9000
    assert summary["max_session_duration_seconds"] == 10800
    assert summary["invalid_session_duration_count"] == 0


def test_experiment_tracker_paper_trade_rejects_negative_duration(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    with pytest.raises(ValueError):
        tracker.log_paper_trade_session(
            alpha_id="ofi_mc",
            started_at="2026-02-20T10:00:00+00:00",
            ended_at="2026-02-20T09:00:00+00:00",
            trading_day="2026-02-20",
        )


def test_paper_trade_session_defaults_to_one_hour_window(tmp_path: Path):
    import json

    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    path = tracker.log_paper_trade_session(alpha_id="ofi_mc", session_id="s1")
    payload = json.loads(path.read_text())
    assert payload["duration_seconds"] == 3600
    assert payload["session_duration_minutes"] == 60


def test_legacy_zero_duration_session_uses_fallback_duration(tmp_path: Path):
    import json

    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    session_path = tmp_path / "experiments" / "paper_trade" / "ofi_mc" / "sessions" / "2026-02-20_legacy.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text(
        json.dumps(
            {
                "alpha_id": "ofi_mc",
                "session_id": "legacy",
                "started_at": "2026-02-20T09:00:00+00:00",
                "ended_at": "2026-02-20T09:00:00+00:00",
                "trading_day": "2026-02-20",
                "fills": 0,
                "pnl_bps": 0.0,
                "drift_alerts": 0,
                "execution_reject_rate": 0.0,
                "notes": "",
            }
        )
    )

    summary = tracker.summarize_paper_trade("ofi_mc")
    assert summary["invalid_session_duration_count"] == 0
    assert summary["min_session_duration_seconds"] == 3600


# ---------------------------------------------------------------------------
# P0-A: session_duration_minutes convenience field
# ---------------------------------------------------------------------------


def test_paper_trade_session_duration_minutes_field(tmp_path: Path):
    """A 65-minute session yields session_duration_minutes == 65."""
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    path = tracker.log_paper_trade_session(
        alpha_id="test_alpha",
        started_at="2026-02-20T09:00:00+00:00",
        ended_at="2026-02-20T10:05:00+00:00",  # 65 minutes
        trading_day="2026-02-20",
    )
    import json

    payload = json.loads(path.read_text())
    assert payload["session_duration_minutes"] == 65


def test_paper_trade_session_duration_minutes_persists(tmp_path: Path):
    """JSON serialization of session includes session_duration_minutes field."""
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    path = tracker.log_paper_trade_session(
        alpha_id="test_alpha",
        started_at="2026-02-20T09:00:00+00:00",
        ended_at="2026-02-20T10:00:00+00:00",  # 60 minutes
        trading_day="2026-02-20",
    )
    import json

    payload = json.loads(path.read_text())
    assert "session_duration_minutes" in payload
    assert payload["session_duration_minutes"] == 60


def test_paper_trade_session_legacy_json_no_minutes_field(tmp_path: Path):
    """Legacy JSON without session_duration_minutes infers it from duration_seconds."""

    from hft_platform.alpha.experiments import _paper_session_from_dict

    payload = {
        "alpha_id": "test_alpha",
        "session_id": "abc123",
        "started_at": "2026-02-20T09:00:00+00:00",
        "ended_at": "2026-02-20T10:30:00+00:00",
        "duration_seconds": 5400,  # 90 minutes; no session_duration_minutes key
        "trading_day": "2026-02-20",
        "fills": 0,
        "pnl_bps": 0.0,
        "drift_alerts": 0,
        "execution_reject_rate": 0.0,
        "notes": "",
    }
    session = _paper_session_from_dict(payload, alpha_id="test_alpha")
    assert session.session_duration_minutes == 90


# ---------------------------------------------------------------------------
# P1: reject_rate_p95 in PaperTradeSession
# ---------------------------------------------------------------------------


def test_paper_trade_session_reject_rate_p95_persists(tmp_path: Path):
    """log_paper_trade_session stores reject_rate_p95 in JSON and round-trips."""
    import json

    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    path = tracker.log_paper_trade_session(
        alpha_id="test_alpha",
        started_at="2026-03-01T09:00:00+00:00",
        ended_at="2026-03-01T10:00:00+00:00",
        execution_reject_rate=0.005,
        reject_rate_p95=0.012,
    )
    payload = json.loads(path.read_text())
    assert payload["reject_rate_p95"] == pytest.approx(0.012)


def test_paper_trade_session_reject_rate_p95_defaults_none(tmp_path: Path):
    """log_paper_trade_session without reject_rate_p95 stores None."""
    import json

    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    path = tracker.log_paper_trade_session(
        alpha_id="test_alpha",
        started_at="2026-03-01T09:00:00+00:00",
        ended_at="2026-03-01T10:00:00+00:00",
        execution_reject_rate=0.005,
    )
    payload = json.loads(path.read_text())
    assert payload["reject_rate_p95"] is None


def test_summarize_paper_trade_includes_p95(tmp_path: Path):
    """summarize_paper_trade emits execution_reject_rate_p95 when sessions have it."""
    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    for i in range(3):
        tracker.log_paper_trade_session(
            alpha_id="test_alpha",
            started_at=f"2026-03-0{i + 1}T09:00:00+00:00",
            ended_at=f"2026-03-0{i + 1}T10:00:00+00:00",
            execution_reject_rate=0.005,
            reject_rate_p95=0.01 * (i + 1),  # 0.01, 0.02, 0.03
        )
    summary = tracker.summarize_paper_trade("test_alpha")
    assert summary["execution_reject_rate_p95"] is not None
    # P95 of [0.01, 0.02, 0.03] ≈ 0.03
    assert summary["execution_reject_rate_p95"] > 0.02


class TestExperimentGC:
    def test_gc_dry_run(self, tmp_path: Path) -> None:
        import json as _json

        from hft_platform.alpha.experiments import gc_experiment_runs

        tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
        tracker.log_run(
            run_id="old-run",
            alpha_id="alpha_old",
            config_hash="h",
            data_paths=["d.npy"],
            metrics={"sharpe_oos": 0.5},
            gate_status={"gate_c": False},
            scorecard_payload={},
            backtest_report_payload={},
        )
        meta_path = tmp_path / "experiments" / "runs" / "old-run" / "meta.json"
        meta = _json.loads(meta_path.read_text())
        meta["timestamp"] = "2025-01-01T00:00:00+00:00"
        meta_path.write_text(_json.dumps(meta))
        result = gc_experiment_runs(base_dir=str(tmp_path / "experiments"), older_than_days=30, apply=False)
        assert result["candidates"] == 1
        assert result["deleted"] == 0
        assert (tmp_path / "experiments" / "runs" / "old-run").exists()

    def test_gc_apply(self, tmp_path: Path) -> None:
        import json as _json

        from hft_platform.alpha.experiments import gc_experiment_runs

        tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
        tracker.log_run(
            run_id="old-run",
            alpha_id="alpha_old",
            config_hash="h",
            data_paths=["d.npy"],
            metrics={"sharpe_oos": 0.5},
            gate_status={"gate_c": False},
            scorecard_payload={},
            backtest_report_payload={},
        )
        meta_path = tmp_path / "experiments" / "runs" / "old-run" / "meta.json"
        meta = _json.loads(meta_path.read_text())
        meta["timestamp"] = "2025-01-01T00:00:00+00:00"
        meta_path.write_text(_json.dumps(meta))
        result = gc_experiment_runs(base_dir=str(tmp_path / "experiments"), older_than_days=30, apply=True)
        assert result["deleted"] == 1
        assert result["freed_bytes"] > 0
        assert not (tmp_path / "experiments" / "runs" / "old-run").exists()

    def test_gc_preserves_gate_c_pass(self, tmp_path: Path) -> None:
        import json as _json

        from hft_platform.alpha.experiments import gc_experiment_runs

        tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
        tracker.log_run(
            run_id="good-run",
            alpha_id="alpha_good",
            config_hash="h",
            data_paths=["d.npy"],
            metrics={"sharpe_oos": 1.5},
            gate_status={"gate_c": True},
            scorecard_payload={},
            backtest_report_payload={},
        )
        meta_path = tmp_path / "experiments" / "runs" / "good-run" / "meta.json"
        meta = _json.loads(meta_path.read_text())
        meta["timestamp"] = "2025-01-01T00:00:00+00:00"
        meta_path.write_text(_json.dumps(meta))
        result = gc_experiment_runs(base_dir=str(tmp_path / "experiments"), older_than_days=30, apply=True)
        assert result["preserved"] == 1
        assert result["deleted"] == 0
        assert (tmp_path / "experiments" / "runs" / "good-run").exists()

    def test_gc_recent_runs_not_eligible(self, tmp_path: Path) -> None:
        from hft_platform.alpha.experiments import gc_experiment_runs

        tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
        tracker.log_run(
            run_id="recent-run",
            alpha_id="alpha_recent",
            config_hash="h",
            data_paths=["d.npy"],
            metrics={"sharpe_oos": 0.5},
            gate_status={"gate_c": False},
            scorecard_payload={},
            backtest_report_payload={},
        )
        result = gc_experiment_runs(base_dir=str(tmp_path / "experiments"), older_than_days=30, apply=True)
        assert result["candidates"] == 0
        assert result["deleted"] == 0
