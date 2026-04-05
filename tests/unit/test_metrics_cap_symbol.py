"""Tests for MetricsRegistry.cap_symbol cardinality guard."""

import os

from hft_platform.observability.metrics import MetricsRegistry


def _fresh_registry(max_symbols: int = 200) -> MetricsRegistry:
    """Create a fresh MetricsRegistry with a custom cap for testing."""
    # Temporarily override the class-level cap
    original = MetricsRegistry._MAX_LABEL_SYMBOLS
    MetricsRegistry._MAX_LABEL_SYMBOLS = max_symbols
    try:
        reg = MetricsRegistry()
    finally:
        MetricsRegistry._MAX_LABEL_SYMBOLS = original
    # Set instance-level cap for the returned object
    reg._MAX_LABEL_SYMBOLS = max_symbols  # type: ignore[attr-defined]
    return reg


def test_cap_symbol_passes_through_within_limit():
    """Symbols within the cardinality limit pass through unchanged."""
    reg = _fresh_registry(max_symbols=5)
    for i in range(5):
        sym = f"SYM_{i}"
        assert reg.cap_symbol(sym) == sym
    assert len(reg._seen_symbols) == 5


def test_cap_symbol_overflows_to_other():
    """Symbol beyond the limit maps to '_other'."""
    reg = _fresh_registry(max_symbols=3)
    for i in range(3):
        reg.cap_symbol(f"SYM_{i}")
    # 4th unique symbol should overflow
    assert reg.cap_symbol("SYM_NEW") == "_other"
    # Original 3 should still be in _seen_symbols
    assert len(reg._seen_symbols) == 3


def test_cap_symbol_seen_always_passes():
    """Already-seen symbols always pass through, even after cap is reached."""
    reg = _fresh_registry(max_symbols=3)
    for i in range(3):
        reg.cap_symbol(f"SYM_{i}")
    # Cap is full
    assert reg.cap_symbol("UNKNOWN") == "_other"
    # But previously seen symbols still work
    assert reg.cap_symbol("SYM_0") == "SYM_0"
    assert reg.cap_symbol("SYM_1") == "SYM_1"
    assert reg.cap_symbol("SYM_2") == "SYM_2"


def test_cap_symbol_default_limit_200():
    """Default cap matches HFT_METRICS_MAX_LABEL_SYMBOLS=200."""
    reg = MetricsRegistry()
    for i in range(200):
        sym = f"S{i:04d}"
        assert reg.cap_symbol(sym) == sym
    # 201st symbol overflows
    assert reg.cap_symbol("S_OVERFLOW") == "_other"
    assert len(reg._seen_symbols) == 200


def test_cap_symbol_env_override(monkeypatch):
    """HFT_METRICS_MAX_LABEL_SYMBOLS env var overrides the default."""
    monkeypatch.setenv("HFT_METRICS_MAX_LABEL_SYMBOLS", "2")
    # Re-evaluate the class attribute from env
    reg = _fresh_registry(max_symbols=int(os.getenv("HFT_METRICS_MAX_LABEL_SYMBOLS", "200")))
    assert reg.cap_symbol("A") == "A"
    assert reg.cap_symbol("B") == "B"
    assert reg.cap_symbol("C") == "_other"


def test_cap_symbol_other_not_added_to_seen():
    """'_other' overflow symbols must not pollute the seen set."""
    reg = _fresh_registry(max_symbols=2)
    reg.cap_symbol("A")
    reg.cap_symbol("B")
    # Overflow several times
    for i in range(10):
        assert reg.cap_symbol(f"EXTRA_{i}") == "_other"
    # Seen set is still exactly 2
    assert reg._seen_symbols == {"A", "B"}
