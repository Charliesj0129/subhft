"""Gate 3 slice: ExposureKey.from_intent canonicalises via ContractRef.

When ``intent.contract`` is set, the exposure bucket is keyed on
``contract.display()``. When not set, it falls back to ``intent.symbol``.
Two intents referring to the same underlying contract should hash into
the same bucket regardless of which path populated the key.
"""

from __future__ import annotations

from datetime import date

from hft_platform.contracts.ref import FutureRef
from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.gateway.exposure import ExposureKey, ExposureStore


def _intent(symbol: str, contract=None, intent_id: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="r47",
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=100_000,
        qty=1,
        contract=contract,
    )


class TestFromIntent:
    def test_legacy_intent_uses_symbol(self) -> None:
        key = ExposureKey.from_intent(_intent("TMFE6"))
        assert key.symbol == "TMFE6"
        assert key.strategy_id == "r47"
        assert key.account == "default"
        assert key.contract is None

    def test_structured_intent_uses_contract_display(self) -> None:
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        key = ExposureKey.from_intent(_intent("TMFE6", contract=ref))
        assert key.symbol == "TMFE6"  # equals display() of FutureRef
        assert key.contract is ref

    def test_structured_symbol_derived_from_contract_not_symbol_field(self) -> None:
        """If someone passes a garbage ``symbol`` but a correct contract,
        ``from_intent`` should prefer the contract's display — that is the
        whole point of Gate 3."""
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        key = ExposureKey.from_intent(_intent("GARBAGE_STR", contract=ref))
        assert key.symbol == "TMFE6"
        assert key.contract is ref

    def test_custom_account_accepted(self) -> None:
        key = ExposureKey.from_intent(_intent("TMFE6"), account="prop-desk-1")
        assert key.account == "prop-desk-1"

    def test_contract_display_failure_falls_back_to_symbol(self) -> None:
        """Defensive: if contract.display() raises, we must not crash the
        exposure check — fall back to the raw intent.symbol."""

        class BrokenRef:
            def display(self):
                raise RuntimeError("bad contract")

        key = ExposureKey.from_intent(_intent("TMFE6", contract=BrokenRef()))
        assert key.symbol == "TMFE6"


class TestBucketParity:
    def test_structured_and_legacy_intents_hit_same_bucket(self) -> None:
        """An intent with ``contract`` set and an identical-symbol legacy
        intent must land in the same exposure bucket."""
        store = ExposureStore()
        ref = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        legacy = _intent("TMFE6")
        structured = _intent("TMFE6", contract=ref, intent_id=2)

        store.check_and_update(ExposureKey.from_intent(legacy), legacy, order_key="o1")
        store.check_and_update(
            ExposureKey.from_intent(structured), structured, order_key="o2"
        )

        # Both reservations share the same (account, strategy, symbol) bucket.
        exposure = store.get_exposure("default", "r47", "TMFE6")
        assert exposure == 2 * 100_000 * 1


class TestEqualitySemantics:
    def test_contract_field_excluded_from_equality(self) -> None:
        """``contract`` is carried for observability but NOT compared —
        otherwise a rollover that rebinds R1 to a new month would
        spuriously split buckets even when ``symbol`` stayed the same."""
        ref_a = FutureRef(root="TMF", expiry=date(2026, 5, 21))
        ref_b = FutureRef(root="TMF", expiry=date(2026, 6, 17))
        k1 = ExposureKey(account="d", strategy_id="s", symbol="TMFE6", contract=ref_a)
        k2 = ExposureKey(account="d", strategy_id="s", symbol="TMFE6", contract=ref_b)
        assert k1 == k2

    def test_symbol_drives_equality(self) -> None:
        k1 = ExposureKey(account="d", strategy_id="s", symbol="TMFE6")
        k2 = ExposureKey(account="d", strategy_id="s", symbol="TMFF6")
        assert k1 != k2
