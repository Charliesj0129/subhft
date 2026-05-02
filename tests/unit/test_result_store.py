"""Tests for ResultStore JSON persistence."""

import json
from pathlib import Path

import numpy as np
import pytest

from research.backtest.result_store import ResultStore
from research.backtest.types import BacktestResult


@pytest.fixture
def tmp_store(tmp_path: Path) -> ResultStore:
    return ResultStore(base_dir=tmp_path)


def _make_result(**overrides) -> BacktestResult:
    defaults = dict(
        signals=np.array([0.1, 0.2, 0.3]),
        equity_curve=np.array([1.0, 1.01, 1.02]),
        positions=np.array([0, 1, 0]),
        sharpe_is=1.5,
        sharpe_oos=0.8,
        ic_series=np.array([0.05, 0.06]),
        ic_mean=0.055,
        ic_std=0.01,
        ic_tstat=5.5,
        ic_pvalue=0.001,
        ic_halflife=10,
        sortino=1.2,
        cvar_5pct=-0.03,
        turnover=0.5,
        max_drawdown=0.1,
        regime_metrics={"high_vol_sharpe": 1.0},
        capacity_estimate=1e6,
        run_id="run-test-001",
        config_hash="abc123",
        latency_profile={"submit_ms": 36.0},
        engine_type="maker",
        fill_model="QueueDepletion(qf=0.5)",
        cost_model="TMFD6(comm=1.3,tax=0.7)",
        instrument="TMFD6",
        data_period="2026-03-01..2026-03-31",
        data_source="clickhouse://localhost:8123/hft",
        pipeline_mode="strict",
        created_at="2026-04-15T10:00:00Z",
        queue_fraction=0.5,
        daily_pnl=[{"date": "2026-03-01", "pnl": 100.0, "fills": 50}],
    )
    defaults.update(overrides)
    return BacktestResult(**defaults)


def test_save_creates_run_directory(tmp_store: ResultStore):
    result = _make_result()
    run_dir = tmp_store.save(result, alpha_id="r47_maker_pivot")
    assert run_dir.exists()
    assert (run_dir / "backtest_report.json").exists()
    assert (run_dir / "config.json").exists()
    assert (run_dir / "equity_curve.npy").exists()


def test_save_report_contains_provenance(tmp_store: ResultStore):
    result = _make_result()
    run_dir = tmp_store.save(result, alpha_id="r47_maker_pivot")
    report = json.loads((run_dir / "backtest_report.json").read_text())
    assert report["alpha_id"] == "r47_maker_pivot"
    assert report["engine_type"] == "maker"
    assert report["fill_model"] == "QueueDepletion(qf=0.5)"
    assert report["instrument"] == "TMFD6"
    assert report["sharpe_is"] == 1.5
    assert report["daily_pnl"][0]["date"] == "2026-03-01"


def test_load_roundtrip(tmp_store: ResultStore):
    result = _make_result()
    tmp_store.save(result, alpha_id="test_alpha")
    loaded = tmp_store.load("run-test-001")
    assert loaded.engine_type == "maker"
    assert loaded.sharpe_is == 1.5
    assert loaded.instrument == "TMFD6"
    assert np.allclose(loaded.equity_curve, result.equity_curve)


def test_query_by_instrument(tmp_store: ResultStore):
    tmp_store.save(_make_result(run_id="r1", instrument="TMFD6"), "alpha1")
    tmp_store.save(_make_result(run_id="r2", instrument="TXFD6"), "alpha2")
    results = tmp_store.query(instrument="TMFD6")
    assert len(results) == 1
    assert results[0]["instrument"] == "TMFD6"


def test_query_by_engine_type(tmp_store: ResultStore):
    tmp_store.save(_make_result(run_id="r1", engine_type="maker"), "alpha1")
    tmp_store.save(_make_result(run_id="r2", engine_type="taker"), "alpha2")
    results = tmp_store.query(engine_type="maker")
    assert len(results) == 1
    assert results[0]["engine_type"] == "maker"
