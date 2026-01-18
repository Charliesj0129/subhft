import numpy as np

from hft_platform.backtest.reporting import HTMLReporter


def test_html_reporter_compute_and_generate(tmp_path):
    report_path = tmp_path / "report.html"
    reporter = HTMLReporter(str(report_path))

    equity_t = np.array([1_000_000_000, 2_000_000_000, 3_000_000_000], dtype=np.int64)
    equity_v = np.array([100.0, 110.0, 105.0], dtype=float)

    reporter.compute_stats(equity_t, equity_v)
    assert "Total Return" in reporter.metrics
    assert reporter.equity_curve["time"]
    assert reporter.equity_curve["value"]

    reporter.generate()
    assert report_path.exists()
    assert "Equity Curve" in report_path.read_text()
