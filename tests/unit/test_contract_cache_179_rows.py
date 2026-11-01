"""Contract-cache rows built from shioaji 1.7.9 contracts.

THESHOW, 2026-10-28 to 11-01: every hourly contract rebuild on all four pooled
facades died with ``Object of type date is not JSON serializable`` (332 times),
so ``config/contracts.json`` -- the file the universe roll reads -- stayed at
the last 1.5.6 write. Two 1.7.9 differences are behind it:

* ``delivery_date`` is a ``datetime.date`` (1.5.6: ``"2026/11/18"``);
* ``FuturesInfo``/``OptionInfo`` have ``root`` but no ``category``/``symbol``,
  so the row carried no root and ``ContractIndex`` fell back to
  ``derive_root("TMFK6") == "TMFK"``. Fixing only the date would have written a
  cache in which ``TMF@front`` matches nothing.

The leaf fakes below carry exactly the 1.7.9 attribute set (``_core.pyi``
``FuturesInfo``/``OptionInfo`` plus the base fields), checked against the
partial ``contracts.json.tmp`` the failing dump left on THESHOW: the first
future row there had ``code``/``name``/``exchange``/``type`` and no ``root``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hft_platform.config import _symbols_types as symbols_types
from hft_platform.config._symbols_contracts import load_contract_cache, write_contract_cache
from hft_platform.config.symbols import build_symbols
from hft_platform.feed_adapter.shioaji.contracts_runtime import _normalize_contract_row


class _FuturesInfo179:
    """Attribute set of a 1.7.9 ``FuturesInfo`` leaf: no ``category``, no ``symbol``."""

    __slots__ = ("code", "exchange", "security_type", "target_code", "name", "root", "delivery_month", "delivery_date")

    def __init__(self, code: str, root: str, delivery: dt.date, target_code: str = "") -> None:
        self.code = code
        self.exchange = "TAIFEX"
        self.security_type = "FUT"
        self.target_code = target_code
        self.name = f"{root} {delivery:%Y%m}"
        self.root = root
        self.delivery_month = f"{delivery:%Y%m}"
        self.delivery_date = delivery


class _OptionInfo179:
    __slots__ = ("code", "exchange", "name", "root", "delivery_date", "strike_price", "option_right")

    def __init__(self, code: str, root: str, delivery: dt.date, strike: float, right: str) -> None:
        self.code = code
        self.exchange = "TAIFEX"
        self.name = code
        self.root = root
        self.delivery_date = delivery
        self.strike_price = strike
        self.option_right = _Right(right)


class _Right:
    """Stands in for ``shioaji._core.OptionRight``: ``.value`` is ``"C"``/``"P"``."""

    def __init__(self, value: str) -> None:
        self.value = value


class _Contract156:
    """1.5.6 leaf: ``category`` holds the root, ``delivery_date`` is a string."""

    def __init__(self) -> None:
        self.code = "TMFK6"
        self.symbol = "TMF202611"
        self.name = "TMF11"
        self.category = "TMF"
        self.delivery_date = "2026/11/18"


class _BareFutureLeaf:
    """A future leaf exposing no root-bearing attribute at all."""

    __slots__ = ("code", "delivery_date")

    def __init__(self) -> None:
        self.code = "TMFK6"
        self.delivery_date = dt.date(2026, 11, 18)


def _tmf_rows_179() -> list[dict]:
    contracts = [
        _FuturesInfo179("TMFK6", "TMF", dt.date(2026, 11, 18)),
        _FuturesInfo179("TMFL6", "TMF", dt.date(2026, 12, 16)),
        _FuturesInfo179("TMFA7", "TMF", dt.date(2027, 1, 20)),
        _FuturesInfo179("TMFR1", "TMF", dt.date(2026, 11, 18), target_code="TMFK6"),
        _FuturesInfo179("TMFR2", "TMF", dt.date(2026, 12, 16), target_code="TMFL6"),
    ]
    return [_normalize_contract_row(c, "TAIFEX", "future") for c in contracts]


def test_normalize_contract_row_reads_179_root_when_category_is_absent():
    row = _normalize_contract_row(_FuturesInfo179("TMFK6", "TMF", dt.date(2026, 11, 18)), "TAIFEX", "future")

    assert row["root"] == "TMF"
    assert row["delivery_date"] == "2026/11/18"
    assert "symbol" not in row


def test_normalize_contract_row_keeps_the_156_category_root_and_string_date():
    row = _normalize_contract_row(_Contract156(), "TAIFEX", "future")

    assert row == {
        "code": "TMFK6",
        "symbol": "TMF202611",
        "name": "TMF11",
        "exchange": "TAIFEX",
        "type": "future",
        "root": "TMF",
        "delivery_date": "2026/11/18",
    }


def test_normalize_contract_row_reads_179_option_root_strike_and_right():
    row = _normalize_contract_row(
        _OptionInfo179("TX141400K6", "TX1", dt.date(2026, 11, 4), 41400.0, "C"), "TAIFEX", "option"
    )

    # derive_root would read this weekly code as "TX".
    assert row["root"] == "TX1"
    assert row["strike"] == 41400.0
    assert row["right"] == "C"
    assert row["delivery_date"] == "2026/11/04"


def test_normalize_contract_row_derives_futures_root_from_code_when_no_attribute_has_it():
    row = _normalize_contract_row(_BareFutureLeaf(), "TAIFEX", "future")

    assert row["root"] == "TMF"
    assert row["delivery_date"] == "2026/11/18"


def test_write_contract_cache_serialises_sdk_dates_in_the_156_slash_form(tmp_path: Path):
    path = tmp_path / "contracts.json"

    write_contract_cache(
        [{"code": "TMFK6", "delivery_date": dt.date(2026, 11, 18), "update_date": dt.datetime(2026, 10, 30, 8, 0)}],
        str(path),
    )

    row = json.loads(path.read_text(encoding="utf-8"))["contracts"][0]
    assert row["delivery_date"] == "2026/11/18"
    assert row["update_date"] == "2026/10/30"


def test_write_contract_cache_rejects_an_unknown_type_and_leaves_the_old_cache_and_no_temp(tmp_path: Path):
    path = tmp_path / "contracts.json"
    write_contract_cache([{"code": "OLD"}], str(path))
    before = path.read_text(encoding="utf-8")

    with pytest.raises(TypeError, match="not JSON serializable"):
        write_contract_cache([{"code": "NEW", "weird": object()}], str(path))

    assert path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "contracts.json.tmp").exists()


def test_universe_build_from_a_179_cache_rolls_tmf_front_and_next_after_delivery(tmp_path: Path):
    cache = tmp_path / "contracts.json"
    write_contract_cache(_tmf_rows_179(), str(cache))
    list_path = tmp_path / "symbols.list"
    list_path.write_text("TMF@front exchange=FUT\nTMF@next exchange=FUT\n", encoding="utf-8")
    index = load_contract_cache(str(cache), metrics_path="")
    assert index is not None

    with patch.object(symbols_types, "exchange_today", return_value=dt.date(2026, 11, 2)):
        before_roll = build_symbols(str(list_path), index)
    with patch.object(symbols_types, "exchange_today", return_value=dt.date(2026, 11, 19)):
        after_roll = build_symbols(str(list_path), index)

    assert before_roll.errors == []
    assert [s["code"] for s in before_roll.symbols] == ["TMFK6", "TMFL6"]
    assert after_roll.errors == []
    assert [s["code"] for s in after_roll.symbols] == ["TMFL6", "TMFA7"]
