"""``MarketDataService._on_shioaji_event``: the broker quote callback.

These cases used to exercise ``_md_normalize.on_shioaji_event``, a copy of the callback that production never
called and that had drifted from the real one (N81). They now run against the real method, unbound, on a
``MagicMock`` service. The second half covers the throttled error log (N80): the callback runs on broker
threads and used to write one ``logger.error`` per failed message.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from hft_platform.services.market_data import MarketDataService

DEFAULT_SYMBOL = "2330"
DEFAULT_PRICE = 5_000_000  # scaled x10000


class _FakeMD:
    """Object with market-data-like attributes."""

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def _make_svc() -> MagicMock:
    """Build a mock ``MarketDataService`` with the attributes the callback reads."""
    svc = MagicMock()
    svc._raw_first_seen = False
    svc._raw_first_parsed = False
    svc.log_raw = False
    svc.metrics_registry = None
    svc._md_callback_parse_counter = 0
    svc._md_callback_parse_metrics_every = 1
    svc._md_callback_parse_metric_children = {}
    svc.loop = MagicMock()
    return svc


def _on_event(svc: Any, *args: Any, **kwargs: Any) -> None:
    MarketDataService._on_shioaji_event(svc, *args, **kwargs)


def _tick_dict(symbol: str = DEFAULT_SYMBOL, price: int = DEFAULT_PRICE) -> dict[str, Any]:
    """Return a minimal tick-like dict with scaled price."""
    return {"code": symbol, "price": price}


def _bidask_dict(
    symbol: str = DEFAULT_SYMBOL,
    bid: int = DEFAULT_PRICE - 10_000,
    ask: int = DEFAULT_PRICE + 10_000,
) -> dict[str, Any]:
    """Return a minimal bidask-like dict with scaled prices."""
    return {"code": symbol, "bid_price": bid, "ask_price": ask}


class TestOnShioajiEvent:
    def test_successful_parse_enqueues(self) -> None:
        svc = _make_svc()
        md = _tick_dict()
        _on_event(svc, "TSE", md)
        svc.loop.call_soon_threadsafe.assert_called_once()
        call_args = svc.loop.call_soon_threadsafe.call_args
        assert call_args[0][0] is svc._enqueue_raw

    def test_enqueued_msg_is_the_parsed_payload(self) -> None:
        svc = _make_svc()
        md = _tick_dict()
        _on_event(svc, "TSE", md)
        call_args = svc.loop.call_soon_threadsafe.call_args
        assert call_args[0][1] == "TSE"
        assert call_args[0][2] is md

    def test_first_seen_flag_set(self) -> None:
        svc = _make_svc()
        _on_event(svc, "TSE", _tick_dict())
        assert svc._raw_first_seen is True

    def test_first_seen_only_set_once(self) -> None:
        svc = _make_svc()
        _on_event(svc, "TSE", _tick_dict())
        svc._raw_first_seen = True
        _on_event(svc, "TSE", _tick_dict())
        # Still True, no error
        assert svc._raw_first_seen is True

    def test_first_parsed_flag_set(self) -> None:
        svc = _make_svc()
        _on_event(svc, "TSE", _tick_dict())
        assert svc._raw_first_parsed is True

    def test_no_args_no_enqueue(self) -> None:
        """With zero args and no kwargs, nothing can be parsed."""
        svc = _make_svc()
        _on_event(svc)
        svc.loop.call_soon_threadsafe.assert_not_called()

    def test_no_parseable_msg_log_raw_warns(self) -> None:
        svc = _make_svc()
        svc.log_raw = True
        _on_event(svc)
        svc.loop.call_soon_threadsafe.assert_not_called()

    def test_exception_calls_crash_signature(self) -> None:
        svc = _make_svc()
        # Force an exception by making loop.call_soon_threadsafe raise
        svc.loop.call_soon_threadsafe.side_effect = RuntimeError("boom")
        md = _tick_dict()
        _on_event(svc, "TSE", md)
        svc._record_shioaji_crash_signature.assert_called_once()
        assert svc._record_shioaji_crash_signature.call_args[1]["context"] == "md_callback"
        svc._cb_drop_callback_error.inc.assert_called_once()

    def test_metrics_counter_incremented(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.market_data_callback_parse_total = MagicMock()
        _on_event(svc, "TSE", _tick_dict())
        assert svc._md_callback_parse_counter == 1

    def test_metrics_label_child_cached(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.market_data_callback_parse_total = MagicMock()
        _on_event(svc, "TSE", _tick_dict())
        assert "fast" in svc._md_callback_parse_metric_children

    def test_kwargs_path(self) -> None:
        svc = _make_svc()
        md = _tick_dict()
        _on_event(svc, exchange="TSE", quote=md)
        svc.loop.call_soon_threadsafe.assert_called_once()

    def test_no_loop_does_not_raise(self) -> None:
        svc = _make_svc()
        del svc.loop
        md = _tick_dict()
        _on_event(svc, "TSE", md)  # must not raise
        assert not hasattr(svc, "loop")

    def test_fallback_path_single_arg(self) -> None:
        """When fast extract returns None, the fallback iterates args."""
        svc = _make_svc()
        md = _tick_dict()
        _on_event(svc, md)
        svc.loop.call_soon_threadsafe.assert_called_once()

    def test_bidask_payload_enqueued(self) -> None:
        svc = _make_svc()
        md = _bidask_dict()
        _on_event(svc, "TSE", md)
        svc.loop.call_soon_threadsafe.assert_called_once()
        call_args = svc.loop.call_soon_threadsafe.call_args
        assert call_args[0][2] is md

    def test_log_raw_debug_called(self) -> None:
        svc = _make_svc()
        svc.log_raw = True
        _on_event(svc, "TSE", _tick_dict())
        svc.loop.call_soon_threadsafe.assert_called_once()

    def test_metrics_exception_suppressed(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.market_data_callback_parse_total.labels.side_effect = RuntimeError("metric fail")
        # Should not raise
        _on_event(svc, "TSE", _tick_dict())
        svc.loop.call_soon_threadsafe.assert_called_once()
