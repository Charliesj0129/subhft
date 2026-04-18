"""ContractRef ADT tests — Gate 0 of the Option-3 migration.

Validates that structured contract identity (FutureRef/OptionRef/StockRef +
ContractFamily) is sound, hashable, frozen, and round-trips through the
canonical display form.
"""

from __future__ import annotations

from datetime import date
from types import MappingProxyType

import pytest

from hft_platform.contracts.ref import (
    ContractFamily,
    ContractRef,
    FamilyCode,
    FutureRef,
    ImmutableContractSnapshot,
    OptionRef,
    Product,
    Right,
    StockRef,
    parse_display,
)


class TestFutureRef:
    def test_display_shioaji_convention_may(self) -> None:
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        assert ref.display() == "TMFE6"

    def test_display_january(self) -> None:
        ref = FutureRef(root="TXF", expiry=date(2026, 1, 15))
        assert ref.display() == "TXFA6"

    def test_display_december(self) -> None:
        ref = FutureRef(root="MXF", expiry=date(2027, 12, 15))
        assert ref.display() == "MXFL7"

    def test_frozen(self) -> None:
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        with pytest.raises((AttributeError, TypeError)):
            ref.root = "TXF"  # type: ignore[misc]

    def test_equal_and_hashable(self) -> None:
        a = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        b = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        c = FutureRef(root="TMF", expiry=date(2026, 6, 15))
        assert a == b
        assert a != c
        assert hash(a) == hash(b)
        d: dict[FutureRef, int] = {a: 1}
        assert d[b] == 1

    def test_family_defaults_to_specific(self) -> None:
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        assert ref.family is FamilyCode.SPECIFIC


class TestOptionRef:
    def test_display_call(self) -> None:
        ref = OptionRef(
            root="TXO", expiry=date(2026, 5, 21), strike=23000, right=Right.CALL
        )
        assert ref.display() == "TXO202605C23000"

    def test_display_put(self) -> None:
        ref = OptionRef(
            root="TXO", expiry=date(2026, 5, 21), strike=22500, right=Right.PUT
        )
        assert ref.display() == "TXO202605P22500"

    def test_hashable(self) -> None:
        a = OptionRef("TXO", date(2026, 5, 21), 23000, Right.CALL)
        b = OptionRef("TXO", date(2026, 5, 21), 23000, Right.CALL)
        c = OptionRef("TXO", date(2026, 5, 21), 23000, Right.PUT)
        assert a == b
        assert a != c
        assert hash(a) == hash(b)


class TestStockRef:
    def test_display(self) -> None:
        assert StockRef(code="2330").display() == "2330"

    def test_hashable(self) -> None:
        assert hash(StockRef("2330")) == hash(StockRef("2330"))


class TestContractFamily:
    def test_valid_r1_future(self) -> None:
        fam = ContractFamily(product=Product.FUTURE, root="TMF", family=FamilyCode.R1)
        assert fam.root == "TMF"
        assert fam.family is FamilyCode.R1

    def test_valid_r2_option(self) -> None:
        fam = ContractFamily(product=Product.OPTION, root="TXO", family=FamilyCode.R2)
        assert fam.family is FamilyCode.R2

    def test_reject_stock(self) -> None:
        with pytest.raises(ValueError, match="not applicable to stocks"):
            ContractFamily(product=Product.STOCK, root="2330", family=FamilyCode.R1)

    def test_reject_specific_family(self) -> None:
        with pytest.raises(ValueError, match="requires R1/R2"):
            ContractFamily(
                product=Product.FUTURE, root="TMF", family=FamilyCode.SPECIFIC
            )

    def test_hashable(self) -> None:
        a = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        b = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        c = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R2)
        assert a == b
        assert a != c
        assert hash(a) == hash(b)


class TestContractRefUnion:
    """Verify the union type supports isinstance dispatch (PEP 604)."""

    def test_isinstance_future(self) -> None:
        ref: ContractRef = FutureRef("TMF", date(2026, 5, 21))
        assert isinstance(ref, ContractRef)

    def test_isinstance_option(self) -> None:
        ref: ContractRef = OptionRef("TXO", date(2026, 5, 21), 23000, Right.CALL)
        assert isinstance(ref, ContractRef)

    def test_isinstance_stock(self) -> None:
        ref: ContractRef = StockRef("2330")
        assert isinstance(ref, ContractRef)


class TestParseDisplay:
    def test_parse_future_may_2026(self) -> None:
        ref = parse_display("TMFE6", base_year=2026)
        assert isinstance(ref, FutureRef)
        assert ref.root == "TMF"
        assert ref.expiry.year == 2026
        assert ref.expiry.month == 5

    def test_parse_future_family_r1(self) -> None:
        ref = parse_display("TMFR1", base_year=2026)
        assert isinstance(ref, ContractFamily)
        assert ref.root == "TMF"
        assert ref.family is FamilyCode.R1

    def test_parse_future_family_r2(self) -> None:
        ref = parse_display("TXFR2", base_year=2026)
        assert isinstance(ref, ContractFamily)
        assert ref.family is FamilyCode.R2

    def test_parse_option_call(self) -> None:
        ref = parse_display("TXO202605C23000", base_year=2026)
        assert isinstance(ref, OptionRef)
        assert ref.root == "TXO"
        assert ref.strike == 23000
        assert ref.right is Right.CALL

    def test_parse_option_put(self) -> None:
        ref = parse_display("TXO202605P22500", base_year=2026)
        assert isinstance(ref, OptionRef)
        assert ref.right is Right.PUT

    def test_parse_stock(self) -> None:
        ref = parse_display("2330", base_year=2026)
        assert isinstance(ref, StockRef)
        assert ref.code == "2330"

    def test_parse_stock_5_digit(self) -> None:
        ref = parse_display("00878", base_year=2026)
        assert isinstance(ref, StockRef)
        assert ref.code == "00878"

    def test_roundtrip_future(self) -> None:
        original = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        parsed = parse_display(original.display(), base_year=2026)
        assert isinstance(parsed, FutureRef)
        assert parsed.root == original.root
        assert parsed.expiry.year == original.expiry.year
        assert parsed.expiry.month == original.expiry.month

    def test_roundtrip_option(self) -> None:
        original = OptionRef("TXO", date(2026, 5, 21), 23000, Right.CALL)
        parsed = parse_display(original.display(), base_year=2026)
        assert isinstance(parsed, OptionRef)
        assert parsed.root == original.root
        assert parsed.strike == original.strike
        assert parsed.right is original.right

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_display("not-a-contract!!!", base_year=2026)

    def test_year_rollover_wraps_forward(self) -> None:
        """Single-digit year '0' with base_year 2026 resolves to 2030 (next decade)."""
        ref = parse_display("TMFA0", base_year=2026)
        assert isinstance(ref, FutureRef)
        assert ref.expiry.year == 2030


class TestImmutableContractSnapshot:
    def _fam(self) -> ContractFamily:
        return ContractFamily(product=Product.FUTURE, root="TMF", family=FamilyCode.R1)

    def test_build_and_resolve_family(self) -> None:
        fam = self._fam()
        ref = FutureRef("TMF", date(2026, 5, 21))
        snap = ImmutableContractSnapshot.build(
            family_map={fam: ref},
            native_hints={"TMFE6": "broker-native-obj"},
            snapshot_ns=1_700_000_000_000_000_000,
        )
        assert snap.resolve_family(fam) == ref
        assert snap.native_hint(ref) == "broker-native-obj"

    def test_miss_returns_none(self) -> None:
        snap = ImmutableContractSnapshot.build(
            family_map={}, native_hints={}, snapshot_ns=0
        )
        assert snap.resolve_family(self._fam()) is None
        assert snap.native_hint(FutureRef("TMF", date(2026, 5, 21))) is None

    def test_family_map_is_read_only(self) -> None:
        fam = self._fam()
        snap = ImmutableContractSnapshot.build(
            family_map={fam: FutureRef("TMF", date(2026, 5, 21))},
            native_hints={},
            snapshot_ns=0,
        )
        assert isinstance(snap.family_map, MappingProxyType)
        with pytest.raises(TypeError):
            snap.family_map[fam] = FutureRef("TXF", date(2027, 1, 15))  # type: ignore[index]

    def test_native_hints_is_read_only(self) -> None:
        snap = ImmutableContractSnapshot.build(
            family_map={}, native_hints={"X": object()}, snapshot_ns=0
        )
        assert isinstance(snap.native_hints, MappingProxyType)
        with pytest.raises(TypeError):
            snap.native_hints["Y"] = object()  # type: ignore[index]

    def test_snapshot_is_frozen(self) -> None:
        snap = ImmutableContractSnapshot.build(
            family_map={}, native_hints={}, snapshot_ns=1234
        )
        with pytest.raises((AttributeError, TypeError)):
            snap.snapshot_ns = 5678  # type: ignore[misc]


class TestMatchDispatch:
    """Exercise the intended match/isinstance dispatch pattern for consumers."""

    def _classify(self, ref: ContractRef) -> str:
        match ref:
            case FutureRef():
                return "future"
            case OptionRef():
                return "option"
            case StockRef():
                return "stock"

    def test_future_classified(self) -> None:
        assert self._classify(FutureRef("TMF", date(2026, 5, 21))) == "future"

    def test_option_classified(self) -> None:
        assert (
            self._classify(OptionRef("TXO", date(2026, 5, 21), 23000, Right.CALL))
            == "option"
        )

    def test_stock_classified(self) -> None:
        assert self._classify(StockRef("2330")) == "stock"
