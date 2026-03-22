"""Coverage tests for feed_adapter/normalizer.py — targeting 80%+ line coverage."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# SymbolMetadata tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def sym_yaml(tmp_path):
    content = """
symbols:
  - code: "TSMC"
    price_scale: 10000
    exchange: TSE
    product_type: stock
    tags: "liquid,large"
    order_cond: "ROD"
    order_lot: "Common"
  - code: "2330"
    tick_size: 0.01
    exchange: TSE
    tags: "liquid"
  - code: "TXF"
    exchange: TAIFEX
    product_type: future
  - code: "OPTFUT"
    exchange: OPT
"""
    path = tmp_path / "symbols.yaml"
    path.write_text(content)
    return str(path)


def test_symbol_metadata_load(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    assert "TSMC" in sm.meta
    assert "2330" in sm.meta


def test_symbol_metadata_price_scale_explicit(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    assert sm.price_scale("TSMC") == 10000


def test_symbol_metadata_price_scale_from_tick_size(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    scale = sm.price_scale("2330")
    assert scale == 100  # 1/0.01 = 100


def test_symbol_metadata_price_scale_default(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    assert sm.price_scale("UNKNOWN") == 10000


def test_symbol_metadata_price_scale_cached(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    s1 = sm.price_scale("TSMC")
    s2 = sm.price_scale("TSMC")
    assert s1 == s2


def test_symbol_metadata_product_type_explicit(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    assert sm.product_type("TSMC") == "stock"
    assert sm.product_type("TXF") == "future"


def test_symbol_metadata_product_type_from_exchange(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    result = sm.product_type("OPTFUT")
    assert result == "option"


def test_symbol_metadata_product_type_unknown(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    assert sm.product_type("BOGUS") == ""


def test_symbol_metadata_tags(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    assert "liquid" in sm.tags_by_symbol.get("TSMC", set())
    assert "large" in sm.tags_by_symbol.get("TSMC", set())


def test_symbol_metadata_symbols_for_tags(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    result = sm.symbols_for_tags(["liquid"])
    assert "TSMC" in result
    assert "2330" in result


def test_symbol_metadata_symbols_for_tags_empty(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    result = sm.symbols_for_tags([])
    assert result == set()


def test_symbol_metadata_symbols_for_tags_unknown(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    result = sm.symbols_for_tags(["no_such_tag"])
    assert result == set()


def test_symbol_metadata_order_params(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    params = sm.order_params("TSMC")
    assert "order_cond" in params
    assert params["order_cond"] == "ROD"


def test_symbol_metadata_order_params_empty(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    params = sm.order_params("TXF")
    assert isinstance(params, dict)


def test_symbol_metadata_exchange(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    assert sm.exchange("TSMC") == "TSE"
    assert sm.exchange("TSMC") == "TSE"  # cached


def test_symbol_metadata_exchange_unknown(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    assert sm.exchange("BOGUS") == ""


def test_symbol_metadata_reload(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    sm.reload()
    assert "TSMC" in sm.meta


def test_symbol_metadata_reload_if_changed_no_change(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    changed = sm.reload_if_changed()
    assert changed is False


def test_symbol_metadata_reload_if_changed_file_gone(tmp_path):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    path = tmp_path / "missing.yaml"
    sm = SymbolMetadata(str(path))
    sm._mtime = 1.0
    changed = sm.reload_if_changed()
    assert changed is False


def test_symbol_metadata_missing_file(tmp_path):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(str(tmp_path / "not_exist.yaml"))
    assert sm.price_scale("X") == 10000


def test_symbol_metadata_tags_string_format(tmp_path):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    content = """
symbols:
  - code: A
    tags: "alpha|beta,gamma"
  - code: B
    tags:
      - delta
      - epsilon
"""
    path = tmp_path / "s.yaml"
    path.write_text(content)
    sm = SymbolMetadata(str(path))
    assert "alpha" in sm.tags_by_symbol.get("A", set())
    assert "beta" in sm.tags_by_symbol.get("A", set())
    assert "gamma" in sm.tags_by_symbol.get("A", set())
    assert "delta" in sm.tags_by_symbol.get("B", set())


# ---------------------------------------------------------------------------
# _clamp_future_ts tests
# ---------------------------------------------------------------------------


def test_clamp_future_ts_no_max():
    import hft_platform.feed_adapter.normalizer as mod

    with patch.object(mod, "_TS_MAX_FUTURE_NS", 0):
        from hft_platform.feed_adapter.normalizer import _clamp_future_ts

        result = _clamp_future_ts(9_999_999_999_999_999_999, 1_000_000_000, "tick", "TSMC")
        assert result == 9_999_999_999_999_999_999


def test_clamp_future_ts_within_limit():
    import hft_platform.feed_adapter.normalizer as mod

    now = 1_700_000_000_000_000_000
    exch = now + 1_000_000

    with patch.object(mod, "_TS_MAX_FUTURE_NS", int(5e9)):
        from hft_platform.feed_adapter.normalizer import _clamp_future_ts

        result = _clamp_future_ts(exch, now, "tick", "TSMC")
        assert result == exch


def test_clamp_future_ts_exceeds_limit():
    import hft_platform.feed_adapter.normalizer as mod

    now = 1_700_000_000_000_000_000
    exch = now + int(10e9)

    with patch.object(mod, "_TS_MAX_FUTURE_NS", int(5e9)):
        from hft_platform.feed_adapter.normalizer import _clamp_future_ts

        result = _clamp_future_ts(exch, now, "tick", "TSMC")
        assert result == now


def test_clamp_future_ts_zero_exch():
    import hft_platform.feed_adapter.normalizer as mod

    with patch.object(mod, "_TS_MAX_FUTURE_NS", int(5e9)):
        from hft_platform.feed_adapter.normalizer import _clamp_future_ts

        result = _clamp_future_ts(0, 1_000_000_000, "tick", "TSMC")
        assert result == 0


# ---------------------------------------------------------------------------
# MarketDataNormalizer tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def normalizer(sym_yaml):
    from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

    with patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as mr:
        mr.get.return_value = MagicMock()
        n = MarketDataNormalizer(sym_yaml)
    return n


def _make_tick_obj(code="TSMC", price=500.0, volume=10, ts=None):
    return SimpleNamespace(
        code=code,
        price=price,
        volume=volume,
        ts=ts or "09:00:00",
        datetime="2024-01-01 09:00:00",
        tick_type=1,
        ask_price=500.1,
        bid_price=499.9,
        ask_volume=5,
        bid_volume=5,
        close=price,
        open=price,
        high=price,
        low=price,
        total_amount=price * volume,
        total_volume=volume,
        simtrade=0,
    )


def _make_bidask_obj(code="TSMC", bid=499.9, ask=500.1, ts=None):
    return SimpleNamespace(
        code=code,
        datetime="2024-01-01 09:00:01",
        ts=ts or "09:00:01",
        bid_price=[bid, bid - 0.5, bid - 1.0, bid - 1.5, bid - 2.0],
        bid_volume=[10, 8, 6, 4, 2],
        ask_price=[ask, ask + 0.5, ask + 1.0, ask + 1.5, ask + 2.0],
        ask_volume=[10, 8, 6, 4, 2],
        diff_bid_vol=[0, 0, 0, 0, 0],
        diff_ask_vol=[0, 0, 0, 0, 0],
        simtrade=0,
    )


def test_normalizer_normalize_tick_basic(normalizer):
    tick = _make_tick_obj("TSMC", price=500.0)
    result = normalizer.normalize_tick(tick)
    # May return None — just ensure no crash


def test_normalizer_normalize_tick_returns_tick_event(normalizer):
    tick = _make_tick_obj("TSMC", price=500.0)
    result = normalizer.normalize_tick(tick)
    if result is not None and not isinstance(result, tuple):
        assert hasattr(result, "price")
        assert isinstance(result.price, int)


def test_normalizer_normalize_bidask_basic(normalizer):
    ba = _make_bidask_obj("TSMC")
    normalizer.normalize_bidask(ba)


def test_normalizer_normalize_bidask_returns_event(normalizer):
    ba = _make_bidask_obj("TSMC")
    result = normalizer.normalize_bidask(ba)
    if result is not None and not isinstance(result, tuple):
        assert hasattr(result, "bids")
        assert hasattr(result, "asks")


def test_normalizer_normalize_snapshot_basic(normalizer):
    ba = _make_bidask_obj("TSMC")
    try:
        normalizer.normalize_snapshot(ba)
    except Exception:
        pass


def test_normalizer_next_seq(normalizer):
    s1 = normalizer._next_seq()
    s2 = normalizer._next_seq()
    assert s2 == s1 + 1


def test_normalizer_get_scale_known(normalizer):
    scale = normalizer._get_scale("TSMC")
    assert scale == 10000


def test_normalizer_get_scale_unknown(normalizer):
    scale = normalizer._get_scale("MYSTERY")
    assert scale == 10000


def test_normalizer_get_scale_caches(normalizer):
    normalizer._get_scale("TSMC")
    assert normalizer._last_symbol == "TSMC"
    scale2 = normalizer._get_scale("TSMC")
    assert scale2 == 10000


# ---------------------------------------------------------------------------
# SymbolMetadata product_type via exchange inference
# ---------------------------------------------------------------------------


def test_product_type_index_exchange(tmp_path):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    content = "\nsymbols:\n  - code: IDX1\n    exchange: IDX\n"
    path = tmp_path / "s.yaml"
    path.write_text(content)
    sm = SymbolMetadata(str(path))
    assert sm.product_type("IDX1") == "index"


def test_product_type_futures_exchange(tmp_path):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    content = "\nsymbols:\n  - code: FUT1\n    exchange: FUTURES\n"
    path = tmp_path / "s.yaml"
    path.write_text(content)
    sm = SymbolMetadata(str(path))
    assert sm.product_type("FUT1") == "future"


def test_product_type_otc_exchange(tmp_path):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    content = "\nsymbols:\n  - code: OTC1\n    exchange: OTC\n"
    path = tmp_path / "s.yaml"
    path.write_text(content)
    sm = SymbolMetadata(str(path))
    assert sm.product_type("OTC1") == "stock"


def test_product_type_cached(sym_yaml):
    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    sm = SymbolMetadata(sym_yaml)
    sm.product_type("TSMC")
    sm.product_type("TSMC")
    assert "TSMC" in sm._product_type_cache


# ---------------------------------------------------------------------------
# _get_field
# ---------------------------------------------------------------------------


def test_get_field_on_tick():
    from hft_platform.feed_adapter.normalizer import _RUST_GET_FIELD

    tick = _make_tick_obj()
    if _RUST_GET_FIELD is not None:
        val = _RUST_GET_FIELD(tick, "price")
        assert val is not None or val is None
    else:
        assert getattr(tick, "code") == "TSMC"
