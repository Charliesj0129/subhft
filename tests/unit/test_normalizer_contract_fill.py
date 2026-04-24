"""Gate 2b: normalizer fills ``event.contract`` when resolvable."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest
import yaml

from hft_platform.contracts.ref import FutureRef, OptionRef, StockRef
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer, SymbolMetadata


@pytest.fixture
def metadata(tmp_path: Path) -> SymbolMetadata:
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "symbols": [
                    {"code": "TMFE6", "exchange": "TAIFEX", "tick_size": 1.0},
                    {"code": "TMFR1", "exchange": "TAIFEX", "tick_size": 1.0},
                    {"code": "TXO202605C23000", "exchange": "TAIFEX"},
                    {"code": "2330", "exchange": "TSE", "tick_size": 0.5},
                    {"code": "GARBAGE-SYMBOL-??", "exchange": "TSE"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return SymbolMetadata(str(cfg))


class TestSymbolMetadataContractRef:
    def test_future_month_code_parses_to_future_ref(self, metadata: SymbolMetadata) -> None:
        ref = metadata.contract_ref("TMFE6")
        assert isinstance(ref, FutureRef)
        assert ref.root == "TMF"

    def test_future_family_code_returns_none_for_event_field(self, metadata: SymbolMetadata) -> None:
        """``TMFR1`` is a family reference, not a concrete expiry. The
        per-event ``contract`` field must stay None for family-form symbols
        — the ContractResolver is the authority for concrete expiries."""
        assert metadata.contract_ref("TMFR1") is None

    def test_option_code_parses_to_option_ref(self, metadata: SymbolMetadata) -> None:
        ref = metadata.contract_ref("TXO202605C23000")
        assert isinstance(ref, OptionRef)
        assert ref.strike == 23_000

    def test_stock_code_parses_to_stock_ref(self, metadata: SymbolMetadata) -> None:
        ref = metadata.contract_ref("2330")
        assert isinstance(ref, StockRef)
        assert ref.code == "2330"

    def test_unknown_or_malformed_symbol_returns_none(self, metadata: SymbolMetadata) -> None:
        assert metadata.contract_ref("GARBAGE-SYMBOL-??") is None
        assert metadata.contract_ref("!!!") is None

    def test_missing_contract_module_returns_none(self, metadata: SymbolMetadata, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = builtins.__import__

        def fake_import(name: str, *args, **kwargs):
            if name == "hft_platform.contracts.ref":
                raise ModuleNotFoundError("No module named 'hft_platform.contracts.ref'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        cache = getattr(metadata, "_contract_ref_cache", None)
        if cache is not None:
            cache.clear()

        assert metadata.contract_ref("TMFE6") is None

    def test_cache_hit_returns_same_object(self, metadata: SymbolMetadata) -> None:
        first = metadata.contract_ref("TMFE6")
        second = metadata.contract_ref("TMFE6")
        assert first is second


class TestNormalizerFillsContract:
    def test_tick_event_carries_future_ref(self, metadata: SymbolMetadata) -> None:
        norm = MarketDataNormalizer(metadata=metadata)
        payload = {
            "code": "TMFE6",
            "close": 100.0,
            "volume": 1,
        }
        event = norm.normalize_tick(payload)
        assert event is not None
        assert isinstance(event.contract, FutureRef)
        assert event.contract.root == "TMF"

    def test_tick_event_contract_none_for_unknown_symbol(self, metadata: SymbolMetadata) -> None:
        norm = MarketDataNormalizer(metadata=metadata)
        payload = {
            "code": "GARBAGE-SYMBOL-??",
            "close": 100.0,
            "volume": 1,
        }
        event = norm.normalize_tick(payload)
        assert event is not None
        assert event.contract is None

    def test_tick_event_skips_contract_for_legacy_event_class(
        self, metadata: SymbolMetadata, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hft_platform.feed_adapter.normalizer as mod

        monkeypatch.setattr(mod, "_TICK_EVENT_SUPPORTS_CONTRACT", False)
        norm = MarketDataNormalizer(metadata=metadata)

        event = norm.normalize_tick({"code": "TMFE6", "close": 100.0, "volume": 1})

        assert event is not None
        assert event.contract is None


class TestCacheIsStable:
    def test_invalid_symbol_cached_as_none(self, metadata: SymbolMetadata) -> None:
        metadata.contract_ref("not-a-valid-code-ever")
        cache = metadata._contract_ref_cache  # type: ignore[attr-defined]
        assert cache["not-a-valid-code-ever"] is None
