"""Regression tests for ``config/symbols.yaml`` canonical integrity.

Background — 2026-04-27 incident (B2 metadata gap, fix-rc4):

``QuoteConnectionPool._refresh_options_inner`` previously used
``SYMBOLS_CONFIG`` as both the INPUT path (where canonical metadata lives)
and the OUTPUT path (where TXO chain auto-refresh writes its snapshot).
A live operator with ``SYMBOLS_CONFIG=config/symbols.yaml`` (the default)
caused the canonical 1868-line file to be overwritten by a 370-line
transient snapshot containing only TXFC0 + TXO June options and **no**
``product_type``/``tick_size``/``price_scale``/``point_value`` fields.

Downstream impact: ``SymbolMetadata`` defaulted every cap/scale lookup
to its built-in fallbacks → ``PositionStore`` mismarked TAIFEX micros
→ ``OrderAdapter`` accepted prices the price-cap should have rejected.

These tests pin the canonical file's invariants so regressions surface
before they hit live trading.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_PATH = PROJECT_ROOT / "config" / "symbols.yaml"


@pytest.fixture(scope="module")
def canonical_data() -> dict:
    """Parse ``config/symbols.yaml`` once for all tests in this module."""
    assert CANONICAL_PATH.is_file(), f"canonical symbols.yaml missing at {CANONICAL_PATH}"
    with CANONICAL_PATH.open("r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    assert isinstance(loaded, dict), "canonical must be a YAML mapping"
    assert "symbols" in loaded, "canonical missing top-level 'symbols' key"
    return loaded


@pytest.fixture(scope="module")
def symbol_index(canonical_data: dict) -> dict[str, dict]:
    return {entry["code"]: entry for entry in canonical_data["symbols"]}


def test_canonical_not_truncated_by_runtime_snapshot(canonical_data: dict) -> None:
    """The transient TXO snapshot wrote only ~120 entries; canonical must
    carry the hand-curated universe (≥150 symbols)."""
    n = len(canonical_data["symbols"])
    assert n >= 150, (
        f"canonical symbols.yaml has only {n} entries — likely overwritten by "
        f"QuoteConnectionPool runtime snapshot. Check HFT_SYMBOLS_RUNTIME_SNAPSHOT "
        f"is NOT equal to SYMBOLS_CONFIG (2026-04-27 B2 fix-rc4)."
    )


def test_canonical_contains_required_taifex_futures(symbol_index: dict[str, dict]) -> None:
    """Front + next-month TXF/MXF/TMF rollover-resolved codes must be present
    so ``SymbolMetadata.product_type()`` returns 'future' for risk caps.
    See ``d6b86020 fix(symbols): set product_type on TAIFEX rollover-resolved
    futures codes`` for the related defence layer."""
    required = ("TXFF6", "TXFI6", "MXFF6", "MXFI6", "TMFF6", "TMFI6")
    missing = [code for code in required if code not in symbol_index]
    assert not missing, f"canonical missing TAIFEX futures: {missing}"


def test_canonical_contains_anchor_stocks(symbol_index: dict[str, dict]) -> None:
    """A handful of TSE stock anchors (TSMC + cement) must be present so
    PositionStore recognises stock product_type for non-TAIFEX symbols."""
    for code in ("2330", "1101"):
        assert code in symbol_index, f"canonical missing anchor stock {code}"


def test_taifex_futures_have_complete_metadata(symbol_index: dict[str, dict]) -> None:
    """Every futures entry must carry ``product_type``, ``point_value``,
    ``tick_size`` and ``price_scale``. The B2 incident wiped these by
    overwriting the canonical with a code/exchange/group-only snapshot."""
    required_fields = ("product_type", "point_value", "tick_size", "price_scale")
    futures_codes = ("TXFF6", "TXFI6", "MXFF6", "MXFI6", "TMFF6", "TMFI6")
    for code in futures_codes:
        entry = symbol_index[code]
        for field in required_fields:
            assert field in entry, (
                f"futures {code} missing '{field}' in canonical — runtime snapshot regression suspected"
            )
        assert entry["product_type"] == "future"
        assert isinstance(entry["price_scale"], int)
        assert entry["price_scale"] == 10000
        assert isinstance(entry["point_value"], (int, float))
        assert entry["point_value"] > 0


def test_stocks_have_product_type(symbol_index: dict[str, dict]) -> None:
    """Stock entries (TSE/OTC) must carry product_type so risk caps fire."""
    sampled = [code for code in symbol_index if code.isdigit()][:10]
    assert sampled, "no stock-style codes in canonical (regression?)"
    for code in sampled:
        entry = symbol_index[code]
        assert entry.get("product_type") == "stock", f"{code} missing/wrong product_type"


def test_no_runtime_snapshot_marker(canonical_data: dict) -> None:
    """The QuoteConnectionPool writer prefixes its output with
    ``# Auto-refreshed by QuoteConnectionPool``. If we ever see that header
    in the canonical file, the writer escaped its sandbox again."""
    raw = CANONICAL_PATH.read_text(encoding="utf-8")
    assert "Auto-refreshed by QuoteConnectionPool" not in raw, (
        "canonical symbols.yaml carries the runtime-snapshot header — "
        "the QuoteConnectionPool writer is overwriting it again."
    )
