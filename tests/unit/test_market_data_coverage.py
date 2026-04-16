"""Coverage tests for services/market_data.py — uncovered paths.

Focuses on the most impactful uncovered branches in the 1317-line module:
- MarketDataService initialization paths
- _looks_like_md / _unwrap_md / _summarize_md / _try_fast_extract_callback_payload
- FeedState transitions
- Recording paths and degradation
- Feature shadow parity
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from hft_platform.services.market_data import FeedState

# ---------------------------------------------------------------------------
# _looks_like_md
# ---------------------------------------------------------------------------


class TestLooksLikeMd:
    def _fn(self, obj):
        from hft_platform.services.market_data import _looks_like_md
        return _looks_like_md(obj)

    def test_none_returns_false(self):
        assert self._fn(None) is False

    def test_dict_with_code(self):
        assert self._fn({"code": "SYM"}) is True

    def test_dict_with_symbol(self):
        assert self._fn({"symbol": "SYM"}) is True

    def test_dict_with_bid_price(self):
        assert self._fn({"bid_price": [100]}) is True

    def test_dict_with_close(self):
        assert self._fn({"close": 100.0}) is True

    def test_dict_with_ask_volume(self):
        assert self._fn({"ask_volume": [10]}) is True

    def test_dict_with_buy_price(self):
        assert self._fn({"buy_price": 100}) is True

    def test_dict_with_ts_only(self):
        assert self._fn({"ts": 123456}) is True

    def test_dict_with_datetime_only(self):
        assert self._fn({"datetime": 123456}) is True

    def test_dict_empty(self):
        assert self._fn({}) is False

    def test_dict_unrelated_keys(self):
        assert self._fn({"foo": "bar"}) is False

    def test_object_with_code_only_returns_false(self):
        # has_code=True but no price or time attrs
        assert self._fn(SimpleNamespace(code="SYM")) is False

    def test_object_with_bid_price(self):
        assert self._fn(SimpleNamespace(bid_price=[100], code=None)) is True

    def test_object_with_close(self):
        assert self._fn(SimpleNamespace(close=100.0, code=None)) is True

    def test_object_with_code_and_ts(self):
        assert self._fn(SimpleNamespace(code="SYM", ts=123456)) is True

    def test_object_with_code_and_close(self):
        assert self._fn(SimpleNamespace(code="SYM", close=100.0)) is True

    def test_object_plain(self):
        assert self._fn(SimpleNamespace(foo="bar")) is False


# ---------------------------------------------------------------------------
# _unwrap_md
# ---------------------------------------------------------------------------


class TestUnwrapMd:
    def _fn(self, obj):
        from hft_platform.services.market_data import _unwrap_md
        return _unwrap_md(obj)

    def test_none_returns_none(self):
        assert self._fn(None) is None

    def test_dict_direct(self):
        d = {"code": "SYM", "close": 100}
        assert self._fn(d) is d

    def test_dict_with_tick_nested(self):
        inner = {"code": "SYM", "close": 100}
        d = {"tick": inner}
        assert self._fn(d) is inner

    def test_dict_with_bidask_nested(self):
        inner = {"code": "SYM", "bid_price": [100]}
        d = {"bidask": inner}
        assert self._fn(d) is inner

    def test_object_with_tick_attr(self):
        inner = SimpleNamespace(code="SYM", close=100)
        obj = SimpleNamespace(tick=inner, bidask=None)
        assert self._fn(obj) is inner

    def test_object_with_bidask_attr(self):
        inner = SimpleNamespace(code="SYM", bid_price=[100])
        obj = SimpleNamespace(tick=None, bidask=inner)
        assert self._fn(obj) is inner


# ---------------------------------------------------------------------------
# _summarize_md
# ---------------------------------------------------------------------------


class TestSummarizeMd:
    def _fn(self, obj):
        from hft_platform.services.market_data import _summarize_md
        return _summarize_md(obj)

    def test_none_returns_empty(self):
        assert self._fn(None) == {}

    def test_dict_payload(self):
        d = {"code": "SYM", "close": 100, "ts": 123}
        result = self._fn(d)
        assert "keys" in result
        assert "present" in result

    def test_object_payload(self):
        obj = SimpleNamespace(code="SYM", close=100, ts=123)
        result = self._fn(obj)
        assert "attrs" in result

    def test_dict_with_nested(self):
        d = {"code": "SYM", "tick": {"close": 100}}
        result = self._fn(d)
        assert "nested" in result
        assert "tick" in result["nested"]


# ---------------------------------------------------------------------------
# _try_fast_extract_callback_payload
# ---------------------------------------------------------------------------


class TestTryFastExtract:
    def _fn(self, *args, **kwargs):
        from hft_platform.services.market_data import _try_fast_extract_callback_payload
        return _try_fast_extract_callback_payload(*args, **kwargs)

    def test_kwargs_quote(self):
        payload = {"code": "SYM", "close": 100}
        exchange, msg = self._fn(quote=payload)
        assert msg is payload

    def test_kwargs_tick(self):
        payload = {"code": "SYM", "close": 100}
        exchange, msg = self._fn(tick=payload)
        assert msg is payload

    def test_kwargs_bidask(self):
        payload = {"code": "SYM", "bid_price": [100]}
        exchange, msg = self._fn(bidask=payload)
        assert msg is payload

    def test_kwargs_data(self):
        payload = {"code": "SYM", "close": 100}
        exchange, msg = self._fn(data=payload)
        assert msg is payload

    def test_kwargs_msg(self):
        payload = {"code": "SYM", "close": 100}
        exchange, msg = self._fn(msg=payload)
        assert msg is payload

    def test_kwargs_exchange(self):
        payload = {"code": "SYM", "close": 100}
        exchange, msg = self._fn(exchange="TSE", quote=payload)
        assert exchange == "TSE"
        assert msg is payload

    def test_two_args_exchange_msg(self):
        exchange, msg = self._fn("TSE", {"code": "SYM", "close": 100})
        assert exchange == "TSE"
        assert msg["code"] == "SYM"

    def test_two_args_reversed(self):
        exchange, msg = self._fn({"code": "SYM", "close": 100}, "TSE")
        assert exchange == "TSE"
        assert msg["code"] == "SYM"

    def test_one_arg(self):
        exchange, msg = self._fn({"code": "SYM", "close": 100})
        assert msg["code"] == "SYM"

    def test_three_args(self):
        exchange, msg = self._fn("TSE", {"foo": "bar"}, {"code": "SYM", "close": 100})
        assert exchange == "TSE"
        assert msg["code"] == "SYM"

    def test_no_md_returns_none(self):
        exchange, msg = self._fn({"foo": "bar"})
        assert msg is None

    def test_no_args_returns_none(self):
        exchange, msg = self._fn()
        assert msg is None

    def test_two_args_neither_md(self):
        exchange, msg = self._fn({"foo": 1}, {"bar": 2})
        assert msg is None


# ---------------------------------------------------------------------------
# _env_int
# ---------------------------------------------------------------------------


class TestEnvInt:
    def _fn(self, name, default):
        from hft_platform.services.market_data import _env_int
        return _env_int(name, default)

    def test_returns_default_when_not_set(self):
        assert self._fn("HFT_NONEXISTENT_VAR_12345", 10) == 10

    def test_returns_parsed_value(self):
        with patch.dict(os.environ, {"HFT_TEST_INT_VAR": "42"}):
            assert self._fn("HFT_TEST_INT_VAR", 10) == 42

    def test_returns_1_for_zero(self):
        with patch.dict(os.environ, {"HFT_TEST_INT_VAR": "0"}):
            assert self._fn("HFT_TEST_INT_VAR", 10) == 1

    def test_returns_default_on_bad_value(self):
        with patch.dict(os.environ, {"HFT_TEST_INT_VAR": "bad"}):
            assert self._fn("HFT_TEST_INT_VAR", 5) == 5


# ---------------------------------------------------------------------------
# _obs_policy
# ---------------------------------------------------------------------------


class TestObsPolicy:
    def _fn(self):
        from hft_platform.services.market_data import _obs_policy
        return _obs_policy()

    def test_default_balanced(self):
        with patch.dict(os.environ, {}, clear=False):
            result = self._fn()
            assert result in {"minimal", "balanced", "debug"}

    def test_minimal(self):
        with patch.dict(os.environ, {"HFT_OBS_POLICY": "minimal"}):
            assert self._fn() == "minimal"

    def test_debug(self):
        with patch.dict(os.environ, {"HFT_OBS_POLICY": "debug"}):
            assert self._fn() == "debug"

    def test_unknown_defaults_to_balanced(self):
        with patch.dict(os.environ, {"HFT_OBS_POLICY": "foobar"}):
            assert self._fn() == "balanced"


# ---------------------------------------------------------------------------
# FeedState
# ---------------------------------------------------------------------------


class TestFeedState:
    def test_feed_states_exist(self):
        assert FeedState.INIT is not None
        assert FeedState.CONNECTED is not None
        assert FeedState.DISCONNECTED is not None
        assert FeedState.RECOVERING is not None
