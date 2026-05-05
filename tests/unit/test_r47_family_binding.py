"""R47 opt-in family binding integration test.

Ensures ``config/live/strategies.yaml``'s R47_MAKER_TMF entry is picked up
correctly by the ``StrategyRegistry`` and ``StrategyRunner`` when the
ContractFamilyResolver has a live snapshot.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from hft_platform.contracts.family_resolver import (
    ContractFamilyResolver,
    build_snapshot_from_calendar,
)
from hft_platform.contracts.ref import (
    ContractFamily,
    FamilyCode,
    FutureRef,
    Product,
)
from hft_platform.strategy.registry import StrategyRegistry, _parse_contract_families


def _tmf(month: int, year: int = 2026) -> FutureRef:
    return FutureRef(root="TMF", expiry=date(year, month, 21))


class TestContractFamilyParser:
    def test_parse_single_entry(self) -> None:
        raw = [{"product": "FUTURE", "root": "TMF", "family": "R1"}]
        out = _parse_contract_families(raw)
        assert out == (ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1),)

    def test_parse_multiple_entries(self) -> None:
        raw = [
            {"product": "FUTURE", "root": "TMF", "family": "R1"},
            {"product": "FUTURE", "root": "TXF", "family": "R1"},
        ]
        out = _parse_contract_families(raw)
        assert len(out) == 2
        assert out[0].root == "TMF"
        assert out[1].root == "TXF"

    def test_parse_empty_list_is_empty_tuple(self) -> None:
        assert _parse_contract_families([]) == ()

    def test_parse_none_is_empty_tuple(self) -> None:
        assert _parse_contract_families(None) == ()

    def test_invalid_entry_skipped_not_raised(self) -> None:
        raw = [
            {"product": "FUTURE", "root": "TMF", "family": "R1"},
            {"product": "INVALID", "root": "X", "family": "R1"},  # bad product
            {"product": "FUTURE", "root": "TXF", "family": "BOGUS"},  # bad family
            {"product": "STOCK", "root": "2330", "family": "R1"},  # rejected: stocks
        ]
        out = _parse_contract_families(raw)
        assert len(out) == 1
        assert out[0].root == "TMF"

    def test_case_insensitive_input(self) -> None:
        raw = [{"product": "future", "root": "tmf", "family": "r1"}]
        out = _parse_contract_families(raw)
        assert out[0] == ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)


class TestR47Config:
    def test_strategies_yaml_entry_has_tmf_r1_family(self) -> None:
        """Guard against accidental removal of R47's family binding."""
        text = Path("config/live/strategies.yaml").read_text(encoding="utf-8")
        doc = yaml.safe_load(text)
        entry = next(s for s in doc["strategies"] if s["id"] == "R47_MAKER_TMF")
        families = entry.get("contract_families") or []
        assert families, "R47 must carry a contract_families binding post-Gate-1"
        assert any(
            fam.get("root") == "TMF"
            and str(fam.get("family", "")).upper() == "R1"
            and str(fam.get("product", "")).upper() == "FUTURE"
            for fam in families
        )

    def test_registry_parses_r47_contract_families(self, tmp_path: Path) -> None:
        cfg = tmp_path / "strategies.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "strategies": [
                        {
                            "id": "R47_TEST",
                            "module": "hft_platform.strategies.simple_mm",
                            "class": "SimpleMarketMaker",
                            "enabled": False,  # avoid instantiation side effects
                            "product_type": "FUT",
                            "symbols": ["TMFE6"],
                            "contract_families": [{"product": "FUTURE", "root": "TMF", "family": "R1"}],
                            "params": {},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        reg = StrategyRegistry(str(cfg))
        assert len(reg.configs) == 1
        cfg0 = reg.configs[0]
        assert cfg0.contract_families == [{"product": "FUTURE", "root": "TMF", "family": "R1"}]


class TestEndToEndBinding:
    def test_r47_family_binding_updates_symbols_on_snapshot_swap(self) -> None:
        """Full sequence: register a TMF:R1 family-bound fake strategy,
        swap snapshot, verify ``strategy.symbols`` flips to TMFE6.
        """
        from hft_platform.strategy.runner import StrategyRunner

        runner = StrategyRunner.__new__(StrategyRunner)
        runner.strategies = []
        runner._family_resolver = None

        resolver = ContractFamilyResolver()
        runner.set_family_resolver(resolver)

        strat = type(
            "FakeR47",
            (),
            {
                "strategy_id": "r47_test",
                "symbols": {"TMFE6"},  # pre-existing hardcode
                "contract_families": (ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1),),
                "enabled": True,
            },
        )()
        runner.strategies.append(strat)
        runner._apply_family_bindings(strat)

        # Prior to any snapshot, only the hardcoded symbol is present.
        assert strat.symbols == {"TMFE6"}

        # Populator swap: first snapshot — TMF R1 binds to May 2026.
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5), _tmf(6)]},
                today=date(2026, 4, 19),
                snapshot_ns=1000,
            )
        )
        assert "TMFE6" in strat.symbols

        # Rollover: May expires, June becomes R1.
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(6), _tmf(7)]},
                today=date(2026, 5, 22),
                snapshot_ns=2000,
            )
        )
        assert "TMFF6" in strat.symbols
        assert "TMFE6" not in strat.symbols, "R47 symbols should drop the expired ref on rollover"

    def test_r47_without_family_binding_is_unaffected(self) -> None:
        """A legacy str-only R47-like strategy (no contract_families) is
        not mutated by the resolver."""
        from hft_platform.strategy.runner import StrategyRunner

        runner = StrategyRunner.__new__(StrategyRunner)
        runner.strategies = []
        runner._family_resolver = None

        resolver = ContractFamilyResolver()
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5)]},
                today=date(2026, 4, 19),
                snapshot_ns=1000,
            )
        )
        runner.set_family_resolver(resolver)

        strat = type(
            "LegacyR47",
            (),
            {
                "strategy_id": "legacy_r47",
                "symbols": {"TMFE6"},
                # no contract_families attribute — true legacy
                "enabled": True,
            },
        )()
        runner.strategies.append(strat)
        runner._apply_family_bindings(strat)

        assert strat.symbols == {"TMFE6"}
