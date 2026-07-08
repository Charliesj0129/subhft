"""Unit tests for the shioaji-api-diff classifier and contract honesty.

These are pure-data tests: they synthesize minimal surface snapshots and assert
the diff + classification + verdict behavior, with no venv, no network, and no
shioaji import. They also include a meta-test that greps the real adapter to
keep ``platform_contract.py`` from going stale.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from scripts.shioaji_api_diff import classify, platform_contract
from scripts.shioaji_api_diff.diff import diff_snapshots

ADAPTER_DIR = Path(__file__).resolve().parents[4] / "src/hft_platform/feed_adapter/shioaji"


def _snapshot(**sections: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "package_layout": {"_present": True, "is_compiled": False, "submodules": {}},
        "constants": {},
        "models": {},
        "methods": {},
        "config": {"_present": True, "module": "shioaji.config", "defaults": {}},
        "exceptions": {},
        "compiled": {},
    }
    base.update(sections)
    return base


def _classify(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    return classify.classify_records(diff_snapshots(old, new))


def _find(records: list[dict[str, Any]], kind: str, qualname: str) -> dict[str, Any]:
    for r in records:
        if r["kind"] == kind and r["qualname"] == qualname:
            return r
    raise AssertionError(f"no record {kind}/{qualname} in {[(r['kind'], r['qualname']) for r in records]}")


def _enum(name: str, members: dict[str, str], source: str = "shioaji.constant") -> dict[str, Any]:
    return {name: {"_present": True, "members": members, "source": source}}


def test_removed_used_enum_member_is_breaking_and_blocks() -> None:
    old = _snapshot(constants=_enum("OrderType", {"ROD": "ROD", "IOC": "IOC", "FOK": "FOK"}))
    new = _snapshot(constants=_enum("OrderType", {"IOC": "IOC", "FOK": "FOK"}))
    records = _classify(old, new)
    rec = _find(records, "enum_member_removed", "OrderType.ROD")
    assert rec["classification"] == classify.BREAKING
    assert rec["platform_used"] is True
    assert classify.summarize(records)["verdict"] == "BLOCKED"


def test_removed_unused_enum_member_is_benign_and_safe() -> None:
    old = _snapshot(constants=_enum("Currency", {"TWD": "TWD", "USD": "USD"}))
    new = _snapshot(constants=_enum("Currency", {"TWD": "TWD"}))
    records = _classify(old, new)
    rec = _find(records, "enum_member_removed", "Currency.USD")
    assert rec["classification"] == classify.BENIGN
    assert rec["platform_used"] is False
    assert classify.summarize(records)["verdict"] == "SAFE"


def test_renamed_used_enum_member_is_breaking_but_shimmable() -> None:
    old = _snapshot(constants=_enum("OrderType", {"ROD": "ROD", "IOC": "IOC"}))
    new = _snapshot(constants=_enum("OrderType", {"ROD_NEW": "ROD", "IOC": "IOC"}))
    records = _classify(old, new)
    rec = _find(records, "enum_member_renamed", "OrderType.ROD")
    assert rec["classification"] == classify.BREAKING
    assert rec["shimmable"] is True
    assert classify.summarize(records)["verdict"] == "NEEDS-SHIM"


def test_added_enum_member_is_additive() -> None:
    old = _snapshot(constants=_enum("OrderType", {"ROD": "ROD"}))
    new = _snapshot(constants=_enum("OrderType", {"ROD": "ROD", "GTC": "GTC"}))
    records = _classify(old, new)
    assert _find(records, "enum_member_added", "OrderType.GTC")["classification"] == classify.ADDITIVE
    assert classify.summarize(records)["verdict"] == "SAFE"


def _method(cls: str, name: str, params: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        cls: {
            "_present": True,
            "source": "shioaji",
            "members": {name: {"_present": True, "params": params, "returns": None}},
        }
    }


def _param(name: str, default: Any = "<empty>") -> dict[str, Any]:
    return {"name": name, "kind": "POSITIONAL_OR_KEYWORD", "annotation": None, "default": default}


def test_new_optional_param_is_additive_ground_truth_account_balance() -> None:
    # The documented 1.2.9 -> 1.3.3 delta: account_balance gained optional account.
    old = _snapshot(methods=_method("Shioaji", "account_balance", [_param("self")]))
    new = _snapshot(methods=_method("Shioaji", "account_balance", [_param("self"), _param("account", default="None")]))
    records = _classify(old, new)
    assert _find(records, "param_added", "Shioaji.account_balance(account)")["classification"] == classify.ADDITIVE
    assert classify.summarize(records)["verdict"] == "SAFE"


def test_new_required_param_on_used_method_is_breaking() -> None:
    old = _snapshot(methods=_method("Shioaji", "place_order", [_param("self"), _param("contract")]))
    new = _snapshot(
        methods=_method("Shioaji", "place_order", [_param("self"), _param("contract"), _param("idempotency_key")])
    )
    records = _classify(old, new)
    rec = _find(records, "param_added", "Shioaji.place_order(idempotency_key)")
    assert rec["classification"] == classify.BREAKING
    assert classify.summarize(records)["verdict"] == "BLOCKED"


def test_sol_config_value_change_is_behavioral_not_blocking() -> None:
    old = _snapshot(
        config={
            "_present": True,
            "module": "shioaji.config",
            "defaults": {"SOL_RECONNECT_RETRIES": {"value": 10, "type": "int", "env_var": "SOL_RECONNECT_RETRIES"}},
        }
    )
    new = _snapshot(
        config={
            "_present": True,
            "module": "shioaji.config",
            "defaults": {"SOL_RECONNECT_RETRIES": {"value": 5, "type": "int", "env_var": "SOL_RECONNECT_RETRIES"}},
        }
    )
    records = _classify(old, new)
    rec = _find(records, "config_value_changed", "SOL_RECONNECT_RETRIES")
    assert rec["classification"] == classify.BEHAVIORAL
    assert classify.summarize(records)["verdict"] == "SAFE"


def test_solace_backend_removal_is_breaking_via_arity_shim() -> None:
    sol = {"shioaji.backend.solace.api": {"_present": True, "module_attrs": [], "classes": {}}}
    old = _snapshot(compiled=sol)
    new = _snapshot(compiled={"shioaji.backend.solace.api": {"_present": False, "_error": "ModuleNotFoundError"}})
    records = _classify(old, new)
    rec = _find(records, "compiled_module_removed", "shioaji.backend.solace.api")
    assert rec["classification"] == classify.BREAKING
    assert "_apply_solace_arity_shim" in rec["remediation"]
    assert classify.summarize(records)["verdict"] == "BLOCKED"


def test_removed_accessor_class_is_behavioral_not_outage() -> None:
    # The adapter reaches Quote via the api.quote accessor (client.py:_quote_api),
    # not by constructing Quote() by name. 1.5.3 folds Quote into Shioaji but
    # keeps api.quote as a property returning _QuoteProxy, so the class symbol
    # vanishing is a review item, NOT a silent outage. VERIFIED on installed 1.5.3.
    old = _snapshot(methods=_method("Quote", "subscribe", [_param("self"), _param("contract")]))
    new = _snapshot(methods={"Quote": {"_present": False}})
    records = _classify(old, new)
    rec = _find(records, "class_removed", "Quote")
    assert rec["classification"] == classify.BEHAVIORAL
    assert rec["platform_used"] is True
    assert "_QuoteProxy" in rec["remediation"]
    assert classify.summarize(records)["verdict"] == "SAFE"


def test_removed_constructed_class_is_breaking() -> None:
    # A class the adapter constructs by name (an order ctor) vanishing IS a hard
    # break — there is no instance-accessor compat path for it.
    old = _snapshot(methods=_method("StockOrder", "__init__", [_param("self")]))
    new = _snapshot(methods={"StockOrder": {"_present": False}})
    records = _classify(old, new)
    rec = _find(records, "class_removed", "StockOrder")
    assert rec["classification"] == classify.BREAKING
    assert rec["platform_used"] is True
    assert classify.summarize(records)["verdict"] == "BLOCKED"


def test_removed_unused_class_is_behavioral_not_breaking() -> None:
    old = _snapshot(methods=_method("SomeInternalThing", "frob", [_param("self")]))
    new = _snapshot(methods={"SomeInternalThing": {"_present": False}})
    records = _classify(old, new)
    rec = _find(records, "class_removed", "SomeInternalThing")
    assert rec["classification"] == classify.BEHAVIORAL
    assert rec["platform_used"] is False
    assert classify.summarize(records)["verdict"] == "SAFE"


def test_package_recompiled_is_informational_headline() -> None:
    old = _snapshot(package_layout={"_present": True, "is_compiled": False, "submodules": {}})
    new = _snapshot(package_layout={"_present": True, "is_compiled": True, "submodules": {}})
    records = _classify(old, new)
    rec = _find(records, "package_recompiled", "package.is_compiled")
    assert rec["classification"] == classify.INFORMATIONAL
    assert rec["platform_used"] is True


def test_identical_snapshots_produce_no_changes() -> None:
    snap = _snapshot(constants=_enum("Action", {"Buy": "Buy", "Sell": "Sell"}))
    assert _classify(snap, snap) == []
    assert classify.summarize([])["verdict"] == "SAFE"


@pytest.mark.unit
def test_platform_contract_covers_every_adapter_enum_reference() -> None:
    """Meta-test: every literal ``constant.<Enum>.<Member>`` in the adapter must
    be declared in platform_contract, so the classifier never silently treats a
    used member as benign."""
    pattern = re.compile(r"constant\.([A-Z][A-Za-z]+)\.([A-Za-z0-9_]+)")
    missing: list[str] = []
    for path in ADAPTER_DIR.glob("*.py"):
        for enum_name, member in pattern.findall(path.read_text(encoding="utf-8")):
            covered = (
                platform_contract.enum_member_used(enum_name, member)
                or enum_name in platform_contract.DYNAMIC_ENUM_CLASSES
            )
            if not covered:
                missing.append(f"{enum_name}.{member} ({path.name})")
    assert not missing, (
        f"platform_contract.py is stale — these adapter enum references are not covered: {sorted(set(missing))}"
    )
