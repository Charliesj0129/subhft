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

# The months that actually matter now, and ones that do not exist yet. The
# 2026-08-10 outage happened because coverage was asserted only for the codes
# above — every one of which had expired by then.
CURRENT_AND_FUTURE_SYMBOLS = ("TMFH6", "TXFH6", "MXFH6", "TMFI6", "TXFI6", "MXFI6", "TMFA7", "TXFA7")

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
@pytest.mark.parametrize("symbol", ROLLOVER_SYMBOLS + CURRENT_AND_FUTURE_SYMBOLS)
def test_shipping_config_covers_rollover_codes_despite_broken_metadata(config_path: Path, symbol: str) -> None:
    """Every TAIFEX rollover code MUST resolve to a futures-grade cap.

    This asserts the B1 *guarantee* rather than the mechanism that used to
    implement it. The original test required a literal
    ``max_price_cap_<SYMBOL>`` key per contract, which is the very thing that
    failed on 2026-08-10: all six declared codes (D6/E6 = April/May-2026) had
    expired, the live front month TMFH6 matched none of them, and the cap fell
    back to ``max_price_cap_futures`` = 50,000 with TAIEX at 45,033 — about 11%
    from rejecting every order. A key that names a delivery month expires with
    the contract, so pinning the key made the config *look* guarded while the
    protection had already lapsed.

    The guarantee is unchanged and now roll-proof: with ``product_type``
    deliberately broken (the documented metadata gap), the shipped config must
    still admit a TAIEX-sized price for the contract in question — including
    months that do not exist yet, which the old assertion could never cover.
    """
    cfg = _load_yaml(config_path)
    validator = PriceBandValidator(cfg, price_scale_provider=_provider_with_broken_metadata())

    cap = validator._resolve_cap_raw(symbol)

    assert cap >= 50000.0, (
        f"{config_path.relative_to(REPO_ROOT)}: {symbol} resolves to a cap of {cap} with product_type broken; "
        "a TAIEX-sized price would be rejected. Declare max_price_cap_root_<ROOT>."
    )


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
    validator = PriceBandValidator(cfg, price_scale_provider=_provider_with_broken_metadata())

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
    validator = PriceBandValidator(cfg, price_scale_provider=_provider_with_broken_metadata())

    ok, reason = validator.check(_intent("TMFE6", price=404_000_000))

    assert not ok
    assert "PRICE_EXCEEDS_CAP" in reason


def test_r47_tmfe6_price_passes_risk_cap(tmp_path: Path) -> None:
    """RC-3 end-to-end: R47_MAKER_TMF intent for TMFE6 at scaled-int 40,200,000
    must pass ``RiskEngine.evaluate`` when the per-symbol override is loaded
    even though ``SymbolMetadata`` cannot resolve product_type for TMFE6.

    Mirrors the live failure mode: TMFE6 is absent from the auto-generated
    ``config/symbols.yaml``, so ``metadata.product_type("TMFE6")`` returns
    ``""`` and ``_resolve_cap_raw`` would fall back to the global stock cap
    (5,000 NTD raw → 50M scaled) without the per-symbol override.

    The intent price is 4,020 raw points × 10,000 = 40,200,000 — well below
    both 50M (broken cap) and 5B (override cap), but the regression doc
    captured the 402M live-broker observation (40,200 × 10k). Both prices
    must pass once the per-symbol override is honoured.
    """
    import asyncio

    from hft_platform.risk.engine import RiskEngine

    config_path = tmp_path / "strategy_limits.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "global_defaults": {
                    "max_price_cap": 5000.0,
                    "max_price_cap_futures": 50000.0,
                    "max_price_cap_options": 10000.0,
                    "max_price_cap_TMFE6": 500000.0,
                    "tick_size": 0.01,
                    "price_band_ticks": 20,
                    "max_qty": 10,
                    "max_notional": 10_000_000,
                    "max_position_lots": 5,
                    "max_daily_loss": 50_000_000,
                },
                "strategies": {},
            }
        )
    )

    intent_q: asyncio.Queue = asyncio.Queue()
    order_q: asyncio.Queue = asyncio.Queue()
    engine = RiskEngine(
        str(config_path),
        intent_q,
        order_q,
        price_scale_provider=_provider_with_broken_metadata(),
    )

    # Two probe prices both above the broken 50M fallback cap.
    for raw_pts in (4_020, 40_200):
        scaled_price = raw_pts * 10_000
        decision = engine.evaluate(_intent("TMFE6", price=scaled_price))
        assert decision.approved, (
            f"Expected approval for TMFE6 @ {scaled_price:,} scaled "
            f"(raw={raw_pts}), got reject={decision.reason_code!r}"
        )


def test_r47_tmfe6_price_rejected_without_override(tmp_path: Path) -> None:
    """Companion negative test: removing the per-symbol override reproduces
    the live RC-3 reject. Pins the regression so any future refactor of
    ``_resolve_cap_raw`` cannot silently weaken the defence.
    """
    import asyncio

    from hft_platform.risk.engine import RiskEngine

    config_path = tmp_path / "strategy_limits.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "global_defaults": {
                    "max_price_cap": 5000.0,
                    "max_price_cap_futures": 50000.0,
                    "max_price_cap_options": 10000.0,
                    # NOTE: max_price_cap_TMFE6 deliberately absent.
                    "tick_size": 0.01,
                    "price_band_ticks": 20,
                    "max_qty": 10,
                    "max_notional": 10_000_000,
                    "max_position_lots": 5,
                    "max_daily_loss": 50_000_000,
                },
                "strategies": {},
            }
        )
    )

    intent_q: asyncio.Queue = asyncio.Queue()
    order_q: asyncio.Queue = asyncio.Queue()
    engine = RiskEngine(
        str(config_path),
        intent_q,
        order_q,
        price_scale_provider=_provider_with_broken_metadata(),
    )

    decision = engine.evaluate(_intent("TMFE6", price=402_000_000))
    assert not decision.approved
    assert "PRICE_EXCEEDS_CAP" in decision.reason_code
