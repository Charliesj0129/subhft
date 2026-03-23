"""Tests for CJK-aware truncation and contract month labels."""

from hft_platform.monitor._renderer import format_contract_name, truncate_display

# ---- truncate_display ---- #


def test_ascii_no_truncation():
    assert truncate_display("hello", 10) == "hello"


def test_cjk_fits():
    # 4 CJK chars (8 cols) + "06" (2 cols) = 10 cols, fits in max_width=10
    assert truncate_display("\u81fa\u80a1\u671f\u8ca806", 10) == "\u81fa\u80a1\u671f\u8ca806"


def test_cjk_truncation():
    result = truncate_display("\u81fa\u80a1\u671f\u8ca806\u6708\u4efd", 10)
    assert result.endswith("\u2026")


def test_empty_string():
    assert truncate_display("", 10) == ""


def test_short_max():
    result = truncate_display("\u81fa\u80a1", 3)
    assert result.endswith("\u2026")


# ---- format_contract_name ---- #


def test_txf_d6():
    assert format_contract_name("TXFD6", "\u81fa\u80a1\u671f\u8ca806") == "\u53f0\u6307\u671f 04\u6708"


def test_mxf_d6():
    assert format_contract_name("MXFD6", "\u5c0f\u578b\u81fa\u630706") == "\u5c0f\u53f0\u6307 04\u6708"


def test_tmf_d6():
    assert format_contract_name("TMFD6", "\u5fae\u578b\u81fa\u6307\u671f\u8ca8") == "\u5fae\u53f0\u6307 04\u6708"


def test_unknown_product_falls_back():
    result = format_contract_name("XYZAB", "\u67d0\u67d0\u5546\u54c1\u540d\u7a31\u5f88\u9577")
    assert "\u2026" in result or len(result) <= 12


def test_month_a_is_january():
    assert format_contract_name("TXFA6", "\u81fa\u80a1\u671f\u8ca8") == "\u53f0\u6307\u671f 01\u6708"


def test_month_l_is_december():
    assert format_contract_name("TXFL6", "\u81fa\u80a1\u671f\u8ca8") == "\u53f0\u6307\u671f 12\u6708"
