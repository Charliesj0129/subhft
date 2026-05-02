"""Unit tests for HFT-P004 (no float for money fields) in scripts/check_discipline.py."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

# scripts/ is not a Python package; load the module by file path.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_discipline.py"
_spec = importlib.util.spec_from_file_location("check_discipline", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["check_discipline"] = _mod
_spec.loader.exec_module(_mod)

check_no_float_money = _mod.check_no_float_money
Severity = _mod.Severity


def _scan(source: str, path: str) -> list:
    tree = ast.parse(source)
    return check_no_float_money(tree, Path(path))


# ---- positive: violations expected ----------------------------------------


def test_class_attr_price_float_in_contracts_is_flagged() -> None:
    src = "class Q:\n    price: float = 0.0\n"
    vs = _scan(src, "src/hft_platform/contracts/order.py")
    assert len(vs) == 1
    assert vs[0].rule_id == "HFT-P004"
    assert vs[0].severity == Severity.HIGH


def test_function_param_balance_float_in_risk_is_flagged() -> None:
    src = "def update(balance: float) -> None: ...\n"
    vs = _scan(src, "src/hft_platform/risk/engine.py")
    assert len(vs) == 1
    assert vs[0].rule_id == "HFT-P004"


def test_optional_float_pnl_in_execution_is_flagged() -> None:
    src = "from typing import Optional\nclass F:\n    pnl: Optional[float] = None\n"
    vs = _scan(src, "src/hft_platform/execution/context.py")
    assert len(vs) == 1


def test_pep604_union_fee_in_order_is_flagged() -> None:
    src = "class O:\n    fee: float | None = None\n"
    vs = _scan(src, "src/hft_platform/order/intent.py")
    assert len(vs) == 1


def test_suffix_pattern_limit_price_is_flagged() -> None:
    src = "class O:\n    limit_price: float = 0.0\n"
    vs = _scan(src, "src/hft_platform/order/intent.py")
    assert len(vs) == 1


def test_interior_pattern_max_loss_amount_is_flagged() -> None:
    src = "class R:\n    max_loss_amount: float = 0.0\n"
    vs = _scan(src, "src/hft_platform/risk/limits.py")
    assert len(vs) == 1


# ---- negative: should NOT be flagged --------------------------------------


def test_int_price_is_not_flagged() -> None:
    src = "class Q:\n    price: int = 0\n"
    vs = _scan(src, "src/hft_platform/contracts/order.py")
    assert vs == []


def test_decimal_price_is_not_flagged() -> None:
    src = "from decimal import Decimal\nclass Q:\n    price: Decimal = Decimal('0')\n"
    vs = _scan(src, "src/hft_platform/contracts/order.py")
    assert vs == []


def test_non_money_float_field_is_not_flagged() -> None:
    src = "class M:\n    latency_seconds: float = 0.0\n    ratio: float = 0.0\n"
    vs = _scan(src, "src/hft_platform/risk/engine.py")
    assert vs == []


def test_money_float_outside_money_domain_is_not_flagged() -> None:
    src = "class S:\n    price: float = 0.0\n"
    # alpha/ is outside the money-precision domain
    vs = _scan(src, "src/hft_platform/alpha/scorecard.py")
    assert vs == []


def test_stress_test_file_is_exempt() -> None:
    src = "def scenario(underlying_price: float) -> float: return underlying_price\n"
    vs = _scan(src, "src/hft_platform/risk/stress_test.py")
    assert vs == []


def test_test_file_is_exempt() -> None:
    src = "class T:\n    price: float = 0.0\n"
    vs = _scan(src, "tests/unit/test_something.py")
    assert vs == []


def test_money_field_typed_as_str_is_not_flagged() -> None:
    src = "class Q:\n    price: str = '0'\n"
    vs = _scan(src, "src/hft_platform/contracts/order.py")
    assert vs == []
