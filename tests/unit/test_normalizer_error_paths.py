from unittest.mock import patch

from hft_platform.feed_adapter.normalizer import MarketDataNormalizer


class _Boom:
    def __getattr__(self, _name):
        raise RuntimeError("boom")


class _Counter:
    def __init__(self):
        self.calls = []

    def labels(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def inc(self):
        self.calls.append("inc")


class _Metrics:
    def __init__(self):
        self.normalization_errors_total = _Counter()


def test_normalize_tick_error_increments_metrics(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")

    metrics = _Metrics()
    with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry.get", return_value=metrics):
        normalizer = MarketDataNormalizer(str(cfg))
        event = normalizer.normalize_tick(_Boom())

    assert event is None
    assert {"type": "Tick"} in metrics.normalization_errors_total.calls


def test_normalize_bidask_error_increments_metrics(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")

    metrics = _Metrics()
    with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry.get", return_value=metrics):
        normalizer = MarketDataNormalizer(str(cfg))
        event = normalizer.normalize_bidask(_Boom())

    assert event is None
    assert {"type": "BidAsk"} in metrics.normalization_errors_total.calls
