"""Tests for Rust coerce_ns_int / coerce_ns_float — timestamp coercion."""

from __future__ import annotations

import pytest

_coerce_int = None
_coerce_float = None


def _get_funcs():
    global _coerce_int, _coerce_float
    if _coerce_int is None:
        try:
            from hft_platform.rust_core import coerce_ns_float, coerce_ns_int  # type: ignore[attr-defined]
        except ImportError:
            try:
                from rust_core import coerce_ns_float, coerce_ns_int  # type: ignore[assignment]
            except ImportError:
                pytest.skip("rust_core not available")
        _coerce_int = coerce_ns_int
        _coerce_float = coerce_ns_float
    return _coerce_int, _coerce_float


class TestCoerceNsInt:
    def test_seconds(self):
        ci, _ = _get_funcs()
        # 1_700_000_000 seconds → nanoseconds
        result = ci(1_700_000_000)
        assert result == 1_700_000_000 * 1_000_000_000

    def test_milliseconds(self):
        ci, _ = _get_funcs()
        # 1_700_000_000_000 ms → ns
        result = ci(1_700_000_000_000)
        assert result == 1_700_000_000_000 * 1_000_000

    def test_microseconds(self):
        ci, _ = _get_funcs()
        # 1_700_000_000_000_000 us → ns
        result = ci(1_700_000_000_000_000)
        assert result == 1_700_000_000_000_000 * 1_000

    def test_nanoseconds(self):
        ci, _ = _get_funcs()
        ns = 1_700_000_000_000_000_000
        assert ci(ns) == ns

    def test_zero(self):
        ci, _ = _get_funcs()
        assert ci(0) == 0

    def test_negative_seconds(self):
        ci, _ = _get_funcs()
        result = ci(-1_700_000_000)
        assert result == -1_700_000_000 * 1_000_000_000


class TestCoerceNsFloat:
    def test_seconds_float(self):
        _, cf = _get_funcs()
        result = cf(1_700_000_000.5)
        assert result == int(1_700_000_000.5 * 1e9)

    def test_milliseconds_float(self):
        _, cf = _get_funcs()
        result = cf(1_700_000_000_000.0)
        assert result == int(1_700_000_000_000.0 * 1e6)

    def test_nanoseconds_float(self):
        _, cf = _get_funcs()
        ns = 1_700_000_000_000_000_000.0
        assert cf(ns) == int(ns)


class TestParityWithPython:
    def test_int_parity(self):
        """Rust coerce_ns_int matches Python coerce_ns for int inputs."""
        from hft_platform.core.timebase import coerce_ns

        ci, _ = _get_funcs()
        test_values = [
            0,
            1_700_000_000,               # seconds
            1_700_000_000_000,            # milliseconds
            1_700_000_000_000_000,        # microseconds
            1_700_000_000_000_000_000,    # nanoseconds
        ]
        for val in test_values:
            py_result = coerce_ns(val)
            rs_result = ci(val)
            assert py_result == rs_result, f"Mismatch for {val}: py={py_result}, rs={rs_result}"

    def test_float_parity(self):
        """Rust coerce_ns_float matches Python coerce_ns for float inputs."""
        from hft_platform.core.timebase import coerce_ns

        _, cf = _get_funcs()
        test_values = [
            1_700_000_000.0,
            1_700_000_000.123,
            1_700_000_000_000.0,
            1_700_000_000_000_000_000.0,
        ]
        for val in test_values:
            py_result = coerce_ns(val)
            rs_result = cf(val)
            assert py_result == rs_result, f"Mismatch for {val}: py={py_result}, rs={rs_result}"
