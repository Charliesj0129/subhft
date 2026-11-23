"""fubon_neo 2.5.0 enums are pyo3 classes without .name; str() is "Type.Member"."""

from __future__ import annotations

from hft_platform.feed_adapter.fubon.order_codec import _enum_name, translate_adapter_order_kwargs


class _Pyo3LikeEnum:
    """Mimics fubon_neo.constant enums: no .name/.value, str() -> 'Type.Member'."""

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text = text

    def __str__(self) -> str:
        return self._text


def test_enum_name_strips_type_prefix_for_pyo3_enum() -> None:
    assert _enum_name(_Pyo3LikeEnum("PriceType.Limit")) == "Limit"
    assert _enum_name(_Pyo3LikeEnum("TimeInForce.ROD")) == "ROD"


def test_enum_name_keeps_plain_string() -> None:
    assert _enum_name("LMT") == "LMT"


def test_translate_accepts_pyo3_price_type_limit() -> None:
    out = translate_adapter_order_kwargs(
        {
            "contract_code": "TXFL6",
            "action": "Buy",
            "price": 36395.0,
            "qty": 1,
            "tif": "ROD",
            "custom_field": "592a62",
            "price_type": _Pyo3LikeEnum("PriceType.Limit"),
            "product_type": "future",
        },
        now_ns=lambda: 1_763_870_400_000_000_000,
    )
    assert out["price_type"] == "LMT"
    assert out["price"] == 363_950_000
