"""Regression checks for canonical maker-action ownership and compatibility."""

from __future__ import annotations

import ast
import base64
import inspect
import pickle
from dataclasses import MISSING, fields
from pathlib import Path

import hft_platform.contracts.maker_actions as canonical
import research.backtest.maker_engine as legacy

_PUBLIC_ACTIONS = ("PostQuote", "CancelQuote", "Hold")

# Captured before the ownership migration from real protocol-4 pickles whose
# globals resolve through research.backtest.maker_engine.
_LEGACY_POST_QUOTE_PICKLE = (
    "gASVWAAAAAAAAACMHnJlc2VhcmNoLmJhY2t0ZXN0Lm1ha2VyX2VuZ2luZZSMCVBvc3RRdW90"
    "ZZSTlCmBlH2UKIwEc2lkZZSMA2J1eZSMBXByaWNllE1oQowDcXR5lEsCdWIu"
)
_LEGACY_CANCEL_QUOTE_PICKLE = (
    "gASVRwAAAAAAAACMHnJlc2VhcmNoLmJhY2t0ZXN0Lm1ha2VyX2VuZ2luZZSMC0NhbmNlbFF1b3RllJOUKYGUfZSMBHNpZGWUjARzZWxslHNiLg=="
)
_LEGACY_HOLD_PICKLE = "gASVLgAAAAAAAACMHnJlc2VhcmNoLmJhY2t0ZXN0Lm1ha2VyX2VuZ2luZZSMBEhvbGSUk5QpgZQu"


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_maker_engine_reexports_canonical_action_identities() -> None:
    assert canonical.__all__ == ["CancelQuote", "Hold", "PostQuote"]
    for name in _PUBLIC_ACTIONS:
        assert getattr(legacy, name) is getattr(canonical, name)


def test_canonical_maker_action_dataclass_abi_is_preserved() -> None:
    assert str(inspect.signature(canonical.PostQuote)) == ("(side: 'str', price: 'int', qty: 'int' = 1) -> None")
    assert str(inspect.signature(canonical.CancelQuote)) == "(side: 'str') -> None"
    assert str(inspect.signature(canonical.Hold)) == "() -> None"

    assert canonical.PostQuote.__annotations__ == {
        "side": "str",
        "price": "int",
        "qty": "int",
    }
    assert canonical.CancelQuote.__annotations__ == {"side": "str"}
    assert canonical.Hold.__annotations__ == {}

    assert canonical.PostQuote.__match_args__ == ("side", "price", "qty")
    assert canonical.CancelQuote.__match_args__ == ("side",)
    assert canonical.Hold.__match_args__ == ()

    post_fields = fields(canonical.PostQuote)
    assert [(field.name, field.default) for field in post_fields] == [
        ("side", MISSING),
        ("price", MISSING),
        ("qty", 1),
    ]
    assert [(field.name, field.default) for field in fields(canonical.CancelQuote)] == [("side", MISSING)]
    assert fields(canonical.Hold) == ()

    for cls in (canonical.PostQuote, canonical.CancelQuote, canonical.Hold):
        assert cls.__dataclass_params__.frozen is True
        assert cls.__dataclass_params__.eq is True
        assert cls.__dataclass_params__.order is False


def test_historical_maker_action_pickles_resolve_to_canonical_classes() -> None:
    post = pickle.loads(base64.b64decode(_LEGACY_POST_QUOTE_PICKLE))
    cancel = pickle.loads(base64.b64decode(_LEGACY_CANCEL_QUOTE_PICKLE))
    hold = pickle.loads(base64.b64decode(_LEGACY_HOLD_PICKLE))

    assert type(post) is canonical.PostQuote
    assert post == canonical.PostQuote(side="buy", price=17_000, qty=2)
    assert type(cancel) is canonical.CancelQuote
    assert cancel == canonical.CancelQuote(side="sell")
    assert type(hold) is canonical.Hold
    assert hold == canonical.Hold()


def test_maker_action_contract_does_not_import_runtime_or_research() -> None:
    root = Path(__file__).resolve().parents[2]
    modules = _imported_modules(root / "src/hft_platform/contracts/maker_actions.py")

    assert all(not module.startswith("research") for module in modules)
    assert all(
        not module.startswith(
            (
                "hft_platform.alpha",
                "hft_platform.backtest",
                "hft_platform.services",
            )
        )
        for module in modules
    )


def test_maker_bridge_does_not_import_research_maker_engine() -> None:
    root = Path(__file__).resolve().parents[2]
    modules = _imported_modules(root / "src/hft_platform/backtest/maker_bridge.py")

    assert "research.backtest.maker_engine" not in modules
