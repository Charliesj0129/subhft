"""LOBEngine ingress invariant for unknown symbols — Hemorrhage #5.

Before this change, a tick or bid/ask event carrying a symbol with no entry
in ``SymbolMetadata`` silently allocated a fresh BookState with the default
price scale (x10000). For contracts that do not use the platform default
scale, this masked alias-resolution failures and produced subtly wrong PnL.

The new behavior is a two-tier guard:
* Permissive mode (default, ``HFT_LOB_STRICT_INGRESS=0``): log the first
  occurrence of each unknown symbol (``lob_unknown_symbol_ingress``) and
  bump the ``unknown_symbol_ingress_total`` Prometheus counter. Book state
  is still allocated so existing flows are not broken.
* Strict mode (``HFT_LOB_STRICT_INGRESS=1``): allocate no book, return
  ``None`` from ``get_book`` so downstream planes (FeatureEngine, strategy)
  fail fast instead of consuming mis-scaled prices.

The log-once set prevents flooding at the hot path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hft_platform.feed_adapter.lob_engine import LOBEngine


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HFT_LOB_STRICT_INGRESS", raising=False)


def _metadata_with(known: set[str]):
    return SimpleNamespace(meta={code: {} for code in known})


class TestPermissiveDefault:
    def test_known_symbol_allocates_book(self) -> None:
        engine = LOBEngine()
        engine.set_symbol_metadata(_metadata_with({"TMFE6"}))
        book = engine.get_book("TMFE6")
        assert book is not None
        assert "TMFE6" not in engine._unknown_symbol_warned

    def test_unknown_symbol_still_allocates_but_is_logged(self) -> None:
        engine = LOBEngine()
        engine.set_symbol_metadata(_metadata_with({"TMFE6"}))
        book = engine.get_book("UNKNOWN")
        assert book is not None, "permissive default must keep flow alive"
        assert "UNKNOWN" in engine._unknown_symbol_warned

    def test_log_fires_once_per_symbol(self, caplog: pytest.LogCaptureFixture) -> None:
        engine = LOBEngine()
        engine.set_symbol_metadata(_metadata_with({"TMFE6"}))
        engine.get_book("UNKNOWN")
        engine.get_book("UNKNOWN")
        engine.get_book("UNKNOWN")
        # Only one membership in the warned set — no repeated logging.
        assert engine._unknown_symbol_warned == {"UNKNOWN"}

    def test_no_metadata_injected_means_permissive_passthrough(self) -> None:
        engine = LOBEngine()
        book = engine.get_book("ANY_SYMBOL")
        assert book is not None
        # No metadata → treated as known (cannot compare against a missing map).
        assert "ANY_SYMBOL" not in engine._unknown_symbol_warned

    def test_empty_metadata_meta_means_permissive_passthrough(self) -> None:
        engine = LOBEngine()
        engine.set_symbol_metadata(SimpleNamespace(meta={}))
        book = engine.get_book("ANY_SYMBOL")
        assert book is not None
        assert "ANY_SYMBOL" not in engine._unknown_symbol_warned


class TestStrictMode:
    def test_strict_mode_refuses_unknown_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_LOB_STRICT_INGRESS", "1")
        engine = LOBEngine()
        engine.set_symbol_metadata(_metadata_with({"TMFE6"}))
        assert engine.get_book("UNKNOWN") is None
        assert "UNKNOWN" in engine._unknown_symbol_warned
        assert "UNKNOWN" not in engine.books

    def test_strict_mode_still_allocates_for_known_symbols(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_LOB_STRICT_INGRESS", "1")
        engine = LOBEngine()
        engine.set_symbol_metadata(_metadata_with({"TMFE6"}))
        book = engine.get_book("TMFE6")
        assert book is not None


class TestMetricIncrement:
    def test_unknown_symbol_bumps_counter(self) -> None:
        engine = LOBEngine()
        engine.set_symbol_metadata(_metadata_with({"TMFE6"}))
        counter = getattr(engine.metrics, "unknown_symbol_ingress_total", None)
        if counter is None:
            pytest.skip("metrics registry disabled in this test environment")

        sample_before = _lob_plane_sample(counter)
        engine.get_book("SURPRISE_SYMBOL")
        sample_after = _lob_plane_sample(counter)
        assert sample_after > sample_before


def _lob_plane_sample(counter) -> float:
    """Return the current value of the ``plane="lob"`` sample for the counter."""
    for metric_family in counter.collect():
        for sample in metric_family.samples:
            if sample.labels.get("plane") == "lob" and sample.name.endswith("_total"):
                return sample.value
    return 0.0
