"""Tests for group attribute support in symbol parsing."""

from hft_platform.config._symbols_parsing import parse_kv_tokens


def test_parse_kv_tokens_group_integer():
    result = parse_kv_tokens(["group=2"])
    assert result["group"] == 2


def test_parse_kv_tokens_group_zero():
    result = parse_kv_tokens(["group=0"])
    assert result["group"] == 0


def test_parse_kv_tokens_group_invalid_ignored():
    result = parse_kv_tokens(["group=abc"])
    assert "group" not in result
    assert "_invalid" in result


def test_parse_kv_tokens_group_with_other_attrs():
    result = parse_kv_tokens(["exchange=TSE", "group=1", "price_scale=10000"])
    assert result["exchange"] == "TSE"
    assert result["group"] == 1
    assert result["price_scale"] == 10000
