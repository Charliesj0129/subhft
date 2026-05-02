"""Tests for module-level import fallback paths in engine/event_bus.py.

Covers lines 32-34, 40-46, 51-58 by reloading the module with poisoned
sys.modules to trigger the Rust import fallback branches.
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import patch

import hft_platform.engine.event_bus as _event_bus_mod


def _reload_event_bus(env_overrides: dict[str, str] | None = None):
    """Reload engine/event_bus with optional env var overrides.

    Returns the freshly reloaded module.
    """
    if env_overrides:
        with patch.dict("os.environ", env_overrides):
            return importlib.reload(_event_bus_mod)
    return importlib.reload(_event_bus_mod)


def _restore_event_bus():
    """Restore the event_bus module to its normal imported state."""
    importlib.reload(_event_bus_mod)


# ---------------------------------------------------------------------------
# Lines 40-46: outer except — BOTH imports fail, all factories set to None.
# This also implicitly covers lines 32-34 (inner except) on the way down.
# ---------------------------------------------------------------------------


class TestRustImportOuterFallback:
    """Cover lines 32-34, 40-46: both rust_core imports fail."""

    def test_all_factories_none_when_rust_unavailable(self):
        saved_hft = sys.modules.get("hft_platform.rust_core")
        saved_bare = sys.modules.get("rust_core")
        try:
            # Poison both import paths so importlib.import_module raises
            sys.modules["hft_platform.rust_core"] = None  # type: ignore[assignment]
            sys.modules["rust_core"] = None  # type: ignore[assignment]
            mod = _reload_event_bus()
            assert mod._rust_core is None
            assert mod._RUST_RING_FACTORY is None
            assert mod._RUST_TICK_RING_FACTORY is None
            assert mod._RUST_BIDASK_RING_FACTORY is None
            assert mod._RUST_LOBSTATS_RING_FACTORY is None
        finally:
            if saved_hft is not None:
                sys.modules["hft_platform.rust_core"] = saved_hft
            else:
                sys.modules.pop("hft_platform.rust_core", None)
            if saved_bare is not None:
                sys.modules["rust_core"] = saved_bare
            else:
                sys.modules.pop("rust_core", None)
            _restore_event_bus()


# ---------------------------------------------------------------------------
# Lines 32-34: inner except — hft_platform.rust_core fails, tries bare
# rust_core.  We poison only hft_platform.rust_core.
# ---------------------------------------------------------------------------


class TestRustImportInnerFallback:
    """Cover lines 32-34: first import fails, second may succeed."""

    def test_inner_fallback_path_executed(self):
        saved = sys.modules.get("hft_platform.rust_core")
        try:
            sys.modules["hft_platform.rust_core"] = None  # type: ignore[assignment]
            mod = _reload_event_bus()
            # Either bare rust_core succeeded (line 34) or outer except fired (40-46)
            # Both paths are valid coverage gains.
            assert mod._rust_core is not None or mod._RUST_RING_FACTORY is None
        finally:
            if saved is not None:
                sys.modules["hft_platform.rust_core"] = saved
            else:
                sys.modules.pop("hft_platform.rust_core", None)
            _restore_event_bus()


# ---------------------------------------------------------------------------
# Lines 51-58: FastTypedRingBuffer fallback when HFT_BUS_MODE=rust_typed
# but the attribute is missing from rust_core.
# ---------------------------------------------------------------------------


class TestFastTypedRingBufferFallback:
    """Cover lines 51-58: rust_typed mode fallback to python."""

    def test_typed_ring_fallback_when_rust_core_none(self):
        """When rust_core is None, FastTypedRingBuffer path falls back."""
        saved_hft = sys.modules.get("hft_platform.rust_core")
        saved_bare = sys.modules.get("rust_core")
        try:
            sys.modules["hft_platform.rust_core"] = None  # type: ignore[assignment]
            sys.modules["rust_core"] = None  # type: ignore[assignment]
            mod = _reload_event_bus(env_overrides={"HFT_BUS_MODE": "rust_typed"})
            assert mod._FastTypedRingBuffer is None
            assert mod._BUS_MODE == "python"
        finally:
            if saved_hft is not None:
                sys.modules["hft_platform.rust_core"] = saved_hft
            else:
                sys.modules.pop("hft_platform.rust_core", None)
            if saved_bare is not None:
                sys.modules["rust_core"] = saved_bare
            else:
                sys.modules.pop("rust_core", None)
            _restore_event_bus()

    def test_typed_ring_fallback_when_attr_missing(self):
        """When rust_core exists but lacks FastTypedRingBuffer."""
        import types

        saved_hft = sys.modules.get("hft_platform.rust_core")
        saved_bare = sys.modules.get("rust_core")
        try:
            stub = types.ModuleType("hft_platform.rust_core")
            stub.FastRingBuffer = None  # type: ignore[attr-defined]
            sys.modules["hft_platform.rust_core"] = stub
            mod = _reload_event_bus(env_overrides={"HFT_BUS_MODE": "rust_typed"})
            assert mod._FastTypedRingBuffer is None
            assert mod._BUS_MODE == "python"
        finally:
            if saved_hft is not None:
                sys.modules["hft_platform.rust_core"] = saved_hft
            else:
                sys.modules.pop("hft_platform.rust_core", None)
            if saved_bare is not None:
                sys.modules["rust_core"] = saved_bare
            else:
                sys.modules.pop("rust_core", None)
            _restore_event_bus()
