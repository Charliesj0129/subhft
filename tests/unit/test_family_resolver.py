"""Gate 1 PoC: ContractFamilyResolver unit tests."""

from __future__ import annotations

from datetime import date

import pytest

from hft_platform.contracts.family_resolver import (
    ContractFamilyResolver,
    FamilyBindingChanged,
    build_snapshot_from_calendar,
)
from hft_platform.contracts.ref import (
    ContractFamily,
    FamilyCode,
    FutureRef,
    ImmutableContractSnapshot,
    Product,
)


def _tmf(month: int, year: int = 2026) -> FutureRef:
    return FutureRef(root="TMF", expiry=date(year, month, 21))


class TestBuildSnapshotFromCalendar:
    def test_r1_r2_ordered_by_expiry(self) -> None:
        cal = {"TMF": [_tmf(5), _tmf(6), _tmf(7)]}
        snap = build_snapshot_from_calendar(
            cal, today=date(2026, 4, 19), snapshot_ns=1000
        )

        fam_r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        fam_r2 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R2)

        assert snap.resolve_family(fam_r1) == FutureRef(
            "TMF", date(2026, 5, 21), FamilyCode.R1
        )
        assert snap.resolve_family(fam_r2) == FutureRef(
            "TMF", date(2026, 6, 21), FamilyCode.R2
        )

    def test_expired_refs_excluded(self) -> None:
        cal = {"TMF": [_tmf(3), _tmf(5), _tmf(6)]}
        snap = build_snapshot_from_calendar(
            cal, today=date(2026, 4, 19), snapshot_ns=0
        )
        fam_r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        assert snap.resolve_family(fam_r1).expiry.month == 5

    def test_multiple_roots(self) -> None:
        cal = {
            "TMF": [_tmf(5), _tmf(6)],
            "TXF": [
                FutureRef("TXF", date(2026, 5, 14)),
                FutureRef("TXF", date(2026, 6, 17)),
            ],
        }
        snap = build_snapshot_from_calendar(
            cal, today=date(2026, 4, 19), snapshot_ns=0
        )
        assert snap.resolve_family(
            ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        ).display() == "TMFE6"
        assert snap.resolve_family(
            ContractFamily(Product.FUTURE, "TXF", FamilyCode.R1)
        ).display() == "TXFE6"


class TestResolver:
    def test_empty_resolver_returns_none(self) -> None:
        r = ContractFamilyResolver()
        fam = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        assert r.resolve_family(fam) is None

    def test_snapshot_swap_updates_binding(self) -> None:
        r = ContractFamilyResolver()
        snap_a = build_snapshot_from_calendar(
            {"TMF": [_tmf(5), _tmf(6)]},
            today=date(2026, 4, 19),
            snapshot_ns=1000,
        )
        r.swap_snapshot(snap_a)
        fam = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        assert r.resolve_family(fam).expiry.month == 5

        # Rollover: May expires, June becomes R1.
        snap_b = build_snapshot_from_calendar(
            {"TMF": [_tmf(6), _tmf(7)]},
            today=date(2026, 5, 22),
            snapshot_ns=2000,
        )
        r.swap_snapshot(snap_b)
        assert r.resolve_family(fam).expiry.month == 6

    def test_hook_fires_on_rebind(self) -> None:
        r = ContractFamilyResolver()
        events: list[FamilyBindingChanged] = []
        r.add_hook(events.append)

        r.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5), _tmf(6)]},
                today=date(2026, 4, 19),
                snapshot_ns=1000,
            )
        )
        # First swap: both R1 and R2 are "changes" (from None).
        assert len(events) == 2

        events.clear()
        r.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(6), _tmf(7)]},
                today=date(2026, 5, 22),
                snapshot_ns=2000,
            )
        )
        # After rollover: R1 old=May, new=June; R2 old=June, new=July — both changed.
        assert len(events) == 2
        for e in events:
            assert e.old_ref is not None
            assert e.new_ref is not None

    def test_hook_does_not_fire_on_no_op_swap(self) -> None:
        r = ContractFamilyResolver()
        snap = build_snapshot_from_calendar(
            {"TMF": [_tmf(5), _tmf(6)]},
            today=date(2026, 4, 19),
            snapshot_ns=1000,
        )
        r.swap_snapshot(snap)
        events: list[FamilyBindingChanged] = []
        r.add_hook(events.append)
        # Re-apply the same snapshot — no bindings changed.
        r.swap_snapshot(snap)
        assert events == []

    def test_hook_exception_does_not_break_resolver(self) -> None:
        r = ContractFamilyResolver()

        def _boom(change: FamilyBindingChanged) -> None:
            raise RuntimeError("hook failure")

        r.add_hook(_boom)
        # Must not raise despite buggy hook.
        r.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5)]},
                today=date(2026, 4, 19),
                snapshot_ns=0,
            )
        )
        fam = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        assert r.resolve_family(fam) is not None

    def test_initial_snapshot_accepted(self) -> None:
        snap = build_snapshot_from_calendar(
            {"TMF": [_tmf(5)]},
            today=date(2026, 4, 19),
            snapshot_ns=999,
        )
        r = ContractFamilyResolver(initial=snap)
        assert r.snapshot.snapshot_ns == 999

    def test_snapshot_property_is_read_only(self) -> None:
        r = ContractFamilyResolver()
        r.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(5)]},
                today=date(2026, 4, 19),
                snapshot_ns=0,
            )
        )
        snap_before = r.snapshot
        with pytest.raises(TypeError):
            snap_before.family_map[  # type: ignore[index]
                ContractFamily(Product.FUTURE, "TXF", FamilyCode.R1)
            ] = _tmf(5)


class TestSnapshotRetention:
    def test_consumer_holding_old_snapshot_sees_consistent_view(self) -> None:
        """During a tick, a consumer reads snapshot once; later swap does
        not affect that consumer's view — which is the whole point of the
        immutable snapshot contract."""
        r = ContractFamilyResolver()
        snap_a = build_snapshot_from_calendar(
            {"TMF": [_tmf(5)]},
            today=date(2026, 4, 19),
            snapshot_ns=1000,
        )
        r.swap_snapshot(snap_a)
        consumer_view = r.snapshot  # captured at tick start

        # Mid-tick the resolver swaps to a new snapshot.
        r.swap_snapshot(
            build_snapshot_from_calendar(
                {"TMF": [_tmf(6)]},
                today=date(2026, 5, 22),
                snapshot_ns=2000,
            )
        )

        # Consumer's captured view still points at May.
        fam = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        assert consumer_view.resolve_family(fam).expiry.month == 5
        # But a fresh read reflects the new binding.
        assert r.resolve_family(fam).expiry.month == 6


class TestInvariants:
    def test_family_code_specific_is_not_a_valid_family_key(self) -> None:
        """``ContractFamily(SPECIFIC)`` is invalid — concrete refs use
        ``FutureRef(family=SPECIFIC)`` directly. This invariant is enforced
        at ``ContractFamily.__post_init__``.
        """
        with pytest.raises(ValueError):
            ContractFamily(
                product=Product.FUTURE, root="TMF", family=FamilyCode.SPECIFIC
            )

    def test_stock_family_rejected(self) -> None:
        with pytest.raises(ValueError):
            ContractFamily(
                product=Product.STOCK, root="2330", family=FamilyCode.R1
            )

    def test_snapshot_builder_produces_refs_with_matching_family_code(
        self,
    ) -> None:
        snap = build_snapshot_from_calendar(
            {"TMF": [_tmf(5), _tmf(6)]},
            today=date(2026, 4, 19),
            snapshot_ns=0,
        )
        r1 = snap.resolve_family(
            ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
        )
        r2 = snap.resolve_family(
            ContractFamily(Product.FUTURE, "TMF", FamilyCode.R2)
        )
        assert isinstance(r1, FutureRef) and r1.family is FamilyCode.R1
        assert isinstance(r2, FutureRef) and r2.family is FamilyCode.R2


class TestEmptyCalendar:
    def test_empty_calendar_produces_empty_snapshot(self) -> None:
        snap = build_snapshot_from_calendar(
            {}, today=date(2026, 4, 19), snapshot_ns=0
        )
        assert snap.family_map == {}

    def test_only_expired_refs_produces_empty_snapshot(self) -> None:
        snap = build_snapshot_from_calendar(
            {"TMF": [_tmf(3), _tmf(4)]},
            today=date(2026, 5, 1),
            snapshot_ns=0,
        )
        assert snap.family_map == {}


def test_immutable_contract_snapshot_stored_in_resolver_is_same_instance() -> None:
    snap = ImmutableContractSnapshot.build(
        family_map={}, native_hints={}, snapshot_ns=42
    )
    r = ContractFamilyResolver(initial=snap)
    assert r.snapshot is snap
