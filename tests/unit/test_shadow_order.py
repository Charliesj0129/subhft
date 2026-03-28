"""Tests for WU-10: Shadow Order Mode."""

from unittest.mock import MagicMock

from hft_platform.order.shadow import ShadowOrderSink


def _make_intent(**kwargs):
    intent = MagicMock()
    intent.strategy_id = kwargs.get("strategy_id", "test_strat")
    intent.symbol = kwargs.get("symbol", "2330")
    intent.side = MagicMock()
    intent.side.name = kwargs.get("side", "BUY")
    intent.price = kwargs.get("price", 5000000)
    intent.qty = kwargs.get("qty", 1)
    intent.intent_type = MagicMock()
    intent.intent_type.name = kwargs.get("intent_type", "NEW")
    intent.intent_id = kwargs.get("intent_id", "test_001")
    return intent


class TestShadowOrderSinkInit:
    def test_disabled_by_default(self):
        sink = ShadowOrderSink()
        assert sink.enabled is False

    def test_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("HFT_ORDER_SHADOW_MODE", "1")
        sink = ShadowOrderSink()
        assert sink.enabled is True

    def test_enabled_via_constructor(self):
        sink = ShadowOrderSink(enabled=True)
        assert sink.enabled is True


class TestShadowOrderSinkIntercept:
    def test_intercept_returns_record(self):
        sink = ShadowOrderSink(enabled=True)
        record = sink.intercept(_make_intent())
        assert record["strategy_id"] == "test_strat"
        assert record["shadow"] is True

    def test_intercept_increments_counter(self):
        sink = ShadowOrderSink(enabled=True)
        assert sink.counter == 0
        sink.intercept(_make_intent())
        assert sink.counter == 1

    def test_enabled_setter(self):
        sink = ShadowOrderSink(enabled=False)
        sink.enabled = True
        assert sink.enabled is True


class TestShadowModeMetric:
    def test_enabled_sets_metric_to_1(self, monkeypatch):
        """shadow_mode_active gauge should be 1 when enabled."""
        mock_metrics = MagicMock()
        mock_gauge = MagicMock()
        mock_metrics.shadow_mode_active = mock_gauge
        monkeypatch.setattr(
            "hft_platform.order.shadow._get_metrics", lambda: mock_metrics
        )
        ShadowOrderSink(enabled=True)
        mock_gauge.set.assert_called_once_with(1)

    def test_disabled_sets_metric_to_0(self, monkeypatch):
        """shadow_mode_active gauge should be 0 when disabled."""
        mock_metrics = MagicMock()
        mock_gauge = MagicMock()
        mock_metrics.shadow_mode_active = mock_gauge
        monkeypatch.setattr(
            "hft_platform.order.shadow._get_metrics", lambda: mock_metrics
        )
        ShadowOrderSink(enabled=False)
        mock_gauge.set.assert_called_once_with(0)
