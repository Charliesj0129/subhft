import numpy as np
import pytest

from hft_platform.backtest.result import BacktestResult


def _base_kwargs():
    return dict(
        run_id="r1",
        config_hash="h1",
        instrument="TMFD6",
        strategy_name="r47_maker_pivot",
        strategy_type="maker",
        engine="hftbacktest",
        queue_model="power_prob(1.5)",
        calibration_profile_id="TMFD6_2026-04-20",
        data_source="clickhouse_streaming",
        latency_profile="shioaji_sim_p95_v2026-03-04",
        pnl_pts=123.5,
        n_fills=50,
        n_trading_days=10,
        equity_curve=np.zeros((2, 100), dtype=np.float64),
    )


def test_backtest_result_frozen():
    r = BacktestResult(**_base_kwargs())
    with pytest.raises((AttributeError, TypeError)):
        r.pnl_pts = 999


def test_backtest_result_maker_fields():
    r = BacktestResult(**_base_kwargs(), pnl_per_fill=2.47, adverse_fill_pct=0.35, fill_rate_per_day=5.0)
    assert r.pnl_per_fill == 2.47
    assert r.ic_is is None


def test_backtest_result_taker_fields():
    kwargs = _base_kwargs()
    kwargs["strategy_type"] = "taker"
    r = BacktestResult(**kwargs, ic_is=0.08, ic_oos=0.05)
    assert r.ic_is == 0.08
    assert r.pnl_per_fill is None


def test_backtest_result_to_provenance_dict():
    r = BacktestResult(**_base_kwargs())
    prov = r.to_provenance_dict()
    assert prov["engine"] == "hftbacktest"
    assert prov["queue_model"] == "power_prob(1.5)"
    assert "equity_curve" not in prov  # arrays excluded
