"""Verify canary slippage query reads from hft.trades."""
import inspect

from hft_platform.alpha.canary_metrics_writer import CanaryMetricsWriter


def test_slippage_query_uses_hft_trades() -> None:
    source = inspect.getsource(CanaryMetricsWriter)
    assert "hft.alpha_trades" not in source
    assert "hft.trades" in source
