"""Tests for src/hft_platform/services/_md_normalize.py helper functions."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

# Guard the import — the module depends on shioaji.signatures which may not be available.
with patch(
    "hft_platform.feed_adapter.shioaji.signatures.detect_crash_signature",
    return_value=None,
    create=True,
):
    from hft_platform.services._md_normalize import (
        _looks_like_md,
        _record_shioaji_crash_signature,
        _summarize_md,
        _try_fast_extract_callback_payload,
        _unwrap_md,
        on_shioaji_event,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_SYMBOL = "2330"
DEFAULT_PRICE = 5_000_000  # scaled x10000


class _FakeMD:
    """Object with market-data-like attributes."""

    def __init__(self, **kw: Any) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


def _make_svc() -> MagicMock:
    """Build a mock ``MarketDataService`` with required attributes."""
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


# ===================================================================
# _looks_like_md
# ===================================================================


class TestLooksLikeMd:
    def test_none_returns_false(self) -> None:
        assert _looks_like_md(None) is False

    def test_empty_dict_returns_false(self) -> None:
        assert _looks_like_md({}) is False

    def test_dict_with_code(self) -> None:
        assert _looks_like_md({"code": DEFAULT_SYMBOL}) is True

    def test_dict_with_symbol(self) -> None:
        assert _looks_like_md({"symbol": DEFAULT_SYMBOL}) is True

    def test_dict_with_price_field(self) -> None:
        assert _looks_like_md({"price": DEFAULT_PRICE}) is True

    def test_dict_with_bid_price(self) -> None:
        assert _looks_like_md({"bid_price": DEFAULT_PRICE}) is True

    def test_dict_with_ask_price(self) -> None:
        assert _looks_like_md({"ask_price": DEFAULT_PRICE + 10_000}) is True

    def test_dict_with_close(self) -> None:
        assert _looks_like_md({"close": DEFAULT_PRICE}) is True

    def test_dict_with_bid_volume(self) -> None:
        assert _looks_like_md({"bid_volume": 10}) is True

    def test_dict_with_ask_volume(self) -> None:
        assert _looks_like_md({"ask_volume": 10}) is True

    def test_dict_with_buy_price(self) -> None:
        assert _looks_like_md({"buy_price": DEFAULT_PRICE}) is True

    def test_dict_with_sell_price(self) -> None:
        assert _looks_like_md({"sell_price": DEFAULT_PRICE}) is True

    def test_dict_with_ts_only(self) -> None:
        assert _looks_like_md({"ts": 123456}) is True

    def test_dict_with_datetime_only(self) -> None:
        assert _looks_like_md({"datetime": "2026-01-01"}) is True

    def test_dict_unrelated_keys(self) -> None:
        assert _looks_like_md({"foo": "bar", "baz": 1}) is False

    def test_object_with_code_and_ts(self) -> None:
        obj = _FakeMD(code=DEFAULT_SYMBOL, ts=123)
        assert _looks_like_md(obj) is True

    def test_object_with_price_attr(self) -> None:
        obj = _FakeMD(bid_price=DEFAULT_PRICE)
        assert _looks_like_md(obj) is True

    def test_object_with_close_attr(self) -> None:
        obj = _FakeMD(close=DEFAULT_PRICE)
        assert _looks_like_md(obj) is True

    def test_object_with_ask_volume_attr(self) -> None:
        obj = _FakeMD(ask_volume=5)
        assert _looks_like_md(obj) is True

    def test_object_with_symbol_and_time(self) -> None:
        obj = _FakeMD(symbol=DEFAULT_SYMBOL, ts=99)
        assert _looks_like_md(obj) is True

    def test_plain_int_returns_false(self) -> None:
        assert _looks_like_md(42) is False

    def test_plain_string_returns_false(self) -> None:
        assert _looks_like_md("hello") is False

    def test_empty_list_returns_false(self) -> None:
        assert _looks_like_md([]) is False

    def test_object_code_none_no_price(self) -> None:
        """code=None should not count as having code."""
        obj = _FakeMD(code=None)
        assert _looks_like_md(obj) is False


# ===================================================================
# _unwrap_md
# ===================================================================


class TestUnwrapMd:
    def test_none_returns_none(self) -> None:
        assert _unwrap_md(None) is None

    def test_plain_md_dict_returns_itself(self) -> None:
        d = _tick_dict()
        assert _unwrap_md(d) is d

    def test_dict_with_tick_nested(self) -> None:
        inner = _tick_dict()
        wrapper: dict[str, Any] = {"tick": inner, "other": 1}
        assert _unwrap_md(wrapper) is inner

    def test_dict_with_bidask_nested(self) -> None:
        inner = _bidask_dict()
        wrapper: dict[str, Any] = {"bidask": inner}
        assert _unwrap_md(wrapper) is inner

    def test_dict_tick_not_md_returns_original(self) -> None:
        wrapper: dict[str, Any] = {"tick": {"foo": "bar"}, "code": DEFAULT_SYMBOL}
        assert _unwrap_md(wrapper) is wrapper

    def test_dict_tick_takes_priority_over_bidask(self) -> None:
        tick_inner = _tick_dict()
        ba_inner = _bidask_dict()
        wrapper: dict[str, Any] = {"tick": tick_inner, "bidask": ba_inner}
        assert _unwrap_md(wrapper) is tick_inner

    def test_object_with_tick_attr(self) -> None:
        inner = _FakeMD(code=DEFAULT_SYMBOL, price=DEFAULT_PRICE)
        outer = _FakeMD(tick=inner)
        assert _unwrap_md(outer) is inner

    def test_object_with_bidask_attr(self) -> None:
        inner = _FakeMD(code=DEFAULT_SYMBOL, bid_price=DEFAULT_PRICE)
        outer = _FakeMD(bidask=inner)
        assert _unwrap_md(outer) is inner

    def test_object_no_nested_returns_itself(self) -> None:
        obj = _FakeMD(code=DEFAULT_SYMBOL)
        assert _unwrap_md(obj) is obj

    def test_object_tick_not_md_falls_to_bidask(self) -> None:
        non_md = _FakeMD(foo="bar")
        ba = _FakeMD(code=DEFAULT_SYMBOL, bid_price=DEFAULT_PRICE)
        outer = _FakeMD(tick=non_md, bidask=ba)
        assert _unwrap_md(outer) is ba


# ===================================================================
# _summarize_md
# ===================================================================


class TestSummarizeMd:
    def test_none_returns_empty(self) -> None:
        assert _summarize_md(None) == {}

    def test_dict_basic(self) -> None:
        result = _summarize_md({"code": DEFAULT_SYMBOL, "price": DEFAULT_PRICE, "extra": "x"})
        assert "keys" in result
        assert "code" in result["present"]
        assert "price" in result["present"]
        assert isinstance(result["nested"], dict)

    def test_dict_with_nested_tick(self) -> None:
        d: dict[str, Any] = {"code": DEFAULT_SYMBOL, "tick": {"inner": 1}}
        result = _summarize_md(d)
        assert "tick" in result["nested"]
        assert result["nested"]["tick"] == "dict"

    def test_dict_with_nested_bidask(self) -> None:
        d: dict[str, Any] = {"code": DEFAULT_SYMBOL, "bidask": _bidask_dict()}
        result = _summarize_md(d)
        assert "bidask" in result["nested"]
        assert result["nested"]["bidask"] == "dict"

    def test_object_returns_attrs(self) -> None:
        obj = _FakeMD(code=DEFAULT_SYMBOL, price=DEFAULT_PRICE)
        result = _summarize_md(obj)
        assert "attrs" in result
        assert "code" in result["attrs"]
        assert "price" in result["attrs"]

    def test_object_with_nested(self) -> None:
        inner = _FakeMD(code=DEFAULT_SYMBOL)
        obj = _FakeMD(tick=inner, code=DEFAULT_SYMBOL)
        result = _summarize_md(obj)
        assert "tick" in result["nested"]

    def test_keys_truncated_to_20(self) -> None:
        big_dict = {f"k{i}": i for i in range(30)}
        result = _summarize_md(big_dict)
        assert len(result["keys"]) == 20

    def test_dict_all_time_fields(self) -> None:
        d: dict[str, Any] = {"ts": 1000, "datetime": "2026-01-01"}
        result = _summarize_md(d)
        assert "ts" in result["present"]
        assert "datetime" in result["present"]

    def test_object_nested_type_name(self) -> None:
        inner = _FakeMD(code=DEFAULT_SYMBOL)
        obj = _FakeMD(bidask=inner, code=DEFAULT_SYMBOL)
        result = _summarize_md(obj)
        assert result["nested"]["bidask"] == "_FakeMD"


# ===================================================================
# _try_fast_extract_callback_payload
# ===================================================================


class TestTryFastExtractCallbackPayload:
    def test_kwargs_quote(self) -> None:
        md = _tick_dict()
        exchange, msg = _try_fast_extract_callback_payload(quote=md)
        assert msg is md
        assert exchange is None

    def test_kwargs_quote_with_exchange(self) -> None:
        md = _tick_dict()
        exchange, msg = _try_fast_extract_callback_payload(exchange="TSE", quote=md)
        assert msg is md
        assert exchange == "TSE"

    def test_kwargs_tick(self) -> None:
        md = _tick_dict()
        exchange, msg = _try_fast_extract_callback_payload(tick=md)
        assert msg is md

    def test_kwargs_bidask(self) -> None:
        md = _bidask_dict()
        exchange, msg = _try_fast_extract_callback_payload(bidask=md)
        assert msg is md

    def test_kwargs_data(self) -> None:
        md: dict[str, Any] = {"code": DEFAULT_SYMBOL, "close": DEFAULT_PRICE}
        exchange, msg = _try_fast_extract_callback_payload(data=md)
        assert msg is md

    def test_kwargs_msg(self) -> None:
        md: dict[str, Any] = {"code": DEFAULT_SYMBOL, "close": DEFAULT_PRICE}
        exchange, msg = _try_fast_extract_callback_payload(msg=md)
        assert msg is md

    def test_two_args_exchange_and_msg(self) -> None:
        md = _tick_dict()
        exchange, msg = _try_fast_extract_callback_payload("TSE", md)
        assert msg is md
        assert exchange == "TSE"

    def test_two_args_reversed(self) -> None:
        md = _tick_dict()
        exchange, msg = _try_fast_extract_callback_payload(md, "TSE")
        assert msg is md
        assert exchange == "TSE"

    def test_single_arg(self) -> None:
        md = _tick_dict()
        exchange, msg = _try_fast_extract_callback_payload(md)
        assert msg is md
        assert exchange is None

    def test_three_plus_args_md_last(self) -> None:
        md = _tick_dict()
        exchange, msg = _try_fast_extract_callback_payload("TSE", "topic", md)
        assert msg is md
        assert exchange == "TSE"

    def test_three_plus_args_md_second_to_last(self) -> None:
        md = _tick_dict()
        exchange, msg = _try_fast_extract_callback_payload("TSE", md, "event")
        assert msg is md
        assert exchange == "TSE"

    def test_no_md_returns_none(self) -> None:
        exchange, msg = _try_fast_extract_callback_payload("a", "b")
        assert msg is None

    def test_empty_returns_none(self) -> None:
        exchange, msg = _try_fast_extract_callback_payload()
        assert msg is None
        assert exchange is None

    def test_kwargs_nested_unwrap(self) -> None:
        inner = _tick_dict()
        wrapper: dict[str, Any] = {"tick": inner}
        exchange, msg = _try_fast_extract_callback_payload(quote=wrapper)
        assert msg is inner

    def test_kwargs_non_md_skipped(self) -> None:
        """Non-MD kwarg values should be skipped, later kwarg wins."""
        md = _tick_dict()
        exchange, msg = _try_fast_extract_callback_payload(quote={"foo": 1}, tick=md)
        assert msg is md

    def test_exchange_from_object_with_name(self) -> None:
        """Exchange can be an object with a .name attribute."""
        md = _tick_dict()
        exch_obj = _FakeMD(name="TSE")
        exchange, msg = _try_fast_extract_callback_payload(exch_obj, md)
        assert msg is md
        assert exchange is exch_obj

    def test_three_args_exchange_object(self) -> None:
        md = _tick_dict()
        exch_obj = _FakeMD(name="OTC")
        exchange, msg = _try_fast_extract_callback_payload(exch_obj, "topic", md)
        assert msg is md
        assert exchange is exch_obj


# ===================================================================
# on_shioaji_event
# ===================================================================


class TestOnShioajiEvent:
    def test_successful_parse_enqueues(self) -> None:
        svc = _make_svc()
        md = _tick_dict()
        on_shioaji_event(svc, "TSE", md)
        svc.loop.call_soon_threadsafe.assert_called_once()
        call_args = svc.loop.call_soon_threadsafe.call_args
        assert call_args[0][0] is svc._enqueue_raw

    def test_enqueued_msg_is_the_parsed_payload(self) -> None:
        svc = _make_svc()
        md = _tick_dict()
        on_shioaji_event(svc, "TSE", md)
        call_args = svc.loop.call_soon_threadsafe.call_args
        assert call_args[0][1] == "TSE"
        assert call_args[0][2] is md

    def test_first_seen_flag_set(self) -> None:
        svc = _make_svc()
        on_shioaji_event(svc, "TSE", _tick_dict())
        assert svc._raw_first_seen is True

    def test_first_seen_only_set_once(self) -> None:
        svc = _make_svc()
        on_shioaji_event(svc, "TSE", _tick_dict())
        svc._raw_first_seen = True
        on_shioaji_event(svc, "TSE", _tick_dict())
        # Still True, no error
        assert svc._raw_first_seen is True

    def test_first_parsed_flag_set(self) -> None:
        svc = _make_svc()
        on_shioaji_event(svc, "TSE", _tick_dict())
        assert svc._raw_first_parsed is True

    def test_no_args_no_enqueue(self) -> None:
        """With zero args and no kwargs, nothing can be parsed."""
        svc = _make_svc()
        on_shioaji_event(svc)
        svc.loop.call_soon_threadsafe.assert_not_called()

    def test_no_parseable_msg_log_raw_warns(self) -> None:
        svc = _make_svc()
        svc.log_raw = True
        on_shioaji_event(svc)
        svc.loop.call_soon_threadsafe.assert_not_called()

    @patch("hft_platform.services._md_normalize._record_shioaji_crash_signature")
    def test_exception_calls_crash_signature(self, mock_record: MagicMock) -> None:
        svc = _make_svc()
        # Force an exception by making loop.call_soon_threadsafe raise
        svc.loop.call_soon_threadsafe.side_effect = RuntimeError("boom")
        md = _tick_dict()
        on_shioaji_event(svc, "TSE", md)
        mock_record.assert_called_once()
        assert mock_record.call_args[1]["context"] == "md_callback"

    def test_metrics_counter_incremented(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.market_data_callback_parse_total = MagicMock()
        on_shioaji_event(svc, "TSE", _tick_dict())
        assert svc._md_callback_parse_counter == 1

    def test_metrics_label_child_cached(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.market_data_callback_parse_total = MagicMock()
        on_shioaji_event(svc, "TSE", _tick_dict())
        assert "fast" in svc._md_callback_parse_metric_children

    def test_kwargs_path(self) -> None:
        svc = _make_svc()
        md = _tick_dict()
        on_shioaji_event(svc, exchange="TSE", quote=md)
        svc.loop.call_soon_threadsafe.assert_called_once()

    def test_no_loop_does_not_raise(self) -> None:
        svc = _make_svc()
        del svc.loop
        md = _tick_dict()
        # Should not raise
        on_shioaji_event(svc, "TSE", md)

    def test_fallback_path_single_arg(self) -> None:
        """When fast extract returns None, the fallback iterates args."""
        svc = _make_svc()
        md = _tick_dict()
        on_shioaji_event(svc, md)
        svc.loop.call_soon_threadsafe.assert_called_once()

    def test_bidask_payload_enqueued(self) -> None:
        svc = _make_svc()
        md = _bidask_dict()
        on_shioaji_event(svc, "TSE", md)
        svc.loop.call_soon_threadsafe.assert_called_once()
        call_args = svc.loop.call_soon_threadsafe.call_args
        assert call_args[0][2] is md

    def test_log_raw_debug_called(self) -> None:
        svc = _make_svc()
        svc.log_raw = True
        on_shioaji_event(svc, "TSE", _tick_dict())
        svc.loop.call_soon_threadsafe.assert_called_once()

    def test_metrics_exception_suppressed(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.market_data_callback_parse_total.labels.side_effect = RuntimeError("metric fail")
        # Should not raise
        on_shioaji_event(svc, "TSE", _tick_dict())
        svc.loop.call_soon_threadsafe.assert_called_once()


# ===================================================================
# _record_shioaji_crash_signature
# ===================================================================


class TestRecordShioajiCrashSignature:
    def test_no_metrics_registry_noop(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = None
        # Should not raise
        _record_shioaji_crash_signature(svc, "error text", context="test")

    @patch(
        "hft_platform.services._md_normalize.detect_crash_signature",
        return_value=None,
    )
    def test_no_signature_noop(self, mock_detect: MagicMock) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.shioaji_crash_signature_total = MagicMock()
        _record_shioaji_crash_signature(svc, "unknown", context="test")
        svc.metrics_registry.shioaji_crash_signature_total.labels.assert_not_called()

    @patch(
        "hft_platform.services._md_normalize.detect_crash_signature",
        return_value="conn_reset",
    )
    def test_known_signature_records_metric(self, mock_detect: MagicMock) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.shioaji_crash_signature_total = MagicMock()
        _record_shioaji_crash_signature(svc, "connection reset by peer", context="md_callback")
        svc.metrics_registry.shioaji_crash_signature_total.labels.assert_called_once_with(
            signature="conn_reset", context="md_callback"
        )

    @patch(
        "hft_platform.services._md_normalize.detect_crash_signature",
        return_value="some_sig",
    )
    def test_metric_exception_suppressed(self, mock_detect: MagicMock) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.shioaji_crash_signature_total.labels.side_effect = RuntimeError("oops")
        # Should not raise
        _record_shioaji_crash_signature(svc, "error", context="test")

    def test_no_crash_signature_attr_noop(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock(spec=[])  # no attrs at all
        _record_shioaji_crash_signature(svc, "text", context="test")
