"""Gate 3 slice: OrderAdapter prefers ``intent.contract`` when the
ContractFamilyResolver recognises it.

Verifies the resolver-aware helper ``_resolve_broker_contract_code`` on
:class:`OrderAdapter` without spinning up the full dispatch loop — the
place_order coroutine has many broker-side preconditions we do not want
to fake here.
"""

from __future__ import annotations

from datetime import date

from hft_platform.contracts.family_resolver import (
    ContractFamilyResolver,
    build_snapshot_from_calendar,
)
from hft_platform.contracts.ref import FutureRef
from hft_platform.contracts.strategy import IntentType, OrderIntent, Side


def _adapter():
    """Bare-bones OrderAdapter instance with only the fields the helper reads.

    Avoids ``__init__`` side effects (metrics registry, queues, background
    tasks) that are irrelevant to contract-code resolution.
    """
    from hft_platform.observability.metrics import MetricsRegistry
    from hft_platform.order.adapter import OrderAdapter

    a = OrderAdapter.__new__(OrderAdapter)
    a._actual_to_config = {}
    a._contract_resolver = None
    a.metrics = MetricsRegistry.get()
    return a


def _counter_sum(metric, source: str) -> float:
    """Sum the current value of ``hft_order_contract_code_resolution_total``
    for a given ``source`` label across processes. prometheus_client stores
    counters as a family; iterate samples to find the matching labelset."""
    total = 0.0
    for family in metric.collect():
        for sample in family.samples:
            if sample.name.endswith("_total") and sample.labels.get("source") == source:
                total += sample.value
    return total


def _intent(symbol: str, contract=None) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="r47",
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=100_000,
        qty=1,
        contract=contract,
    )


def _populated_resolver(code: str = "TMFE6") -> ContractFamilyResolver:
    """Resolver with a single FutureRef bound; native_hint returns an object."""
    ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
    resolver = ContractFamilyResolver()
    snapshot = build_snapshot_from_calendar(
        {"TMF": [ref]},
        today=date(2026, 4, 19),
        snapshot_ns=1,
        native_hints={code: object()},
    )
    resolver.swap_snapshot(snapshot)
    return resolver


class TestLegacyPath:
    def test_no_resolver_no_contract_uses_alias_dict(self) -> None:
        a = _adapter()
        a._actual_to_config = {"TMFE6": "TMFR1"}
        assert a._resolve_broker_contract_code(_intent("TMFE6")) == "TMFR1"

    def test_no_resolver_no_contract_no_alias_uses_symbol(self) -> None:
        a = _adapter()
        assert a._resolve_broker_contract_code(_intent("TMFE6")) == "TMFE6"


class TestResolverPath:
    def test_contract_with_resolver_hit_returns_display(self) -> None:
        a = _adapter()
        a.set_contract_resolver(_populated_resolver())
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        # Alias dict is present but resolver path should win.
        a._actual_to_config = {"TMFE6": "TMFR1"}
        assert a._resolve_broker_contract_code(_intent("TMFE6", contract=ref)) == "TMFE6"

    def test_contract_with_resolver_miss_falls_back_to_alias(self) -> None:
        """Ref not in resolver's native_hints — adapter falls back."""
        a = _adapter()
        # Empty resolver: no native hints.
        a.set_contract_resolver(ContractFamilyResolver())
        a._actual_to_config = {"TMFE6": "TMFR1"}
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        assert a._resolve_broker_contract_code(_intent("TMFE6", contract=ref)) == "TMFR1"

    def test_no_contract_field_falls_back_to_alias(self) -> None:
        a = _adapter()
        a.set_contract_resolver(_populated_resolver())
        a._actual_to_config = {"TMFE6": "TMFR1"}
        # intent.contract is None — resolver doesn't apply.
        assert a._resolve_broker_contract_code(_intent("TMFE6")) == "TMFR1"

    def test_resolver_snapshot_exception_falls_back(self) -> None:
        """Defensive: resolver.snapshot raising must not break the order path."""
        a = _adapter()

        class BrokenResolver:
            @property
            def snapshot(self):
                raise RuntimeError("boom")

        a.set_contract_resolver(BrokenResolver())
        a._actual_to_config = {"TMFE6": "TMFR1"}
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        assert a._resolve_broker_contract_code(_intent("TMFE6", contract=ref)) == "TMFR1"

    def test_native_hint_none_falls_back(self) -> None:
        """Snapshot exists but native_hint returns None — fall back."""
        a = _adapter()
        resolver = ContractFamilyResolver()
        # Build a snapshot with family_map but no native_hints at all.
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        snapshot = build_snapshot_from_calendar(
            {"TMF": [ref]},
            today=date(2026, 4, 19),
            snapshot_ns=1,
            native_hints={},
        )
        resolver.swap_snapshot(snapshot)
        a.set_contract_resolver(resolver)
        a._actual_to_config = {"TMFE6": "TMFR1"}
        assert a._resolve_broker_contract_code(_intent("TMFE6", contract=ref)) == "TMFR1"


class TestObservability:
    def test_resolver_hit_increments_resolver_hit_label(self) -> None:
        a = _adapter()
        a.set_contract_resolver(_populated_resolver())
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        before = _counter_sum(a.metrics.order_contract_code_resolution_total, "resolver_hit")
        a._resolve_broker_contract_code(_intent("TMFE6", contract=ref))
        after = _counter_sum(a.metrics.order_contract_code_resolution_total, "resolver_hit")
        assert after == before + 1

    def test_alias_fallback_increments_alias_fallback_label(self) -> None:
        a = _adapter()
        a._actual_to_config = {"TMFE6": "TMFR1"}
        before = _counter_sum(a.metrics.order_contract_code_resolution_total, "alias_fallback")
        a._resolve_broker_contract_code(_intent("TMFE6"))
        after = _counter_sum(a.metrics.order_contract_code_resolution_total, "alias_fallback")
        assert after == before + 1

    def test_symbol_raw_increments_symbol_raw_label(self) -> None:
        a = _adapter()
        # Neither resolver nor alias dict has anything.
        before = _counter_sum(a.metrics.order_contract_code_resolution_total, "symbol_raw")
        a._resolve_broker_contract_code(_intent("TMFE6"))
        after = _counter_sum(a.metrics.order_contract_code_resolution_total, "symbol_raw")
        assert after == before + 1


class TestSetters:
    def test_set_contract_resolver_idempotent(self) -> None:
        a = _adapter()
        r1 = ContractFamilyResolver()
        r2 = ContractFamilyResolver()
        a.set_contract_resolver(r1)
        assert a._contract_resolver is r1
        a.set_contract_resolver(r2)
        assert a._contract_resolver is r2

    def test_set_contract_resolver_none_valid(self) -> None:
        """Explicitly clearing should revert to legacy behaviour."""
        a = _adapter()
        a.set_contract_resolver(_populated_resolver())
        a.set_contract_resolver(None)
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        a._actual_to_config = {"TMFE6": "TMFR1"}
        assert a._resolve_broker_contract_code(_intent("TMFE6", contract=ref)) == "TMFR1"
