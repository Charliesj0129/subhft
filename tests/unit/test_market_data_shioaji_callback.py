"""``MarketDataService._on_shioaji_event``: the broker quote callback.

These cases used to exercise ``_md_normalize.on_shioaji_event``, a copy of the callback that production never
called and that had drifted from the real one (N81). They now run against the real method, unbound, on a
``MagicMock`` service. The second half covers the throttled error log (N80): the callback runs on broker
threads and used to write one ``logger.error`` per failed message.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.services import market_data as market_data_module
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


# ── N80: the callback's error log is throttled, the counters are not ─────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture()
def throttled(monkeypatch):
    """A service with the real ``_log_callback_error`` and a hand-driven clock."""
    clock = _Clock()
    monkeypatch.setattr(market_data_module, "time", clock)
    logger = MagicMock()
    monkeypatch.setattr(market_data_module, "logger", logger)
    svc = _make_svc()
    svc._cb_log_lock = threading.Lock()
    svc._cb_log_last = {}
    svc._cb_log_suppressed = {}
    svc._log_callback_error = lambda event, **fields: MarketDataService._log_callback_error(svc, event, **fields)
    return svc, clock, logger


def test_a_burst_of_callback_errors_logs_once_and_counts_every_one(throttled):
    svc, _clock, logger = throttled
    svc.loop.call_soon_threadsafe.side_effect = RuntimeError("malformed packet")

    for _ in range(500):
        _on_event(svc, "TSE", _tick_dict())

    assert svc._cb_drop_callback_error.inc.call_count == 500
    assert svc._record_shioaji_crash_signature.call_count == 500
    assert [c.args[0] for c in logger.error.call_args_list] == ["shioaji_callback_error"]
    assert logger.error.call_args.kwargs["suppressed_since_last"] == 0
    assert logger.error.call_args.kwargs["error"] == "malformed packet"


def test_the_next_log_after_the_interval_reports_how_many_it_skipped(throttled):
    svc, clock, logger = throttled
    svc.loop.call_soon_threadsafe.side_effect = RuntimeError("malformed packet")
    for _ in range(5):
        _on_event(svc, "TSE", _tick_dict())

    clock.now += market_data_module._CALLBACK_LOG_INTERVAL_S
    _on_event(svc, "TSE", _tick_dict())

    assert [c.kwargs["suppressed_since_last"] for c in logger.error.call_args_list] == [0, 4]


def test_a_second_burst_is_not_blamed_for_the_first(throttled):
    svc, clock, logger = throttled
    svc.loop.call_soon_threadsafe.side_effect = RuntimeError("malformed packet")
    _on_event(svc, "TSE", _tick_dict())
    clock.now += market_data_module._CALLBACK_LOG_INTERVAL_S
    _on_event(svc, "TSE", _tick_dict())  # logs, suppressed 0
    clock.now += market_data_module._CALLBACK_LOG_INTERVAL_S
    _on_event(svc, "TSE", _tick_dict())

    assert [c.kwargs["suppressed_since_last"] for c in logger.error.call_args_list] == [0, 0, 0]


def test_a_missing_loop_is_throttled_separately_from_a_callback_error(throttled):
    svc, _clock, logger = throttled
    del svc.loop

    for _ in range(50):
        _on_event(svc, "TSE", _tick_dict())
    svc.loop = MagicMock()
    svc.loop.call_soon_threadsafe.side_effect = RuntimeError("boom")
    _on_event(svc, "TSE", _tick_dict())

    assert svc._cb_drop_loop_missing.inc.call_count == 50
    assert [c.args[0] for c in logger.error.call_args_list] == ["Callback loop missing", "shioaji_callback_error"]


def test_a_healthy_callback_logs_no_error(throttled):
    svc, _clock, logger = throttled

    for _ in range(10):
        _on_event(svc, "TSE", _tick_dict())

    assert logger.error.call_args_list == []
    assert svc.loop.call_soon_threadsafe.call_count == 10
