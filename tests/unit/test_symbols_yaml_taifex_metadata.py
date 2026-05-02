"""B2 regression: TAIFEX rollover-resolved codes MUST classify as ``future``.

Background
----------
The 2026-04-27 PRICE_EXCEEDS_CAP incident was triggered by a metadata gap:
``config/base/symbols.yaml`` declared only continuous alias codes (TMFR1,
TXFR1, MXFR1) and relied on ``set_alias_map`` (called after broker login)
to copy entries from each alias to its resolved month code (e.g.
TMFR1 -> TMFE6). When that propagation fails for any reason — restart
ordering, missing broker session, contracts fetch error — the platform
sees the resolved code (e.g. ``TMFE6``) but ``SymbolMetadata.product_type``
returns ``""`` because no entry exists, leading PriceBandValidator's
fallback to the wrong cap.

Fix B2 — add explicit entries for the active rollover-resolved codes so
``product_type`` correctly returns ``"future"`` even without alias propagation.
This is the root-cause fix complementing the per-symbol cap workaround (B1).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hft_platform.feed_adapter.normalizer import SymbolMetadata

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_SYMBOLS_YAML = REPO_ROOT / "config" / "base" / "symbols.yaml"

# The 2026-Q2 TAIFEX rollover window: D6 = April-2026 (just expired),
# E6 = May-2026 (front month as of 2026-04-27). Cover all three TAIFEX
# futures families (TMF/TXF/MXF) so a metadata-resolution miss on any
# product type cannot produce the PRICE_EXCEEDS_CAP regression.
ROLLOVER_FUTURES = ("TMFE6", "TMFD6", "TXFE6", "TXFD6", "MXFE6", "MXFD6")


@pytest.fixture(scope="module")
def metadata() -> SymbolMetadata:
    """Load the canonical base symbols.yaml directly (no alias propagation).

    This deliberately bypasses ``set_alias_map`` so the test exercises the
    behaviour seen in production when alias copying fails.
    """
    return SymbolMetadata(str(BASE_SYMBOLS_YAML))


@pytest.mark.parametrize("symbol", ROLLOVER_FUTURES)
def test_rollover_code_classifies_as_future(metadata: SymbolMetadata, symbol: str) -> None:
    """Each rollover-resolved code MUST be classified as ``future``.

    Asserted directly against the on-disk config file rather than via a
    mock so a YAML-level regression (someone removes an entry, or the
    auto-generator overwrites the canonical base file) is caught.
    """
    ptype = metadata.product_type(symbol)
    assert ptype == "future", (
        f"{symbol}: expected product_type='future' from "
        f"{BASE_SYMBOLS_YAML.relative_to(REPO_ROOT)}, got {ptype!r}. "
        "Add an explicit entry with `product_type: future` so the platform "
        "never falls through to the global price cap."
    )
