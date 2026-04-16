"""Coverage tests for feature/kernel.py — missing lines 20-28, 271-282.

Lines 20-28 are the try/except import block for Rust core.
Lines 271-282 are reset_symbol/reset_all in RustFeatureKernelAdapter.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.feature.kernel import (
    RustFeatureKernelAdapter,
    _top_qty,
    rust_backend_available,
)

# ---------------------------------------------------------------------------
# Rust import fallback — lines 20-28
# The try/except block at module level is exercised by patching the module-level
# variable, which is the only practical way to cover it post-import.
# ---------------------------------------------------------------------------


class TestRustImportFallback:
    def test_rust_backend_available_returns_bool(self) -> None:
        result = rust_backend_available()
        assert isinstance(result, bool)

    def test_rust_backend_false_when_module_var_is_none(self) -> None:
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", None):
            assert rust_backend_available() is False

    def test_rust_backend_true_when_module_var_is_set(self) -> None:
        sentinel = object()
        with patch("hft_platform.feature.kernel._RUST_LOB_FEATURE_KERNEL_V1", sentinel):
            assert rust_backend_available() is True


# ---------------------------------------------------------------------------
# RustFeatureKernelAdapter.reset_symbol — lines 264-282
# ---------------------------------------------------------------------------


class TestRustFeatureKernelAdapterResetSymbol:
    def _make_adapter(self) -> RustFeatureKernelAdapter:
        return RustFeatureKernelAdapter(ema_alpha=0.25, warmup_thresholds=[8])

    def test_reset_symbol_with_callable_reset_on_kernel(self) -> None:
        """Lines 268-270: kernel.reset() called when present and callable."""
        adapter = self._make_adapter()
        mock_kernel = MagicMock()
        mock_kernel.reset = MagicMock()
        adapter._kernels["SYM"] = mock_kernel

        adapter.reset_symbol("SYM")
        mock_kernel.reset.assert_called_once()
        assert "SYM" not in adapter._kernels

    def test_reset_symbol_with_callable_reset_on_pipeline(self) -> None:
        """Lines 276-278: pipeline.reset() called when present and callable."""
        adapter = self._make_adapter()
        mock_pipeline = MagicMock()
        mock_pipeline.reset = MagicMock()
        adapter._pipelines["SYM"] = mock_pipeline

        adapter.reset_symbol("SYM")
        mock_pipeline.reset.assert_called_once()
        assert "SYM" not in adapter._pipelines

    def test_reset_symbol_kernel_exception_suppressed(self) -> None:
        """Lines 271-272: exception from kernel.reset() is logged and suppressed."""
        adapter = self._make_adapter()
        mock_kernel = MagicMock()
        mock_kernel.reset = MagicMock(side_effect=ValueError("kernel failure"))
        adapter._kernels["SYM"] = mock_kernel

        # Must not raise
        adapter.reset_symbol("SYM")
        assert "SYM" not in adapter._kernels

    def test_reset_symbol_pipeline_exception_suppressed(self) -> None:
        """Lines 280-281: exception from pipeline.reset() is logged and suppressed."""
        adapter = self._make_adapter()
        mock_pipeline = MagicMock()
        mock_pipeline.reset = MagicMock(side_effect=RuntimeError("pipeline failure"))
        adapter._pipelines["SYM"] = mock_pipeline

        adapter.reset_symbol("SYM")
        assert "SYM" not in adapter._pipelines

    def test_reset_symbol_no_reset_method_on_kernel(self) -> None:
        """Non-callable reset attribute — getattr returns None, skip calling it."""
        adapter = self._make_adapter()

        class NoReset:
            pass

        adapter._kernels["SYM"] = NoReset()
        adapter.reset_symbol("SYM")
        assert "SYM" not in adapter._kernels

    def test_reset_symbol_no_reset_method_on_pipeline(self) -> None:
        adapter = self._make_adapter()

        class NoReset:
            pass

        adapter._pipelines["SYM"] = NoReset()
        adapter.reset_symbol("SYM")
        assert "SYM" not in adapter._pipelines

    def test_reset_symbol_cleans_both_kernel_and_pipeline(self) -> None:
        adapter = self._make_adapter()
        k = MagicMock()
        k.reset = MagicMock()
        p = MagicMock()
        p.reset = MagicMock()
        adapter._kernels["SYM"] = k
        adapter._pipelines["SYM"] = p

        adapter.reset_symbol("SYM")
        k.reset.assert_called_once()
        p.reset.assert_called_once()
        assert "SYM" not in adapter._kernels
        assert "SYM" not in adapter._pipelines

    def test_reset_symbol_unknown_symbol_is_noop(self) -> None:
        adapter = self._make_adapter()
        # Should not raise even if symbol not present
        adapter.reset_symbol("DOES_NOT_EXIST")
        assert "DOES_NOT_EXIST" not in adapter._kernels
        assert "DOES_NOT_EXIST" not in adapter._pipelines

    def test_reset_all_clears_kernels_and_pipelines(self) -> None:
        """Line 284-286: reset_all clears both dicts."""
        adapter = self._make_adapter()
        adapter._kernels["A"] = MagicMock()
        adapter._kernels["B"] = MagicMock()
        adapter._pipelines["A"] = MagicMock()
        adapter.reset_all()
        assert len(adapter._kernels) == 0
        assert len(adapter._pipelines) == 0


# ---------------------------------------------------------------------------
# _top_qty in kernel.py — already tested in test_feature_kernel.py,
# but we add edge cases for the exception path.
# ---------------------------------------------------------------------------


class TestKernelTopQtyEdgeCases:
    def test_raises_internally_returns_none(self) -> None:
        """An object that raises on getitem should return None (lines 48-50)."""
        bad = MagicMock()
        bad.size = 2
        bad.__getitem__ = MagicMock(side_effect=IndexError("bad"))
        result = _top_qty(bad)
        assert result is None

    def test_single_element_top_without_qty_returns_zero(self) -> None:
        # top = (px,) → len(top) not > 1 → return 0
        assert _top_qty([(100_0000,)]) == 0
