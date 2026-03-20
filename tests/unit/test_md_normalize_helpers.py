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


# ===================================================================
# _looks_like_md
# ===================================================================


class TestLooksLikeMd:
    def test_none_returns_false(self) -> None:
        assert _looks_like_md(None) is False

    def test_empty_dict_returns_false(self) -> None:
        assert _looks_like_md({}) is False

    def test_dict_with_code(self) -> None:
        assert _looks_like_md({"code": "2330"}) is True

    def test_dict_with_symbol(self) -> None:
        assert _looks_like_md({"symbol": "2330"}) is True

    def test_dict_with_price_field(self) -> None:
        assert _looks_like_md({"price": 100}) is True

    def test_dict_with_bid_price(self) -> None:
        assert _looks_like_md({"bid_price": 50}) is True

    def test_dict_with_ask_price(self) -> None:
        assert _looks_like_md({"ask_price": 51}) is True

    def test_dict_with_close(self) -> None:
        assert _looks_like_md({"close": 99}) is True

    def test_dict_with_bid_volume(self) -> None:
        assert _looks_like_md({"bid_volume": 10}) is True

    def test_dict_with_ask_volume(self) -> None:
        assert _looks_like_md({"ask_volume": 10}) is True

    def test_dict_with_buy_price(self) -> None:
        assert _looks_like_md({"buy_price": 100}) is True

    def test_dict_with_sell_price(self) -> None:
        assert _looks_like_md({"sell_price": 101}) is True

    def test_dict_with_ts_only(self) -> None:
        assert _looks_like_md({"ts": 123456}) is True

    def test_dict_with_datetime_only(self) -> None:
        assert _looks_like_md({"datetime": "2026-01-01"}) is True

    def test_dict_unrelated_keys(self) -> None:
        assert _looks_like_md({"foo": "bar", "baz": 1}) is False

    def test_object_with_code_attr(self) -> None:
        obj = _FakeMD(code="2330", ts=123)
        assert _looks_like_md(obj) is True

    def test_object_with_price_attr(self) -> None:
        obj = _FakeMD(bid_price=50)
        assert _looks_like_md(obj) is True

    def test_object_with_close_attr(self) -> None:
        obj = _FakeMD(close=100)
        assert _looks_like_md(obj) is True

    def test_plain_int_returns_false(self) -> None:
        assert _looks_like_md(42) is False

    def test_plain_string_returns_false(self) -> None:
        assert _looks_like_md("hello") is False


# ===================================================================
# _unwrap_md
# ===================================================================


class TestUnwrapMd:
    def test_none_returns_none(self) -> None:
        assert _unwrap_md(None) is None

    def test_plain_md_dict_returns_itself(self) -> None:
        d: dict[str, Any] = {"code": "2330", "price": 100}
        assert _unwrap_md(d) is d

    def test_dict_with_tick_nested(self) -> None:
        inner: dict[str, Any] = {"code": "2330", "price": 100}
        wrapper: dict[str, Any] = {"tick": inner, "other": 1}
        assert _unwrap_md(wrapper) is inner

    def test_dict_with_bidask_nested(self) -> None:
        inner: dict[str, Any] = {"code": "2330", "bid_price": 50}
        wrapper: dict[str, Any] = {"bidask": inner}
        assert _unwrap_md(wrapper) is inner

    def test_dict_tick_not_md_returns_original(self) -> None:
        wrapper: dict[str, Any] = {"tick": {"foo": "bar"}, "code": "2330"}
        assert _unwrap_md(wrapper) is wrapper

    def test_object_with_tick_attr(self) -> None:
        inner = _FakeMD(code="2330", price=100)
        outer = _FakeMD(tick=inner)
        assert _unwrap_md(outer) is inner

    def test_object_with_bidask_attr(self) -> None:
        inner = _FakeMD(code="2330", bid_price=50)
        outer = _FakeMD(bidask=inner)
        assert _unwrap_md(outer) is inner

    def test_object_no_nested_returns_itself(self) -> None:
        obj = _FakeMD(code="2330")
        assert _unwrap_md(obj) is obj


# ===================================================================
# _summarize_md
# ===================================================================


class TestSummarizeMd:
    def test_none_returns_empty(self) -> None:
        assert _summarize_md(None) == {}

    def test_dict_basic(self) -> None:
        result = _summarize_md({"code": "2330", "price": 100, "extra": "x"})
        assert "keys" in result
        assert "code" in result["present"]
        assert "price" in result["present"]
        assert isinstance(result["nested"], dict)

    def test_dict_with_nested_tick(self) -> None:
        d: dict[str, Any] = {"code": "2330", "tick": {"inner": 1}}
        result = _summarize_md(d)
        assert "tick" in result["nested"]
        assert result["nested"]["tick"] == "dict"

    def test_object_returns_attrs(self) -> None:
        obj = _FakeMD(code="2330", price=100)
        result = _summarize_md(obj)
        assert "attrs" in result
        assert "code" in result["attrs"]
        assert "price" in result["attrs"]

    def test_object_with_nested(self) -> None:
        inner = _FakeMD(code="2330")
        obj = _FakeMD(tick=inner, code="2330")
        result = _summarize_md(obj)
        assert "tick" in result["nested"]

    def test_keys_truncated_to_20(self) -> None:
        big_dict = {f"k{i}": i for i in range(30)}
        result = _summarize_md(big_dict)
        assert len(result["keys"]) == 20


# ===================================================================
# _try_fast_extract_callback_payload
# ===================================================================


class TestTryFastExtractCallbackPayload:
    def test_kwargs_quote(self) -> None:
        md: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, msg = _try_fast_extract_callback_payload(quote=md)
        assert msg is md
        assert exchange is None

    def test_kwargs_quote_with_exchange(self) -> None:
        md: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, msg = _try_fast_extract_callback_payload(exchange="TSE", quote=md)
        assert msg is md
        assert exchange == "TSE"

    def test_kwargs_tick(self) -> None:
        md: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, msg = _try_fast_extract_callback_payload(tick=md)
        assert msg is md

    def test_kwargs_bidask(self) -> None:
        md: dict[str, Any] = {"code": "2330", "bid_price": 50}
        exchange, msg = _try_fast_extract_callback_payload(bidask=md)
        assert msg is md

    def test_kwargs_data(self) -> None:
        md: dict[str, Any] = {"code": "2330", "close": 100}
        exchange, msg = _try_fast_extract_callback_payload(data=md)
        assert msg is md

    def test_kwargs_msg(self) -> None:
        md: dict[str, Any] = {"code": "2330", "close": 100}
        exchange, msg = _try_fast_extract_callback_payload(msg=md)
        assert msg is md

    def test_two_args_exchange_and_msg(self) -> None:
        md: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, msg = _try_fast_extract_callback_payload("TSE", md)
        assert msg is md
        assert exchange == "TSE"

    def test_two_args_reversed(self) -> None:
        md: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, msg = _try_fast_extract_callback_payload(md, "TSE")
        assert msg is md
        assert exchange == "TSE"

    def test_single_arg(self) -> None:
        md: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, msg = _try_fast_extract_callback_payload(md)
        assert msg is md
        assert exchange is None

    def test_three_plus_args(self) -> None:
        md: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, msg = _try_fast_extract_callback_payload("TSE", "topic", md)
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
        inner: dict[str, Any] = {"code": "2330", "price": 100}
        wrapper: dict[str, Any] = {"tick": inner}
        exchange, msg = _try_fast_extract_callback_payload(quote=wrapper)
        assert msg is inner


# ===================================================================
# on_shioaji_event
# ===================================================================


class TestOnShioajiEvent:
    def test_successful_parse_enqueues(self) -> None:
        svc = _make_svc()
        md: dict[str, Any] = {"code": "2330", "price": 100}
        on_shioaji_event(svc, "TSE", md)
        svc.loop.call_soon_threadsafe.assert_called_once()
        call_args = svc.loop.call_soon_threadsafe.call_args
        assert call_args[0][0] is svc._enqueue_raw

    def test_first_seen_flag_set(self) -> None:
        svc = _make_svc()
        md: dict[str, Any] = {"code": "2330", "price": 100}
        on_shioaji_event(svc, "TSE", md)
        assert svc._raw_first_seen is True

    def test_first_parsed_flag_set(self) -> None:
        svc = _make_svc()
        md: dict[str, Any] = {"code": "2330", "price": 100}
        on_shioaji_event(svc, "TSE", md)
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
        md: dict[str, Any] = {"code": "2330", "price": 100}
        on_shioaji_event(svc, "TSE", md)
        mock_record.assert_called_once()
        assert mock_record.call_args[1]["context"] == "md_callback"

    def test_metrics_counter_incremented(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.market_data_callback_parse_total = MagicMock()
        md: dict[str, Any] = {"code": "2330", "price": 100}
        on_shioaji_event(svc, "TSE", md)
        assert svc._md_callback_parse_counter == 1

    def test_kwargs_path(self) -> None:
        svc = _make_svc()
        md: dict[str, Any] = {"code": "2330", "price": 100}
        on_shioaji_event(svc, exchange="TSE", quote=md)
        svc.loop.call_soon_threadsafe.assert_called_once()

    def test_no_loop_does_not_raise(self) -> None:
        svc = _make_svc()
        del svc.loop
        md: dict[str, Any] = {"code": "2330", "price": 100}
        # Should not raise
        on_shioaji_event(svc, "TSE", md)

    def test_fallback_path_single_arg(self) -> None:
        """When fast extract returns None, the fallback iterates args."""
        svc = _make_svc()
        md: dict[str, Any] = {"code": "2330", "price": 100}
        # single arg should still work
        on_shioaji_event(svc, md)
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

    @patch("hft_platform.services._md_normalize.detect_crash_signature", return_value=None)
    def test_no_signature_noop(self, mock_detect: MagicMock) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.shioaji_crash_signature_total = MagicMock()
        _record_shioaji_crash_signature(svc, "unknown", context="test")
        svc.metrics_registry.shioaji_crash_signature_total.labels.assert_not_called()

    @patch("hft_platform.services._md_normalize.detect_crash_signature", return_value="conn_reset")
    def test_known_signature_records_metric(self, mock_detect: MagicMock) -> None:
        svc = _make_svc()
        svc.metrics_registry = MagicMock()
        svc.metrics_registry.shioaji_crash_signature_total = MagicMock()
        _record_shioaji_crash_signature(svc, "connection reset by peer", context="md_callback")
        svc.metrics_registry.shioaji_crash_signature_total.labels.assert_called_once_with(
            signature="conn_reset", context="md_callback"
        )

    @patch("hft_platform.services._md_normalize.detect_crash_signature", return_value="some_sig")
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
