"""B1 regression: per-symbol price cap overrides for TAIFEX rollover safety.

Background
----------
2026-04-27 PRICE_EXCEEDS_CAP incident — R47_MAKER_TMF intents at scaled
price ~404,000,000 (TAIEX ~40,400 pts × 10,000) were being 100 % rejected
because:

1. ``config/env/prod/strategy_limits.yaml`` did not declare a per-symbol
   override for the active TAIFEX front-month (e.g. ``TMFE6``).
2. ``config/symbols.yaml`` did not contain a ``TMFE6`` entry, so
   ``SymbolMetadata.product_type("TMFE6")`` returned ``""``.
3. ``PriceBandValidator._resolve_cap_raw`` then fell back to the global
   ``max_price_cap`` (5000.0 NTD → 50,000,000 scaled), which rejected the
   real ~40,400 NTD scaled futures price.

Fix B1 (this test) — defensive workaround. Even when ``product_type`` is
empty (the documented metadata-resolution gap), an explicit per-symbol
override (``max_price_cap_TMFE6``) MUST cover every TAIFEX rollover code
shipped in production strategy_limits.

Fix B2 (separate) addresses the root metadata gap in symbols.yaml.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.core.pricing import SymbolMetadataPriceScaleProvider
from hft_platform.risk.validators import PriceBandValidator

# Codes covering the active rollover window: D6 = April-2026 (just expired),
# E6 = May-2026 (front month as of 2026-04-27). TMF/TXF/MXF roots cover the
# three TAIFEX futures families that platform strategies route through.
ROLLOVER_SYMBOLS = ("TMFE6", "TMFD6", "TXFE6", "TXFD6", "MXFE6", "MXFD6")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_LIMITS = REPO_ROOT / "config" / "base" / "strategy_limits.yaml"
PROD_LIMITS = REPO_ROOT / "config" / "env" / "prod" / "strategy_limits.yaml"


def _intent(symbol: str, price: int) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="R47_MAKER_TMF",
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price,
        qty=1,
    )


def _provider_with_broken_metadata() -> SymbolMetadataPriceScaleProvider:
    """Simulate the production failure: metadata.product_type returns ''.

    This mirrors the real failure mode where ``symbols.yaml`` has no entry
    for the active front-month contract code (e.g. TMFE6), so neither the
    explicit ``product_type`` field nor the ``exchange``-based fallback
    classifies the symbol as a future.
    """
    metadata = MagicMock()
    metadata.price_scale.return_value = 10000
    metadata.product_type.return_value = ""  # broken: no metadata
    return SymbolMetadataPriceScaleProvider(metadata=metadata)


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


@pytest.mark.parametrize("config_path", [BASE_LIMITS, PROD_LIMITS], ids=["base", "prod"])
@pytest.mark.parametrize("symbol", ROLLOVER_SYMBOLS)
def test_yaml_declares_per_symbol_override(config_path: Path, symbol: str) -> None:
    """Each rollover code MUST have ``max_price_cap_<symbol>`` in shipping configs.

    This is the regression that the prod incident required: without these
    entries, ``_resolve_cap_raw`` cannot defend against a metadata gap.
    """
    cfg = _load_yaml(config_path)
    defaults = cfg.get("global_defaults") or {}
    key = f"max_price_cap_{symbol}"
    assert key in defaults, (
        f"{config_path.relative_to(REPO_ROOT)}: missing {key!r} — "
        f"per-symbol override required for rollover safety."
    )
    # Sanity: the override must be high enough to admit a TAIEX-sized price
    # (~40,400 NTD raw). 50000.0 is the minimum safe value; we recommend
    # 500000.0 for ~12x headroom.
    cap = float(defaults[key])
    assert cap >= 50000.0, f"{key} = {cap} too low for futures pricing."


def test_validator_admits_real_taiex_price_with_override() -> None:
    """End-to-end: with the per-symbol override, validator accepts the real prod price.

    This is the behavioural assertion that mirrors the live-broker reject
    (~404M scaled) under the real metadata-broken path.
    """
    cfg = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "max_price_cap_futures": 50000.0,
            "max_price_cap_TMFE6": 500000.0,
        }
    }
    validator = PriceBandValidator(
        cfg, price_scale_provider=_provider_with_broken_metadata()
    )

    # TAIEX ~40,400 pts × 10,000 = 404_000_000 scaled — real prod price.
    ok, reason = validator.check(_intent("TMFE6", price=404_000_000))

    assert ok, f"Expected approval with per-symbol override; got reject: {reason}"


def test_validator_without_override_rejects_due_to_global_fallback() -> None:
    """Demonstrates the original bug: no override + empty product_type → reject."""
    cfg = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "max_price_cap_futures": 50000.0,
            # No TMFE6 override -> falls back to global 5000.
        }
    }
    validator = PriceBandValidator(
        cfg, price_scale_provider=_provider_with_broken_metadata()
    )

    ok, reason = validator.check(_intent("TMFE6", price=404_000_000))

    assert not ok
    assert "PRICE_EXCEEDS_CAP" in reason
