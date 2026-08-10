"""A price cap keyed on a delivery month expires with the contract.

`config/env/prod/strategy_limits.yaml` carried six per-symbol caps
(TMFE6/TMFD6/TXFE6/TXFD6/MXFE6/MXFD6), all May/April-2026. On 2026-08-10 the
live front month was TMFH6, which matched none of them and fell through to
`max_price_cap_futures` = 50,000 while TAIEX traded near 45,033 — about 11%
from `PriceBandValidator` rejecting every order R47 sends.

Same defect class as the session-governor track membership in this branch: a
key that names a month is a key that expires. These tests pin the per-root
tier and assert the shipped prod config no longer depends on month codes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from hft_platform.risk.validators import PriceBandValidator

_PROD_LIMITS = Path(__file__).resolve().parents[2] / "config" / "env" / "prod" / "strategy_limits.yaml"
_MONTH_LETTERS = "ABCDEFGHIJKL"


def _make_validator(defaults: dict[str, Any]) -> PriceBandValidator:
    return PriceBandValidator(config={"global_defaults": defaults, "strategies": {}})


@pytest.mark.unit
class TestRootCapResolution:
    def test_root_cap_covers_a_month_that_has_no_entry(self) -> None:
        """The exact 2026-08-10 shape: TMFH6 is live, only the root is configured."""
        v = _make_validator({"max_price_cap_root_TMF": 500000.0, "max_price_cap_futures": 50000.0})

        assert v._resolve_cap_raw("TMFH6") == 500000.0

    def test_root_cap_survives_every_roll(self) -> None:
        v = _make_validator({"max_price_cap_root_TMF": 500000.0, "max_price_cap_futures": 50000.0})

        for letter in _MONTH_LETTERS:
            for year in "6789":
                assert v._resolve_cap_raw(f"TMF{letter}{year}") == 500000.0

    def test_exact_symbol_cap_still_wins_over_the_root(self) -> None:
        """Ops keep the last word for a single contract."""
        v = _make_validator(
            {
                "max_price_cap_TMFH6": 123456.0,
                "max_price_cap_root_TMF": 500000.0,
                "max_price_cap_futures": 50000.0,
            }
        )

        assert v._resolve_cap_raw("TMFH6") == 123456.0

    def test_unconfigured_root_falls_through_to_the_global_cap(self) -> None:
        """Root matching must not become a blanket cap for everything."""
        v = _make_validator({"max_price_cap_root_TMF": 500000.0, "max_price_cap": 5000.0})

        assert v._resolve_cap_raw("2330") == 5000.0

    def test_option_series_does_not_inherit_a_futures_root_cap(self) -> None:
        """Root + month letter + year digit, not a prefix scan."""
        v = _make_validator({"max_price_cap_root_TXF": 500000.0, "max_price_cap": 5000.0})

        assert v._resolve_cap_raw("TXFH6") == 500000.0
        for code in ("TXO20800L6", "TXFH6X", "TXF"):
            assert v._resolve_cap_raw(code) == 5000.0

    def test_no_root_config_preserves_previous_behaviour(self) -> None:
        """Purely additive: a config with no root keys resolves as before."""
        v = _make_validator({"max_price_cap_TMFE6": 500000.0, "max_price_cap": 5000.0})

        assert v._resolve_cap_raw("TMFE6") == 500000.0
        assert v._resolve_cap_raw("TMFH6") == 5000.0


@pytest.mark.unit
class TestProdConfigCannotGoStale:
    """Guards the shipped file. The outage lived in config, not in code."""

    @staticmethod
    def _defaults() -> dict[str, Any]:
        return (yaml.safe_load(_PROD_LIMITS.read_text()) or {}).get("global_defaults", {})

    def test_no_price_cap_key_names_a_delivery_month(self) -> None:
        offenders = []
        for key in self._defaults():
            if not isinstance(key, str) or not key.startswith("max_price_cap_"):
                continue
            suffix = key[len("max_price_cap_") :]
            if len(suffix) == 5 and suffix[3] in _MONTH_LETTERS and suffix[4].isdigit():
                offenders.append(key)
        assert not offenders, (
            f"strategy_limits.yaml keys a price cap on a delivery month {offenders}; it expires and the "
            "live contract silently falls back to a lower cap. Use max_price_cap_root_<ROOT>."
        )

    def test_live_front_month_resolves_well_above_the_index(self) -> None:
        """TAIEX was ~45,033 on 2026-08-10; the futures fallback is 50,000."""
        v = _make_validator(self._defaults())

        for symbol in ("TMFH6", "TXFH6", "MXFH6", "TMFI6", "TXFI6"):
            cap = v._resolve_cap_raw(symbol)
            assert cap >= 100000.0, f"{symbol} resolves to a cap of {cap}, too close to a ~45,000 index"
