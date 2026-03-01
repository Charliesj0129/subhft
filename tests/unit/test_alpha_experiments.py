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
    import json

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
