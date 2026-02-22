import numpy as np

from hft_platform.backtest.equity import EquitySeries, extract_equity_series, mark_to_market_equity


class _StatsObj:
    def __init__(self):
        self.timestamps = np.array([1, 2, 3], dtype=np.int64)
        self.equity = np.array([100.0, 101.0, 103.5], dtype=np.float64)


class _HbtWithStats:
    def stats(self):
        return _StatsObj()


class _AdapterLike:
    def __init__(self):
        self.equity_timestamps_ns = np.array([10, 20, 30], dtype=np.int64)
        self.equity_values = np.array([1_000_000.0, 1_000_050.0, 1_000_020.0], dtype=np.float64)


def test_mark_to_market_equity():
    out = mark_to_market_equity([100.0, 95.0], [2, -1], [10.0, 11.0])
    assert np.allclose(out, np.array([120.0, 84.0], dtype=np.float64))


def test_extract_equity_series_prefers_adapter_trace():
    series = extract_equity_series(_AdapterLike())
    assert isinstance(series, EquitySeries)
    assert series is not None
    assert np.array_equal(series.timestamps_ns, np.array([10, 20, 30], dtype=np.int64))
    assert np.array_equal(series.equity, np.array([1_000_000.0, 1_000_050.0, 1_000_020.0], dtype=np.float64))


def test_extract_equity_series_from_stats():
    series = extract_equity_series(_HbtWithStats())
    assert isinstance(series, EquitySeries)
    assert series is not None
    assert series.is_valid()
    assert float(series.equity[-1]) == 103.5


def test_extract_equity_series_rejects_invalid():
    class _Invalid:
        equity_timestamps_ns = []
        equity_values = []

    assert extract_equity_series(_Invalid()) is None
