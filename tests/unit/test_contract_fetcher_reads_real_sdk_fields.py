"""Every contract field the fetcher asks for must exist on the SDK Contract.

``_normalize_contract`` reads the broker Contract with ``getattr(c, name,
None)`` and then drops every ``None``.  A name that does not exist on the SDK
model is therefore indistinguishable from a field the broker left empty: the
key simply never appears in ``config/contracts.json`` and nothing complains.

That is not hypothetical.  ``tick_size``, ``price_scale`` and
``contract_size`` were read here for years and appear on the shioaji Contract
model in *no* version -- 1.3.3 lists them in neither ``fields`` nor
``attributes``, and neither does 1.5.6.  All three were dropped on every one
of the 55,888 rows of the production cache, and the symbols builder quietly
fell through to its hardcoded per-root table instead.

The SDK surface golden is the source of truth for what exists, so this test
diffs the fetcher's own request list against it and fails on the next such
typo rather than after it has been in the cache for a year.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FETCHER = REPO / "src/hft_platform/feed_adapter/contract_fetcher.py"
GOLDEN = REPO / "tests/golden/shioaji_sdk/surface_1.5.6.json"

# Not read off the broker Contract: supplied by the caller / derived locally.
_NOT_SDK_FIELDS = {"right"}


def _requested_attribute_names() -> set[str]:
    """Collect every ``getattr(contract, "<name>", ...)`` in _normalize_contract."""
    tree = ast.parse(FETCHER.read_text(encoding="utf-8"))
    fn = next(
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_normalize_contract"
    )
    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
            continue
        if len(node.args) < 2:
            continue
        target, attr = node.args[0], node.args[1]
        if not (isinstance(target, ast.Name) and target.id == "contract"):
            continue
        if isinstance(attr, ast.Constant) and isinstance(attr.value, str):
            names.add(attr.value)
    return names


def _sdk_contract_attributes() -> set[str]:
    surface = json.loads(GOLDEN.read_text(encoding="utf-8"))
    model = surface["models"]["Contract"]
    return set(model.get("attributes") or []) | set((model.get("fields") or {}).keys())


def test_the_fetcher_only_asks_for_contract_fields_the_sdk_actually_has() -> None:
    requested = _requested_attribute_names() - _NOT_SDK_FIELDS
    available = _sdk_contract_attributes()
    assert requested, "no getattr(contract, ...) calls found -- the parser broke, not the code"
    missing = sorted(requested - available)
    assert not missing, (
        f"_normalize_contract reads {missing} off the broker Contract, but the "
        f"shioaji 1.5.6 surface golden has no such field. getattr(..., None) makes "
        f"this silent: the key is dropped from every cache row."
    )


def test_the_option_strike_and_reference_names_are_the_sdk_spelling() -> None:
    """The two fields ATM strike selection depends on."""
    available = _sdk_contract_attributes()
    assert "strike_price" in available
    assert "reference" in available
