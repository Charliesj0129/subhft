"""Coverage tests for hft_platform.alpha.experiments — missing line ranges.

Targets: corrupt meta, list_runs filter, compare missing, best_by_metric skip,
latest_signals/equity paths, proxy_returns edge cases, paper-trade sessions
(corrupt, legacy zero-duration, duration parsing), summarize_paper_trade,
session window resolution, gc_experiment_runs, and helper functions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from hft_platform.alpha.experiments import (
    ExperimentTracker,
    PaperTradeSession,
    _coerce_utc,
    _is_legacy_zero_duration_session,
    _parse_day,
    _session_duration_seconds,
    _trading_day_from_iso,
    gc_experiment_runs,
)


# ---------------------------------------------------------------------------
# list_runs: corrupt meta files are skipped (lines 137-139, 141)
# ---------------------------------------------------------------------------


def test_list_runs_skips_corrupt_meta(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    runs_dir = tracker.runs_dir / "run-bad"
    runs_dir.mkdir(parents=True)
    (runs_dir / "meta.json").write_text("NOT VALID JSON {{")

    rows = tracker.list_runs()
    assert rows == []


def test_list_runs_filters_by_alpha_id(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="run-a",
        alpha_id="alpha_x",
        config_hash="h1",
        data_paths=[],
        metrics={"sharpe": 1.0},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
    )
    tracker.log_run(
        run_id="run-b",
        alpha_id="alpha_y",
        config_hash="h2",
        data_paths=[],
        metrics={"sharpe": 2.0},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
    )
    rows = tracker.list_runs(alpha_id="alpha_x")
    assert len(rows) == 1
    assert rows[0].alpha_id == "alpha_x"


# ---------------------------------------------------------------------------
# compare: run_id not in target set is skipped (line 151)
# ---------------------------------------------------------------------------


def test_compare_skips_unknown_run_ids(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="r1",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={"m": 1.0},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
    )
    result = tracker.compare(["nonexistent"])
    assert result == []


# ---------------------------------------------------------------------------
# best_by_metric: metric missing from run (line 174)
# ---------------------------------------------------------------------------


def test_best_by_metric_skips_missing_metric(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="r1",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={"other_metric": 1.0},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
    )
    best = tracker.best_by_metric("sharpe_oos")
    assert best == []


# ---------------------------------------------------------------------------
# latest_signals/equity: no signals_path (lines 198, 201, 218, 225)
# ---------------------------------------------------------------------------


def test_latest_signals_by_alpha_no_signals_path(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="r1",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
    )
    result = tracker.latest_signals_by_alpha()
    assert result == {}


def test_latest_equity_by_alpha_no_equity_path(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="r1",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
    )
    result = tracker.latest_equity_by_alpha()
    assert result == {}


def test_latest_signals_returns_none_for_missing_file(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="r1",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
        signals=np.array([1.0, 2.0]),
    )
    # Delete the signals file to trigger _load_numpy returning None (line 415)
    signals_file = tracker.runs_dir / "r1" / "signals.npy"
    signals_file.unlink()
    result = tracker.latest_signals_by_alpha()
    assert result == {}


def test_latest_equity_returns_none_for_corrupt_file(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="r1",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
        equity=np.array([100.0, 101.0]),
    )
    # Corrupt the equity file (line 418-420)
    eq_file = tracker.runs_dir / "r1" / "equity.npy"
    eq_file.write_bytes(b"NOT A NPY FILE")
    result = tracker.latest_equity_by_alpha()
    assert result == {}


# ---------------------------------------------------------------------------
# proxy_returns: empty, single-element equity, min_len<2 (lines 224-225, 231, 237-238)
# ---------------------------------------------------------------------------


def test_proxy_returns_empty_equities(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    result = tracker.proxy_returns()
    assert result is None


def test_proxy_returns_single_element_equity(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="r1",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
        equity=np.array([100.0]),
    )
    result = tracker.proxy_returns()
    assert result is None


# ---------------------------------------------------------------------------
# Paper trade sessions: corrupt session files (lines 306-312)
# ---------------------------------------------------------------------------


def test_list_paper_trade_sessions_skips_corrupt(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    sessions_dir = tracker.paper_trade_dir / "alpha_x" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "2026-01-01_abc.json").write_text("{{INVALID JSON")

    rows = tracker.list_paper_trade_sessions("alpha_x")
    assert rows == []


# ---------------------------------------------------------------------------
# Paper trade session: duration_seconds parsing (lines 436-437, 443-444)
# ---------------------------------------------------------------------------


def test_paper_session_from_dict_invalid_duration_seconds(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    sessions_dir = tracker.paper_trade_dir / "alpha_x" / "sessions"
    sessions_dir.mkdir(parents=True)
    payload = {
        "alpha_id": "alpha_x",
        "session_id": "s1",
        "started_at": "2026-01-01T09:00:00+00:00",
        "ended_at": "2026-01-01T10:00:00+00:00",
        "duration_seconds": "not_a_number",
        "trading_day": "2026-01-01",
        "fills": 5,
        "pnl_bps": 1.0,
        "drift_alerts": 0,
        "execution_reject_rate": 0.01,
    }
    (sessions_dir / "2026-01-01_s1.json").write_text(json.dumps(payload))
    rows = tracker.list_paper_trade_sessions("alpha_x")
    assert len(rows) == 1
    assert rows[0].duration_seconds == 3600


def test_paper_session_from_dict_invalid_session_duration_minutes(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    sessions_dir = tracker.paper_trade_dir / "alpha_x" / "sessions"
    sessions_dir.mkdir(parents=True)
    payload = {
        "alpha_id": "alpha_x",
        "session_id": "s1",
        "started_at": "2026-01-01T09:00:00+00:00",
        "ended_at": "2026-01-01T10:00:00+00:00",
        "duration_seconds": 3600,
        "trading_day": "2026-01-01",
        "fills": 5,
        "pnl_bps": 1.0,
        "drift_alerts": 0,
        "execution_reject_rate": 0.01,
        "session_duration_minutes": "bad_value",
    }
    (sessions_dir / "2026-01-01_s1.json").write_text(json.dumps(payload))
    rows = tracker.list_paper_trade_sessions("alpha_x")
    assert len(rows) == 1
    assert rows[0].session_duration_minutes == 60


# ---------------------------------------------------------------------------
# Paper trade: legacy zero-duration fallback (lines 468-469, 475-476, 481)
# ---------------------------------------------------------------------------


def test_legacy_zero_duration_session_fallback(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    sessions_dir = tracker.paper_trade_dir / "alpha_x" / "sessions"
    sessions_dir.mkdir(parents=True)
    payload = {
        "alpha_id": "alpha_x",
        "session_id": "s1",
        "started_at": "2026-01-01T09:00:00+00:00",
        "ended_at": "2026-01-01T09:00:00+00:00",
        "trading_day": "2026-01-01",
        "fills": 0,
        "pnl_bps": 0.0,
        "drift_alerts": 0,
        "execution_reject_rate": 0.0,
        # No duration_seconds, no session_duration_minutes -> legacy zero
    }
    (sessions_dir / "2026-01-01_s1.json").write_text(json.dumps(payload))
    rows = tracker.list_paper_trade_sessions("alpha_x")
    assert len(rows) == 1
    assert rows[0].duration_seconds == 3600  # 60 min fallback


# ---------------------------------------------------------------------------
# summarize_paper_trade: with sessions (lines 355-356, 361, 375-387)
# ---------------------------------------------------------------------------


def test_summarize_paper_trade_with_sessions(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_paper_trade_session(
        alpha_id="a1",
        started_at="2026-01-01T09:00:00+00:00",
        ended_at="2026-01-01T10:00:00+00:00",
        trading_day="2026-01-01",
        fills=10,
        pnl_bps=2.5,
        reject_rate_p95=0.02,
        regime="trending",
        session_id="s1",
    )
    tracker.log_paper_trade_session(
        alpha_id="a1",
        started_at="2026-01-02T09:00:00+00:00",
        ended_at="2026-01-02T10:30:00+00:00",
        trading_day="2026-01-02",
        fills=5,
        pnl_bps=-1.0,
        reject_rate_p95=0.05,
        regime="mean_reverting",
        session_id="s2",
    )
    summary = tracker.summarize_paper_trade("a1")
    assert summary["session_count"] == 2
    assert summary["distinct_trading_days"] == 2
    assert summary["calendar_span_days"] == 2
    assert summary["total_fills"] == 15
    assert len(summary["regimes_covered"]) == 2
    assert summary["first_trading_day"] == "2026-01-01"
    assert summary["execution_reject_rate_p95"] is not None


def test_summarize_paper_trade_empty(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    summary = tracker.summarize_paper_trade("nonexistent")
    assert summary["session_count"] == 0
    assert summary["regimes_covered"] == []


# ---------------------------------------------------------------------------
# log_paper_trade_session edge cases (lines 497, 499, 509, 513-516, 519-520)
# ---------------------------------------------------------------------------


def test_log_paper_trade_session_no_timestamps(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    path = tracker.log_paper_trade_session(alpha_id="a1")
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["alpha_id"] == "a1"


def test_log_paper_trade_session_start_only(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    path = tracker.log_paper_trade_session(
        alpha_id="a1",
        started_at="2026-01-01T09:00:00+00:00",
    )
    assert path.exists()


def test_log_paper_trade_session_end_only(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    path = tracker.log_paper_trade_session(
        alpha_id="a1",
        ended_at="2026-01-01T10:00:00+00:00",
    )
    assert path.exists()


def test_log_paper_trade_session_invalid_started_at(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    with pytest.raises(ValueError, match="invalid_started_at"):
        tracker.log_paper_trade_session(
            alpha_id="a1",
            started_at="NOT_A_DATE",
        )


def test_log_paper_trade_session_invalid_ended_at(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    with pytest.raises(ValueError, match="invalid_ended_at"):
        tracker.log_paper_trade_session(
            alpha_id="a1",
            ended_at="NOT_A_DATE",
        )


def test_log_paper_trade_session_end_before_start(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    with pytest.raises(ValueError, match="invalid_session_window"):
        tracker.log_paper_trade_session(
            alpha_id="a1",
            started_at="2026-01-01T10:00:00+00:00",
            ended_at="2026-01-01T09:00:00+00:00",
        )


def test_log_paper_trade_session_equal_start_end(tmp_path: Path):
    """When start == end, the session window is extended by default_minutes."""
    tracker = ExperimentTracker(base_dir=tmp_path)
    path = tracker.log_paper_trade_session(
        alpha_id="a1",
        started_at="2026-01-01T09:00:00+00:00",
        ended_at="2026-01-01T09:00:00+00:00",
    )
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["duration_seconds"] == 3600


# ---------------------------------------------------------------------------
# _session_duration_seconds strict mode (lines 543-545, 547-549)
# ---------------------------------------------------------------------------


def test_session_duration_seconds_strict_bad_start():
    with pytest.raises(ValueError, match="invalid_started_at"):
        _session_duration_seconds("INVALID", "2026-01-01T10:00:00+00:00", strict=True)


def test_session_duration_seconds_strict_bad_end():
    with pytest.raises(ValueError, match="invalid_ended_at"):
        _session_duration_seconds("2026-01-01T09:00:00+00:00", "INVALID", strict=True)


def test_session_duration_seconds_nonstrict_returns_zero():
    result = _session_duration_seconds("INVALID", "INVALID", strict=False)
    assert result == 0


def test_session_duration_seconds_end_before_start_nonstrict():
    result = _session_duration_seconds(
        "2026-01-01T10:00:00+00:00",
        "2026-01-01T09:00:00+00:00",
        strict=False,
    )
    assert result == 0


# ---------------------------------------------------------------------------
# _parse_day invalid (lines 468-469)
# ---------------------------------------------------------------------------


def test_parse_day_invalid_returns_none():
    result = _parse_day("not-a-date")
    assert result is None


# ---------------------------------------------------------------------------
# _trading_day_from_iso invalid (lines 475-476)
# ---------------------------------------------------------------------------


def test_trading_day_from_iso_invalid_returns_today():
    result = _trading_day_from_iso("NOT_VALID")
    assert len(result) == 10  # YYYY-MM-DD format


# ---------------------------------------------------------------------------
# _coerce_utc naive vs aware (lines 481)
# ---------------------------------------------------------------------------


def test_coerce_utc_naive_datetime():
    dt = datetime(2026, 1, 1, 9, 0, 0)
    result = _coerce_utc(dt)
    assert result.tzinfo is not None


def test_coerce_utc_aware_datetime():
    from datetime import timedelta

    dt = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    result = _coerce_utc(dt)
    assert result.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# _is_legacy_zero_duration_session branches (lines 529, 531, 535)
# ---------------------------------------------------------------------------


def test_is_legacy_zero_duration_has_duration_seconds():
    payload = {"duration_seconds": 3600}
    result = _is_legacy_zero_duration_session(
        payload, started_at="2026-01-01T09:00:00+00:00", ended_at="2026-01-01T10:00:00+00:00"
    )
    assert result is False


def test_is_legacy_zero_duration_has_session_minutes():
    payload = {"session_duration_minutes": 60}
    result = _is_legacy_zero_duration_session(
        payload, started_at="2026-01-01T09:00:00+00:00", ended_at="2026-01-01T10:00:00+00:00"
    )
    assert result is False


def test_is_legacy_zero_duration_unparseable_timestamps():
    payload = {}
    result = _is_legacy_zero_duration_session(
        payload, started_at="INVALID", ended_at="INVALID"
    )
    assert result is False


def test_is_legacy_zero_duration_true():
    payload = {}
    ts = "2026-01-01T09:00:00+00:00"
    result = _is_legacy_zero_duration_session(payload, started_at=ts, ended_at=ts)
    assert result is True


# ---------------------------------------------------------------------------
# gc_experiment_runs (lines 415, 418-420, 436-437, 443-444)
# ---------------------------------------------------------------------------


def test_gc_experiment_runs_dry_run(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="old-run",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={"sharpe_oos": 0.1},
        gate_status={"gate_c": False},
        scorecard_payload={},
        backtest_report_payload={},
    )
    # Rewrite meta.json with an old timestamp
    meta_path = tracker.runs_dir / "old-run" / "meta.json"
    meta_data = json.loads(meta_path.read_text())
    meta_data["timestamp"] = "2020-01-01T00:00:00+00:00"
    meta_path.write_text(json.dumps(meta_data))

    result = gc_experiment_runs(
        base_dir=str(tmp_path),
        older_than_days=1,
        apply=False,
        promotions_dir=str(tmp_path / "nonexistent_promos"),
    )
    assert result["candidates"] >= 1
    assert result["applied"] is False
    assert result["deleted"] == 0


def test_gc_experiment_runs_apply_deletes(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="old-run-2",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={"sharpe_oos": 0.1},
        gate_status={"gate_c": False},
        scorecard_payload={},
        backtest_report_payload={},
    )
    meta_path = tracker.runs_dir / "old-run-2" / "meta.json"
    meta_data = json.loads(meta_path.read_text())
    meta_data["timestamp"] = "2020-01-01T00:00:00+00:00"
    meta_path.write_text(json.dumps(meta_data))

    result = gc_experiment_runs(
        base_dir=str(tmp_path),
        older_than_days=1,
        apply=True,
        promotions_dir=str(tmp_path / "nonexistent_promos"),
    )
    assert result["applied"] is True
    assert result["deleted"] >= 1
    assert not (tracker.runs_dir / "old-run-2").exists()


def test_gc_preserves_gate_c_passed(tmp_path: Path):
    tracker = ExperimentTracker(base_dir=tmp_path)
    tracker.log_run(
        run_id="good-run",
        alpha_id="a",
        config_hash="h",
        data_paths=[],
        metrics={},
        gate_status={"gate_c": True},
        scorecard_payload={},
        backtest_report_payload={},
    )
    meta_path = tracker.runs_dir / "good-run" / "meta.json"
    meta_data = json.loads(meta_path.read_text())
    meta_data["timestamp"] = "2020-01-01T00:00:00+00:00"
    meta_path.write_text(json.dumps(meta_data))

    result = gc_experiment_runs(
        base_dir=str(tmp_path),
        older_than_days=1,
        apply=True,
        promotions_dir=str(tmp_path / "nonexistent"),
    )
    assert result["preserved"] >= 1
    assert (tracker.runs_dir / "good-run").exists()


# ---------------------------------------------------------------------------
# PaperTradeSession.to_dict
# ---------------------------------------------------------------------------


def test_paper_trade_session_to_dict():
    session = PaperTradeSession(
        alpha_id="a",
        session_id="s",
        started_at="2026-01-01T09:00:00",
        ended_at="2026-01-01T10:00:00",
        duration_seconds=3600,
        trading_day="2026-01-01",
        fills=10,
        pnl_bps=1.5,
        drift_alerts=0,
        execution_reject_rate=0.01,
    )
    d = session.to_dict()
    assert d["alpha_id"] == "a"
    assert d["fills"] == 10
