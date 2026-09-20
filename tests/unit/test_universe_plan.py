"""The plan behind a universe roll: what is safe to adopt, and where it goes.

Production shape these are written from (THESHOW, 2026-09-16): ``symbols.yaml``
held 102 September codes that had settled. They kept failing to subscribe for
three days, one ``FeedSubscriptionPermanentlyFailed`` per code per hour, because
nothing rebuilt the universe between hand-runs of ``make rebuild-symbols-yaml``.

The rebuild itself already existed. These tests pin the two decisions around it:
refuse a universe that is not safe, and move as little as possible when adopting
one that is.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from hft_platform.config.universe_plan import (
    UniversePlanError,
    plan_shards,
    validate_universe,
)

CUTOFF = date(2026, 9, 17)


def _sym(code: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"code": code, "exchange": "FUT", "product_type": "future"}
    entry.update(extra)
    return entry


def _deliveries(mapping: dict[str, date]) -> Any:
    def delivery_of(code: str) -> date | None:
        return mapping.get(code)

    return delivery_of


class TestValidateUniverse:
    def test_rolled_universe_with_only_live_contracts_is_accepted(self) -> None:
        symbols = [_sym("TMFJ6"), _sym("TXFJ6")]
        errors = validate_universe(
            symbols,
            delivery_of=_deliveries({"TMFJ6": date(2026, 10, 21), "TXFJ6": date(2026, 10, 21)}),
            cutoff=CUTOFF,
            num_conns=4,
            per_conn_cap=120,
        )
        assert errors == []

    def test_settled_contract_is_refused_naming_the_code(self) -> None:
        """The exact September shape: TMFI6 settled 09-16, cutoff is 09-17."""
        symbols = [_sym("TMFI6"), _sym("TMFJ6")]
        errors = validate_universe(
            symbols,
            delivery_of=_deliveries({"TMFI6": date(2026, 9, 16), "TMFJ6": date(2026, 10, 21)}),
            cutoff=CUTOFF,
            num_conns=4,
            per_conn_cap=120,
        )
        assert len(errors) == 1
        assert "TMFI6@2026-09-16" in errors[0]

    def test_contract_delivering_on_the_cutoff_is_still_live(self) -> None:
        """Delivery day is a trading day until 13:30; the cutoff already encodes that."""
        errors = validate_universe(
            [_sym("TMFI6")],
            delivery_of=_deliveries({"TMFI6": date(2026, 9, 16)}),
            cutoff=date(2026, 9, 16),
            num_conns=4,
            per_conn_cap=120,
        )
        assert errors == []

    def test_empty_universe_is_refused(self) -> None:
        errors = validate_universe(
            [],
            delivery_of=_deliveries({}),
            cutoff=CUTOFF,
            num_conns=4,
            per_conn_cap=120,
        )
        assert any("at least" in err for err in errors)

    def test_universe_beyond_pool_capacity_is_refused(self) -> None:
        symbols = [_sym(f"CODE{i}") for i in range(9)]
        errors = validate_universe(
            symbols,
            delivery_of=_deliveries({}),
            cutoff=CUTOFF,
            num_conns=2,
            per_conn_cap=4,
        )
        assert any("exceeds pool capacity 8" in err for err in errors)

    def test_duplicate_code_is_refused(self) -> None:
        errors = validate_universe(
            [_sym("TMFJ6"), _sym("TMFJ6")],
            delivery_of=_deliveries({}),
            cutoff=CUTOFF,
            num_conns=4,
            per_conn_cap=120,
        )
        assert any("duplicate codes: TMFJ6" in err for err in errors)

    def test_symbol_without_a_code_is_refused(self) -> None:
        errors = validate_universe(
            [{"exchange": "FUT"}],
            delivery_of=_deliveries({}),
            cutoff=CUTOFF,
            num_conns=4,
            per_conn_cap=120,
        )
        assert any("without a code" in err for err in errors)

    def test_contract_the_broker_has_no_date_for_is_kept(self) -> None:
        """Equities have no delivery date; a missing date must not read as expired."""
        errors = validate_universe(
            [_sym("2330", exchange="TSE", product_type="stock")],
            delivery_of=_deliveries({}),
            cutoff=CUTOFF,
            num_conns=4,
            per_conn_cap=120,
        )
        assert errors == []


class TestPlanShards:
    def test_surviving_code_keeps_its_connection(self) -> None:
        """R47's contract must not be re-subscribed because an option rolled."""
        current = {0: [_sym("TMFJ6"), _sym("TXOI6")], 1: [_sym("TXFJ6")]}
        plan = plan_shards(
            current,
            [_sym("TMFJ6"), _sym("TXFJ6"), _sym("TXOJ6")],
            num_conns=2,
            per_conn_cap=120,
        )
        placed = {s["code"]: g for g, syms in plan.groups.items() for s in syms}
        assert placed["TMFJ6"] == 0
        assert placed["TXFJ6"] == 1
        assert plan.removed_codes == {"TXOI6"}
        assert plan.added_codes == {"TXOJ6"}
        assert plan.changed is True

    def test_unchanged_universe_plans_no_move(self) -> None:
        current = {0: [_sym("TMFJ6")], 1: [_sym("TXFJ6")]}
        plan = plan_shards(
            current,
            [_sym("TMFJ6"), _sym("TXFJ6")],
            num_conns=2,
            per_conn_cap=120,
        )
        assert plan.changed is False
        assert plan.added_codes == set()
        assert plan.removed_codes == set()

    def test_new_codes_go_to_the_least_loaded_connection(self) -> None:
        current = {0: [_sym("A1"), _sym("A2"), _sym("A3")], 1: [_sym("B1")]}
        plan = plan_shards(
            current,
            [_sym("A1"), _sym("A2"), _sym("A3"), _sym("B1"), _sym("N1"), _sym("N2")],
            num_conns=2,
            per_conn_cap=120,
        )
        assert sorted(plan.added[1]) == ["N1", "N2"]
        assert plan.added[0] == []

    def test_placement_is_deterministic_for_the_same_inputs(self) -> None:
        current: dict[int, list[dict[str, Any]]] = {0: [], 1: []}
        new = [_sym("Z9"), _sym("A1"), _sym("M5"), _sym("B2")]
        first = plan_shards(current, new, num_conns=2, per_conn_cap=120)
        second = plan_shards(current, list(reversed(new)), num_conns=2, per_conn_cap=120)
        assert {g: [s["code"] for s in syms] for g, syms in first.groups.items()} == {
            g: [s["code"] for s in syms] for g, syms in second.groups.items()
        }

    def test_every_entry_carries_its_connection(self) -> None:
        plan = plan_shards({0: [_sym("TMFJ6")]}, [_sym("TMFJ6"), _sym("TXFJ6")], num_conns=2, per_conn_cap=120)
        for group_id, entries in plan.groups.items():
            assert all(entry["group"] == group_id for entry in entries)

    def test_roll_that_does_not_fit_is_refused(self) -> None:
        current = {0: [_sym("A1"), _sym("A2")], 1: [_sym("B1"), _sym("B2")]}
        with pytest.raises(UniversePlanError, match="cap"):
            plan_shards(
                current,
                [_sym(f"N{i}") for i in range(5)],
                num_conns=2,
                per_conn_cap=2,
            )

    def test_connection_already_over_cap_is_refused_without_new_codes(self) -> None:
        current = {0: [_sym("A1"), _sym("A2"), _sym("A3")]}
        with pytest.raises(UniversePlanError, match="cap is 2"):
            plan_shards(
                current,
                [_sym("A1"), _sym("A2"), _sym("A3")],
                num_conns=1,
                per_conn_cap=2,
            )

    def test_codes_on_a_connection_that_no_longer_exists_are_replaced(self) -> None:
        """Shrinking the pool must not strand a code on a gone connection."""
        current = {0: [_sym("A1")], 3: [_sym("A2")]}
        plan = plan_shards(current, [_sym("A1"), _sym("A2")], num_conns=1, per_conn_cap=120)
        assert [s["code"] for s in plan.groups[0]] == ["A1", "A2"]
        assert plan.added_codes == {"A2"}

    def test_zero_connections_is_refused(self) -> None:
        with pytest.raises(UniversePlanError, match="positive"):
            plan_shards({}, [_sym("A1")], num_conns=0, per_conn_cap=120)

    def test_the_september_roll_moves_only_the_settled_month(self) -> None:
        """102 September codes out, 102 October codes in, everything else still."""
        survivors = [_sym(f"S{i}") for i in range(20)]
        september = [_sym(f"TXO{i}I6") for i in range(10)]
        october = [_sym(f"TXO{i}J6") for i in range(10)]
        current = {0: survivors[:10] + september[:5], 1: survivors[10:] + september[5:]}

        plan = plan_shards(current, survivors + october, num_conns=2, per_conn_cap=120)

        assert plan.removed_codes == {s["code"] for s in september}
        assert plan.added_codes == {s["code"] for s in october}
        placed = {s["code"]: g for g, syms in plan.groups.items() for s in syms}
        for i in range(10):
            assert placed[f"S{i}"] == 0
        for i in range(10, 20):
            assert placed[f"S{i}"] == 1
