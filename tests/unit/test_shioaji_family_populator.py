"""Shioaji family-resolver populator tests."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from hft_platform.contracts.family_resolver import ContractFamilyResolver
from hft_platform.contracts.ref import ContractFamily, FamilyCode, Product
from hft_platform.feed_adapter.shioaji.family_populator import (
    _parse_delivery_to_date,
    populate_resolver_from_shioaji,
)


def _mock_contract(code: str, *, delivery_month: str | None = None, delivery_date: str | None = None):
    obj = SimpleNamespace()
    obj.code = code
    obj.delivery_month = delivery_month
    obj.delivery_date = delivery_date
    return obj


class _FakeContainer:
    """Simulates Shioaji's ``Contracts.Futures`` mapping interface."""

    def __init__(self, roots: dict[str, list]) -> None:
        self._roots = roots

    def keys(self):
        return self._roots.keys()

    def __getitem__(self, root: str) -> list:
        return self._roots[root]


def _fake_api(roots: dict[str, list]) -> SimpleNamespace:
    return SimpleNamespace(Contracts=SimpleNamespace(Futures=_FakeContainer(roots)))


class TestDeliveryDateParser:
    def test_yyyymm(self) -> None:
        assert _parse_delivery_to_date("202605") == date(2026, 5, 31)

    def test_yyyy_slash_mm(self) -> None:
        assert _parse_delivery_to_date("2026/05") == date(2026, 5, 31)

    def test_yyyymmdd(self) -> None:
        assert _parse_delivery_to_date("20260521") == date(2026, 5, 21)

    def test_yyyy_slash_mm_slash_dd(self) -> None:
        assert _parse_delivery_to_date("2026/05/21") == date(2026, 5, 21)

    def test_iso_dash_form(self) -> None:
        assert _parse_delivery_to_date("2026-05-21") == date(2026, 5, 21)

    def test_none_returns_none(self) -> None:
        assert _parse_delivery_to_date(None) is None

    def test_garbage_returns_none(self) -> None:
        assert _parse_delivery_to_date("not-a-date") is None


class TestPopulator:
    def test_single_root_multiple_expiries(self) -> None:
        api = _fake_api(
            {
                "TMF": [
                    _mock_contract("TMFE6", delivery_date="2026/05/21"),
                    _mock_contract("TMFF6", delivery_date="2026/06/17"),
                    _mock_contract("TMFG6", delivery_date="2026/07/15"),
                ]
            }
        )
        resolver = ContractFamilyResolver()
        count = populate_resolver_from_shioaji(resolver, api, today=date(2026, 4, 19))

        # Both R1 and R2 installed.
        assert count == 2
        fam_r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        fam_r2 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R2)
        assert resolver.resolve_family(fam_r1).expiry.month == 5
        assert resolver.resolve_family(fam_r2).expiry.month == 6

    def test_alias_contracts_are_skipped(self) -> None:
        api = _fake_api(
            {
                "TMF": [
                    _mock_contract("TMFR1", delivery_date="2026/05/21"),
                    _mock_contract("TMFR2", delivery_date="2026/06/17"),
                    _mock_contract("TMFE6", delivery_date="2026/05/21"),
                ]
            }
        )
        resolver = ContractFamilyResolver()
        populate_resolver_from_shioaji(resolver, api, today=date(2026, 4, 19))
        # Only the real expiry shows up as R1 binding.
        fam_r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        ref = resolver.resolve_family(fam_r1)
        assert ref is not None
        assert ref.display() == "TMFE6"

    def test_multiple_roots(self) -> None:
        api = _fake_api(
            {
                "TMF": [_mock_contract("TMFE6", delivery_date="2026/05/21")],
                "TXF": [_mock_contract("TXFE6", delivery_date="2026/05/21")],
            }
        )
        resolver = ContractFamilyResolver()
        populate_resolver_from_shioaji(resolver, api, today=date(2026, 4, 19))
        assert resolver.resolve_family(ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)) is not None
        assert resolver.resolve_family(ContractFamily(Product.FUTURE, "TXF", FamilyCode.R1)) is not None

    def test_expired_contracts_dropped(self) -> None:
        api = _fake_api(
            {
                "TMF": [
                    _mock_contract("TMFC6", delivery_date="2026/03/18"),
                    _mock_contract("TMFD6", delivery_date="2026/04/16"),
                    _mock_contract("TMFE6", delivery_date="2026/05/21"),
                ]
            }
        )
        resolver = ContractFamilyResolver()
        populate_resolver_from_shioaji(resolver, api, today=date(2026, 4, 17))
        # April 17 is past March/April → May becomes R1.
        ref = resolver.resolve_family(ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1))
        assert ref is not None
        assert ref.display() == "TMFE6"

    def test_contracts_without_delivery_info_skipped(self) -> None:
        api = _fake_api(
            {
                "TMF": [
                    _mock_contract("TMFE6", delivery_date="2026/05/21"),
                    _mock_contract("TMF_UNKNOWN"),  # no delivery info
                ]
            }
        )
        resolver = ContractFamilyResolver()
        count = populate_resolver_from_shioaji(resolver, api, today=date(2026, 4, 19))
        assert count == 1

    def test_idempotent_repeat_call_produces_no_rebinds(self) -> None:
        api = _fake_api({"TMF": [_mock_contract("TMFE6", delivery_date="2026/05/21")]})
        resolver = ContractFamilyResolver()
        hook_events: list = []
        resolver.add_hook(hook_events.append)

        populate_resolver_from_shioaji(resolver, api, today=date(2026, 4, 19))
        assert len(hook_events) == 1  # R1 binding installed (None -> FutureRef)

        populate_resolver_from_shioaji(resolver, api, today=date(2026, 4, 19))
        # No rebind — same snapshot.
        assert len(hook_events) == 1

    def test_none_api_is_noop(self) -> None:
        resolver = ContractFamilyResolver()
        count = populate_resolver_from_shioaji(resolver, None, today=date(2026, 4, 19))
        assert count == 0

    def test_api_without_contracts_is_noop(self) -> None:
        resolver = ContractFamilyResolver()
        api = SimpleNamespace()  # no Contracts attribute
        count = populate_resolver_from_shioaji(resolver, api, today=date(2026, 4, 19))
        assert count == 0

    def test_native_hints_carry_broker_contract(self) -> None:
        """After populate, ``snapshot.native_hint(ref)`` returns the
        original broker Contract object, keyed by canonical display."""
        c = _mock_contract("TMFE6", delivery_date="2026/05/21")
        api = _fake_api({"TMF": [c]})
        resolver = ContractFamilyResolver()
        populate_resolver_from_shioaji(resolver, api, today=date(2026, 4, 19))

        from hft_platform.contracts.ref import FutureRef

        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        # native_hint() keys on display() which is "TMFE6".
        assert resolver.snapshot.native_hint(ref) is c

    def test_native_hints_cover_broker_code_alias(self) -> None:
        """Broker ``code`` is also registered as a native_hint key, not only
        the canonical display form (useful if the two diverge)."""
        c = _mock_contract("TMFE6", delivery_date="2026/05/21")
        api = _fake_api({"TMF": [c]})
        resolver = ContractFamilyResolver()
        populate_resolver_from_shioaji(resolver, api, today=date(2026, 4, 19))

        assert resolver.snapshot.native_hints["TMFE6"] is c

    def test_rollover_scenario(self) -> None:
        """Before rollover: TMFE6 is R1. Simulate the May expiry passing
        and refresh: TMFF6 becomes R1 and hooks fire.
        """
        api_before = _fake_api(
            {
                "TMF": [
                    _mock_contract("TMFE6", delivery_date="2026/05/21"),
                    _mock_contract("TMFF6", delivery_date="2026/06/17"),
                ]
            }
        )
        api_after = _fake_api(
            {
                "TMF": [
                    _mock_contract("TMFF6", delivery_date="2026/06/17"),
                    _mock_contract("TMFG6", delivery_date="2026/07/15"),
                ]
            }
        )
        resolver = ContractFamilyResolver()
        hook_events: list = []
        resolver.add_hook(hook_events.append)

        populate_resolver_from_shioaji(resolver, api_before, today=date(2026, 5, 20))
        first_round = len(hook_events)

        populate_resolver_from_shioaji(resolver, api_after, today=date(2026, 5, 22))
        # Both R1 and R2 bindings moved forward — 2 more rebind events.
        assert len(hook_events) > first_round
        fam_r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        assert resolver.resolve_family(fam_r1).display() == "TMFF6"
