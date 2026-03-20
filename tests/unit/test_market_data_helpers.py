"""Tests for pure helper functions in services/market_data.py."""

from unittest.mock import MagicMock

import pytest

from hft_platform.services.market_data import (
    FeedState,
    _env_int,
    _looks_like_md,
    _obs_policy,
    _try_fast_extract_callback_payload,
    _unwrap_md,
)

# ---------------------------------------------------------------------------
# _looks_like_md
# ---------------------------------------------------------------------------


class TestLooksLikeMd:
    def test_none_returns_false(self) -> None:
        assert _looks_like_md(None) is False

    def test_dict_with_code_returns_true(self) -> None:
        assert _looks_like_md({"code": "2330"}) is True

    def test_dict_with_bid_price_returns_true(self) -> None:
        assert _looks_like_md({"bid_price": 100}) is True

    def test_dict_with_ts_returns_true(self) -> None:
        assert _looks_like_md({"ts": 1234567890}) is True

    def test_object_with_code_and_price_returns_true(self) -> None:
        obj = MagicMock(spec=[])
        obj.code = "2330"
        obj.bid_price = 100
        assert _looks_like_md(obj) is True

    def test_empty_dict_returns_false(self) -> None:
        assert _looks_like_md({}) is False


# ---------------------------------------------------------------------------
# _unwrap_md
# ---------------------------------------------------------------------------


class TestUnwrapMd:
    def test_none_returns_none(self) -> None:
        assert _unwrap_md(None) is None

    def test_dict_with_nested_tick(self) -> None:
        tick = {"code": "2330", "price": 500}
        wrapper = {"tick": tick}
        assert _unwrap_md(wrapper) is tick

    def test_dict_with_nested_bidask(self) -> None:
        bidask = {"code": "2330", "bid_price": 100, "ask_price": 101}
        wrapper = {"bidask": bidask}
        assert _unwrap_md(wrapper) is bidask

    def test_object_without_nested_returns_self(self) -> None:
        obj = MagicMock(spec=[])
        obj.code = "2330"
        obj.price = 500
        result = _unwrap_md(obj)
        assert result is obj


# ---------------------------------------------------------------------------
# _try_fast_extract_callback_payload
# ---------------------------------------------------------------------------


class TestTryFastExtractCallbackPayload:
    def test_kwargs_with_quote_key(self) -> None:
        quote = {"code": "2330", "price": 500}
        exchange, payload = _try_fast_extract_callback_payload(exchange="TSE", quote=quote)
        assert exchange == "TSE"
        assert payload is quote

    def test_two_positional_args(self) -> None:
        msg = {"code": "2330", "bid_price": 100}
        exchange, payload = _try_fast_extract_callback_payload("TSE", msg)
        assert exchange == "TSE"
        assert payload is msg

    def test_no_valid_payload_returns_none(self) -> None:
        exchange, payload = _try_fast_extract_callback_payload(42)
        assert exchange is None
        assert payload is None


# ---------------------------------------------------------------------------
# _env_int
# ---------------------------------------------------------------------------


class TestEnvInt:
    def test_valid_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_TEST_INT", "42")
        assert _env_int("HFT_TEST_INT", 10) == 42

    def test_missing_env_var_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_TEST_MISSING", raising=False)
        assert _env_int("HFT_TEST_MISSING", 5) == 5

    def test_invalid_env_var_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_TEST_BAD", "not_a_number")
        assert _env_int("HFT_TEST_BAD", 7) == 7


# ---------------------------------------------------------------------------
# _obs_policy
# ---------------------------------------------------------------------------


class TestObsPolicy:
    def test_valid_policy_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_OBS_POLICY", "minimal")
        assert _obs_policy() == "minimal"

    def test_invalid_policy_falls_back_to_balanced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_OBS_POLICY", "turbo")
        assert _obs_policy() == "balanced"


# ---------------------------------------------------------------------------
# FeedState enum
# ---------------------------------------------------------------------------


class TestFeedState:
    def test_all_expected_values_exist(self) -> None:
        expected = {"INIT", "CONNECTING", "SNAPSHOTTING", "CONNECTED", "DISCONNECTED", "RECOVERING"}
        actual = {s.value for s in FeedState}
        assert actual == expected
