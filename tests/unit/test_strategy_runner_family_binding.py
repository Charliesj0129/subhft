"""Gate 1 prod: StrategyRunner family-aware registration tests.

Drives the real ``StrategyRunner`` (via ``__new__``) with a real
``ContractFamilyResolver`` and a minimal fake strategy to verify:

* Registering a strategy with ``contract_families`` pulls the current
  ``FutureRef`` into ``strategy.symbols``.
* Snapshot swap on the resolver atomically updates ``strategy.symbols``
  via the rebind hook.
* Str-only strategies (legacy) are unaffected by the resolver.
* Cold start (resolver empty at registration) still rebinds correctly
  once the first snapshot lands.
"""

from __future__ import annotations

from datetime import date

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


class _FakeStrategy:
    def __init__(
        self,
        strategy_id: str,
        *,
        symbols: set[str] | None = None,
        contract_families: tuple[ContractFamily, ...] = (),
    ) -> None:
        self.strategy_id = strategy_id
        self.symbols = symbols or set()
        self.contract_families = contract_families
        self.enabled = True


def _runner():
    from hft_platform.strategy.runner import StrategyRunner

    r = StrategyRunner.__new__(StrategyRunner)
    r.strategies = []
    r._family_resolver = None
    return r


def _tmf(month: int, year: int = 2026) -> FutureRef:
    return FutureRef(root="TMF", expiry=date(year, month, 21))


class TestRegistrationSeedsSymbols:
    def test_family_binding_adds_current_ref_display(self) -> None:
        runner = _runner()
        resolver = ContractFamilyResolver()
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5), _tmf(6)]},
                today=date(2026, 4, 19),
                snapshot_ns=1000,
            )
        )
        runner.set_family_resolver(resolver)

        strat = _FakeStrategy(
            "s1",
            contract_families=(
                ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1),
            ),
        )
        runner.strategies.append(strat)
        runner._apply_family_bindings(strat)

        assert "TMFE6" in strat.symbols

    def test_str_only_strategy_not_affected_by_resolver(self) -> None:
        runner = _runner()
        resolver = ContractFamilyResolver()
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5)]},
                today=date(2026, 4, 19),
                snapshot_ns=1000,
            )
        )
        runner.set_family_resolver(resolver)

        strat = _FakeStrategy("s1", symbols={"2330"})  # no families
        runner.strategies.append(strat)
        runner._apply_family_bindings(strat)

        assert strat.symbols == {"2330"}

    def test_no_resolver_registration_is_noop(self) -> None:
        runner = _runner()
        # No set_family_resolver() call.
        strat = _FakeStrategy(
            "s1",
            contract_families=(
                ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1),
            ),
        )
        runner.strategies.append(strat)
        runner._apply_family_bindings(strat)
        # No ref available, symbols unchanged.
        assert strat.symbols == set()


class TestRebindHookUpdatesSymbols:
    def test_snapshot_swap_replaces_old_ref(self) -> None:
        runner = _runner()
        resolver = ContractFamilyResolver()
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5)]},
                today=date(2026, 4, 19),
                snapshot_ns=1000,
            )
        )
        runner.set_family_resolver(resolver)

        strat = _FakeStrategy(
            "s1",
            contract_families=(
                ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1),
            ),
        )
        runner.strategies.append(strat)
        runner._apply_family_bindings(strat)
        assert strat.symbols == {"TMFE6"}

        # Rollover: swap to June.
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(6)]},
                today=date(2026, 5, 22),
                snapshot_ns=2000,
            )
        )
        # Hook installed via set_family_resolver → strategy.symbols updated.
        assert "TMFF6" in strat.symbols
        assert "TMFE6" not in strat.symbols

    def test_hook_ignores_strategies_not_subscribed_to_that_family(self) -> None:
        runner = _runner()
        resolver = ContractFamilyResolver()
        runner.set_family_resolver(resolver)

        tmf_strat = _FakeStrategy(
            "tmf",
            contract_families=(
                ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1),
            ),
        )
        txf_strat = _FakeStrategy(
            "txf",
            contract_families=(
                ContractFamily(Product.FUTURE, "TXF", FamilyCode.R1),
            ),
        )
        runner.strategies.extend([tmf_strat, txf_strat])

        # Swap a snapshot that only has TMF — txf_strat's symbols stay empty.
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5)]},
                today=date(2026, 4, 19),
                snapshot_ns=1000,
            )
        )
        assert tmf_strat.symbols == {"TMFE6"}
        assert txf_strat.symbols == set()


class TestColdStart:
    def test_empty_resolver_registered_first_binds_on_first_swap(self) -> None:
        runner = _runner()
        resolver = ContractFamilyResolver()  # cold — no snapshot yet
        runner.set_family_resolver(resolver)

        strat = _FakeStrategy(
            "s1",
            contract_families=(
                ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1),
            ),
        )
        runner.strategies.append(strat)
        runner._apply_family_bindings(strat)
        # Nothing to bind yet.
        assert strat.symbols == set()

        # Populator lands first snapshot — hook rebinds strategy atomically.
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5)]},
                today=date(2026, 4, 19),
                snapshot_ns=1000,
            )
        )
        assert strat.symbols == {"TMFE6"}


class TestSetResolverReBindsExistingStrategies:
    def test_set_family_resolver_after_registration_applies_bindings(self) -> None:
        runner = _runner()
        # Register strategies FIRST, then attach resolver (reverse of normal).
        strat = _FakeStrategy(
            "s1",
            contract_families=(
                ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1),
            ),
        )
        runner.strategies.append(strat)

        resolver = ContractFamilyResolver()
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5)]},
                today=date(2026, 4, 19),
                snapshot_ns=1000,
            )
        )
        runner.set_family_resolver(resolver)

        assert "TMFE6" in strat.symbols


class TestNoneResolverSafe:
    def test_set_family_resolver_none_clears_state(self) -> None:
        runner = _runner()
        resolver = ContractFamilyResolver()
        runner.set_family_resolver(resolver)
        runner.set_family_resolver(None)  # should not raise
        assert runner._family_resolver is None


class TestResolverMissingAddHookSafely:
    def test_non_resolver_like_object_logs_warning_and_skips(self) -> None:
        runner = _runner()

        class _Fake:
            pass

        runner.set_family_resolver(_Fake())
        # Must not raise; runner must remember the attempt.
        assert runner._family_resolver is not None


# Smoke: the module-level hook wiring in bootstrap sets up families AND still
# calls resolve_symbol_aliases — verify both paths are compatible.


class TestCoexistWithLegacyAliasPath:
    def test_legacy_resolve_symbol_aliases_still_works_alongside_family(
        self,
    ) -> None:
        """A strategy can mix ``contract_families`` and legacy str ``symbols``
        (e.g. for a stock hedge alongside a futures family). Both resolution
        paths must cooperate — neither overwrites the other's contribution.
        """
        runner = _runner()

        from hft_platform.feed_adapter.normalizer import SymbolMetadata

        sm = SymbolMetadata.__new__(SymbolMetadata)
        sm.meta = {}
        sm.tags_by_symbol = {}
        sm.symbols_by_tag = {}
        sm.alias_to_actual = {}
        runner.symbol_metadata = sm

        resolver = ContractFamilyResolver()
        resolver.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5)]},
                today=date(2026, 4, 19),
                snapshot_ns=1000,
            )
        )
        runner.set_family_resolver(resolver)

        strat = _FakeStrategy(
            "hedge",
            symbols={"2330"},
            contract_families=(
                ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1),
            ),
        )
        runner.strategies.append(strat)
        runner._apply_family_bindings(strat)

        assert "TMFE6" in strat.symbols
        assert "2330" in strat.symbols
