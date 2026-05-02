"""Tests for src/hft_platform/services/_md_ingestion.py helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from hft_platform.services._md_ingestion import (
    FeedState,
    env_int,
    get_trace_sampler,
    looks_like_md,
    obs_policy,
    summarize_md,
    try_fast_extract_callback_payload,
    unwrap_md,
)

# -----------------------------------------------------------------------
# FeedState
# -----------------------------------------------------------------------


class TestFeedState:
    def test_all_six_states_exist(self) -> None:
        expected = {"INIT", "CONNECTING", "SNAPSHOTTING", "CONNECTED", "DISCONNECTED", "RECOVERING"}
        actual = {s.name for s in FeedState}
        assert actual == expected

    def test_values_are_strings(self) -> None:
        for state in FeedState:
            assert isinstance(state.value, str)

    def test_values_match_names(self) -> None:
        for state in FeedState:
            assert state.value == state.name

    def test_enum_length(self) -> None:
        assert len(FeedState) == 6

    def test_lookup_by_value(self) -> None:
        assert FeedState("INIT") is FeedState.INIT
        assert FeedState("RECOVERING") is FeedState.RECOVERING


# -----------------------------------------------------------------------
# env_int
# -----------------------------------------------------------------------


class TestEnvInt:
    def test_normal_case(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENV_INT", "42")
        assert env_int("TEST_ENV_INT", 10) == 42

    def test_missing_env_var_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_ENV_INT_MISSING", raising=False)
        assert env_int("TEST_ENV_INT_MISSING", 7) == 7

    def test_invalid_value_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENV_INT", "not_a_number")
        assert env_int("TEST_ENV_INT", 5) == 5

    def test_zero_clamped_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENV_INT", "0")
        assert env_int("TEST_ENV_INT", 10) == 1

    def test_negative_clamped_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENV_INT", "-5")
        assert env_int("TEST_ENV_INT", 10) == 1

    def test_one_is_minimum(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENV_INT", "1")
        assert env_int("TEST_ENV_INT", 10) == 1

    def test_large_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENV_INT", "100000")
        assert env_int("TEST_ENV_INT", 10) == 100000

    def test_default_zero_clamped_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENV_INT", "abc")
        # default=0, but clamped to 1
        assert env_int("TEST_ENV_INT", 0) == 1

    def test_empty_string_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENV_INT", "")
        assert env_int("TEST_ENV_INT", 5) == 5

    def test_whitespace_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_ENV_INT", "  ")
        assert env_int("TEST_ENV_INT", 3) == 3


# -----------------------------------------------------------------------
# obs_policy
# -----------------------------------------------------------------------


class TestObsPolicy:
    def test_default_is_balanced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_OBS_POLICY", raising=False)
        assert obs_policy() == "balanced"

    def test_minimal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_OBS_POLICY", "minimal")
        assert obs_policy() == "minimal"

    def test_balanced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_OBS_POLICY", "balanced")
        assert obs_policy() == "balanced"

    def test_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_OBS_POLICY", "debug")
        assert obs_policy() == "debug"

    def test_invalid_falls_back_to_balanced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_OBS_POLICY", "verbose")
        assert obs_policy() == "balanced"

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_OBS_POLICY", "MINIMAL")
        assert obs_policy() == "minimal"

    def test_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_OBS_POLICY", "  debug  ")
        assert obs_policy() == "debug"

    def test_mixed_case_with_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_OBS_POLICY", " Balanced ")
        assert obs_policy() == "balanced"

    def test_empty_string_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_OBS_POLICY", "")
        assert obs_policy() == "balanced"


# -----------------------------------------------------------------------
# get_trace_sampler
# -----------------------------------------------------------------------


class TestGetTraceSampler:
    def test_returns_none_on_import_failure(self) -> None:
        with patch.dict("sys.modules", {"hft_platform.diagnostics.trace": None}):
            result = get_trace_sampler()
            assert result is None

    def test_returns_none_on_runtime_error(self) -> None:
        """If the underlying import raises, get_trace_sampler returns None."""
        with patch(
            "builtins.__import__",
            side_effect=RuntimeError("fail"),
        ):
            result = get_trace_sampler()
            assert result is None


# -----------------------------------------------------------------------
# looks_like_md
# -----------------------------------------------------------------------


class TestLooksLikeMd:
    # --- dict cases ---

    def test_dict_with_code_key(self) -> None:
        assert looks_like_md({"code": "2330"}) is True

    def test_dict_with_symbol_key(self) -> None:
        assert looks_like_md({"symbol": "2330"}) is True

    def test_dict_with_price_field(self) -> None:
        assert looks_like_md({"price": 100}) is True

    def test_dict_with_bid_price(self) -> None:
        assert looks_like_md({"bid_price": 100}) is True

    def test_dict_with_ask_price(self) -> None:
        assert looks_like_md({"ask_price": 100}) is True

    def test_dict_with_close(self) -> None:
        assert looks_like_md({"close": 100}) is True

    def test_dict_with_bid_volume(self) -> None:
        assert looks_like_md({"bid_volume": 10}) is True

    def test_dict_with_ask_volume(self) -> None:
        assert looks_like_md({"ask_volume": 10}) is True

    def test_dict_with_buy_price(self) -> None:
        assert looks_like_md({"buy_price": 100}) is True

    def test_dict_with_sell_price(self) -> None:
        assert looks_like_md({"sell_price": 100}) is True

    def test_dict_with_ts(self) -> None:
        assert looks_like_md({"ts": 1234567890}) is True

    def test_dict_with_datetime(self) -> None:
        assert looks_like_md({"datetime": "2026-01-01"}) is True

    def test_empty_dict(self) -> None:
        assert looks_like_md({}) is False

    def test_dict_with_irrelevant_keys(self) -> None:
        assert looks_like_md({"foo": "bar", "baz": 1}) is False

    # --- None ---

    def test_none(self) -> None:
        assert looks_like_md(None) is False

    # --- object cases ---

    def test_object_with_code_and_ts(self) -> None:
        class Tick:
            code: str = "2330"
            ts: int = 123

        assert looks_like_md(Tick()) is True

    def test_object_with_price_attr(self) -> None:
        class Tick:
            bid_price: int = 100

        assert looks_like_md(Tick()) is True

    def test_object_with_close_attr(self) -> None:
        class Tick:
            close: int = 50

        assert looks_like_md(Tick()) is True

    def test_object_with_ask_volume_attr(self) -> None:
        class Tick:
            ask_volume: int = 10

        assert looks_like_md(Tick()) is True

    def test_object_without_relevant_attrs(self) -> None:
        class Other:
            foo: int = 1

        assert looks_like_md(Other()) is False

    def test_object_with_code_none(self) -> None:
        """code=None and symbol=None means has_code is False."""

        class Obj:
            code = None
            symbol = None

        assert looks_like_md(Obj()) is False

    def test_object_with_code_set(self) -> None:
        class Obj:
            code: str = "2330"
            ts: int = 123

        assert looks_like_md(Obj()) is True

    def test_string_is_not_md(self) -> None:
        assert looks_like_md("some string") is False

    def test_int_is_not_md(self) -> None:
        assert looks_like_md(42) is False

    def test_list_is_not_md(self) -> None:
        assert looks_like_md([1, 2, 3]) is False

    def test_object_with_symbol_and_datetime(self) -> None:
        class Obj:
            symbol: str = "2330"
            datetime: str = "2026-01-01"

        assert looks_like_md(Obj()) is True

    def test_object_code_only_no_time_no_price(self) -> None:
        """has_code=True but no price and no time => (has_code and (has_price or has_time)) is False."""

        class Obj:
            code: str = "2330"

        # has_code=True, has_price=False, has_time=False => False
        # but also has_price is checked first: bool(has_price or (has_code and (has_price or has_time)))
        assert looks_like_md(Obj()) is False


# -----------------------------------------------------------------------
# unwrap_md
# -----------------------------------------------------------------------


class TestUnwrapMd:
    def test_none_returns_none(self) -> None:
        assert unwrap_md(None) is None

    def test_dict_with_nested_tick(self) -> None:
        inner = {"code": "2330", "price": 100}
        outer = {"tick": inner, "other": "data"}
        assert unwrap_md(outer) is inner

    def test_dict_with_nested_bidask(self) -> None:
        inner = {"code": "2330", "bid_price": 100}
        outer = {"bidask": inner}
        assert unwrap_md(outer) is inner

    def test_tick_takes_priority_over_bidask(self) -> None:
        tick = {"code": "2330", "price": 100}
        bidask = {"code": "2330", "bid_price": 50}
        outer = {"tick": tick, "bidask": bidask}
        assert unwrap_md(outer) is tick

    def test_dict_with_non_md_nested(self) -> None:
        outer = {"tick": {"foo": "bar"}, "code": "2330"}
        result = unwrap_md(outer)
        assert result is outer

    def test_plain_md_dict_returns_itself(self) -> None:
        d: dict[str, Any] = {"code": "2330", "price": 100}
        assert unwrap_md(d) is d

    def test_object_with_tick_attr(self) -> None:
        inner = {"code": "2330", "price": 100}

        class Wrapper:
            tick = inner

        w = Wrapper()
        assert unwrap_md(w) is inner

    def test_object_with_bidask_attr(self) -> None:
        inner = {"symbol": "2330", "bid_price": 50}

        class Wrapper:
            bidask = inner

        w = Wrapper()
        assert unwrap_md(w) is inner

    def test_object_without_nested(self) -> None:
        class Obj:
            code: str = "2330"
            price: int = 100

        obj = Obj()
        assert unwrap_md(obj) is obj

    def test_dict_nested_tick_none(self) -> None:
        outer: dict[str, Any] = {"tick": None, "code": "2330", "price": 100}
        assert unwrap_md(outer) is outer

    def test_dict_bidask_not_md_falls_through(self) -> None:
        """If tick is missing and bidask is not MD, return original."""
        outer: dict[str, Any] = {"bidask": {"foo": 1}, "code": "2330"}
        assert unwrap_md(outer) is outer

    def test_object_tick_none_bidask_valid(self) -> None:
        inner = {"code": "2330", "bid_price": 50}

        class Wrapper:
            tick = None
            bidask = inner

        w = Wrapper()
        assert unwrap_md(w) is inner


# -----------------------------------------------------------------------
# summarize_md
# -----------------------------------------------------------------------


class TestSummarizeMd:
    def test_none_returns_empty_dict(self) -> None:
        assert summarize_md(None) == {}

    def test_dict_returns_keys_present_nested(self) -> None:
        d: dict[str, Any] = {"code": "2330", "price": 100, "ts": 123}
        result = summarize_md(d)
        assert "keys" in result
        assert "present" in result
        assert "nested" in result
        assert "code" in result["present"]
        assert "price" in result["present"]
        assert "ts" in result["present"]

    def test_dict_with_nested_tick(self) -> None:
        d: dict[str, Any] = {"code": "2330", "tick": {"price": 100}}
        result = summarize_md(d)
        assert "tick" in result["nested"]
        assert result["nested"]["tick"] == "dict"

    def test_dict_keys_truncated_at_20(self) -> None:
        d = {f"key_{i}": i for i in range(30)}
        result = summarize_md(d)
        assert len(result["keys"]) == 20

    def test_object_returns_attrs_and_nested(self) -> None:
        class Tick:
            code: str = "2330"
            price: int = 100
            ts: int = 123

        result = summarize_md(Tick())
        assert "attrs" in result
        assert "nested" in result
        assert "code" in result["attrs"]
        assert "price" in result["attrs"]

    def test_object_with_nested_attr(self) -> None:
        class Wrapper:
            tick = {"price": 100}

        result = summarize_md(Wrapper())
        assert "tick" in result["nested"]
        assert result["nested"]["tick"] == "dict"

    def test_empty_dict(self) -> None:
        result = summarize_md({})
        assert result["keys"] == []
        assert result["present"] == []
        assert result["nested"] == {}

    def test_dict_present_order_matches_field_tuples(self) -> None:
        """Present fields should follow the order: code fields, price fields, time fields."""
        d: dict[str, Any] = {"ts": 1, "price": 100, "code": "X"}
        result = summarize_md(d)
        # Order: code, price, ts (code_fields first, then price_fields, then time_fields)
        assert result["present"] == ["code", "price", "ts"]

    def test_object_no_relevant_attrs(self) -> None:
        class Obj:
            foo: int = 1

        result = summarize_md(Obj())
        assert result["attrs"] == []
        assert result["nested"] == {}

    def test_dict_with_bidask_nested(self) -> None:
        d: dict[str, Any] = {"bidask": [1, 2, 3]}
        result = summarize_md(d)
        assert "bidask" in result["nested"]
        assert result["nested"]["bidask"] == "list"


# -----------------------------------------------------------------------
# try_fast_extract_callback_payload
# -----------------------------------------------------------------------


class TestTryFastExtractCallbackPayload:
    def test_kwargs_with_quote_key(self) -> None:
        msg: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, payload = try_fast_extract_callback_payload(exchange="TSE", quote=msg)
        assert exchange == "TSE"
        assert payload is msg

    def test_kwargs_with_tick_key(self) -> None:
        msg: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, payload = try_fast_extract_callback_payload(tick=msg)
        assert payload is msg

    def test_kwargs_with_bidask_key(self) -> None:
        msg: dict[str, Any] = {"code": "2330", "bid_price": 50}
        exchange, payload = try_fast_extract_callback_payload(bidask=msg)
        assert payload is msg

    def test_kwargs_with_data_key(self) -> None:
        msg: dict[str, Any] = {"symbol": "2330", "close": 100}
        exchange, payload = try_fast_extract_callback_payload(data=msg)
        assert payload is msg

    def test_kwargs_with_msg_key(self) -> None:
        msg: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, payload = try_fast_extract_callback_payload(msg=msg)
        assert payload is msg

    def test_kwargs_with_nested_wrapper(self) -> None:
        inner: dict[str, Any] = {"code": "2330", "price": 100}
        wrapper: dict[str, Any] = {"tick": inner}
        exchange, payload = try_fast_extract_callback_payload(quote=wrapper)
        assert payload is inner

    def test_args_exchange_and_msg(self) -> None:
        msg: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, payload = try_fast_extract_callback_payload("TSE", msg)
        assert exchange == "TSE"
        assert payload is msg

    def test_args_msg_and_exchange(self) -> None:
        """When first arg is md and second is a string, second becomes exchange."""
        msg: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, payload = try_fast_extract_callback_payload(msg, "TSE")
        assert exchange == "TSE"
        assert payload is msg

    def test_args_single_msg(self) -> None:
        msg: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, payload = try_fast_extract_callback_payload(msg)
        assert exchange is None
        assert payload is msg

    def test_args_topic_quote_event(self) -> None:
        """Three args: (topic, event, quote) -- should find the md-like one."""
        msg: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, payload = try_fast_extract_callback_payload("TSE", "some_event", msg)
        assert exchange == "TSE"
        assert payload is msg

    def test_no_md_payload_returns_none(self) -> None:
        exchange, payload = try_fast_extract_callback_payload("hello", "world")
        assert payload is None

    def test_no_args_no_kwargs(self) -> None:
        exchange, payload = try_fast_extract_callback_payload()
        assert exchange is None
        assert payload is None

    def test_exchange_from_kwargs(self) -> None:
        msg: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, payload = try_fast_extract_callback_payload(msg, exchange="OTC")
        assert exchange == "OTC"
        assert payload is msg

    def test_kwargs_non_md_quote(self) -> None:
        """If quote kwarg is not md-like, falls through to args."""
        msg: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, payload = try_fast_extract_callback_payload(msg, quote={"foo": "bar"})
        assert payload is msg

    def test_args_three_with_exchange_object(self) -> None:
        """Exchange arg with .name attr should be detected."""

        class Exchange:
            name: str = "TSE"

        msg: dict[str, Any] = {"code": "2330", "price": 100}
        ex = Exchange()
        exchange, payload = try_fast_extract_callback_payload(ex, "event", msg)
        assert exchange is ex
        assert payload is msg

    def test_kwargs_priority_order(self) -> None:
        """quote is checked before tick in kwargs iteration."""
        quote_msg: dict[str, Any] = {"code": "Q", "price": 1}
        tick_msg: dict[str, Any] = {"code": "T", "price": 2}
        _, payload = try_fast_extract_callback_payload(quote=quote_msg, tick=tick_msg)
        assert payload is quote_msg

    def test_args_four_finds_last(self) -> None:
        """With >=3 args, tries args[-1], args[-2], args[0]."""
        msg: dict[str, Any] = {"code": "2330", "price": 100}
        exchange, payload = try_fast_extract_callback_payload("TSE", "a", "b", msg)
        assert exchange == "TSE"
        assert payload is msg

    def test_args_two_neither_md(self) -> None:
        """Two non-md args should return (None, None)."""
        exchange, payload = try_fast_extract_callback_payload(42, 99)
        assert exchange is None
        assert payload is None

    def test_single_non_md_arg(self) -> None:
        exchange, payload = try_fast_extract_callback_payload("not_md")
        assert exchange is None
        assert payload is None
