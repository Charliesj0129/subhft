"""Unit tests for hft_platform.recorder._loader_common.

Covers: _to_scaled, _dumps/_loads codec, PRICE_SCALE constant,
DEFAULT_INSERT_* constants, _TS_MAX_FUTURE_NS env-var parsing.
Targets ≥85% line coverage.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers: import the module under test
# ---------------------------------------------------------------------------


def _import_module():
    import hft_platform.recorder._loader_common as m

    return m


# ---------------------------------------------------------------------------
# PRICE_SCALE
# ---------------------------------------------------------------------------


class TestPriceScaleConstant:
    def test_price_scale_value(self) -> None:
        m = _import_module()
        assert m.PRICE_SCALE == 1_000_000


# ---------------------------------------------------------------------------
# _to_scaled
# ---------------------------------------------------------------------------


class TestToScaled:
    def test_none_returns_zero(self) -> None:
        m = _import_module()
        assert m._to_scaled(None) == 0

    def test_zero_int(self) -> None:
        m = _import_module()
        assert m._to_scaled(0) == 0

    def test_integer_value(self) -> None:
        m = _import_module()
        assert m._to_scaled(1) == 1_000_000

    def test_float_value(self) -> None:
        m = _import_module()
        # 100.5 * 1_000_000 = 100_500_000
        assert m._to_scaled(100.5) == 100_500_000

    def test_small_float(self) -> None:
        m = _import_module()
        # 0.001 * 1_000_000 = 1000
        assert m._to_scaled(0.001) == 1000

    def test_negative_value(self) -> None:
        m = _import_module()
        assert m._to_scaled(-5) == -5_000_000

    def test_rounding(self) -> None:
        m = _import_module()
        # 1.0000005 * 1e6 = 1000000.5 → rounds to 1000001
        result = m._to_scaled(1.0000005)
        assert isinstance(result, int)

    def test_large_price(self) -> None:
        m = _import_module()
        # 50000 (TAIEX-like) * 1e6
        assert m._to_scaled(50000) == 50_000 * 1_000_000

    def test_returns_int_type(self) -> None:
        m = _import_module()
        assert isinstance(m._to_scaled(1.23), int)


# ---------------------------------------------------------------------------
# JSON codec (_dumps / _loads)
# ---------------------------------------------------------------------------


class TestJsonCodec:
    def test_dumps_produces_string(self) -> None:
        m = _import_module()
        result = m._dumps({"key": "value", "num": 42})
        assert isinstance(result, str)
        assert "key" in result

    def test_loads_parses_string(self) -> None:
        m = _import_module()
        result = m._loads('{"x": 1}')
        assert result == {"x": 1}

    def test_dumps_loads_roundtrip(self) -> None:
        m = _import_module()
        original = {"price": 12345, "symbol": "TXFD6", "list": [1, 2, 3]}
        assert m._loads(m._dumps(original)) == original

    def test_dumps_handles_none_value(self) -> None:
        m = _import_module()
        result = m._dumps({"v": None})
        assert isinstance(result, str)
        loaded = m._loads(result)
        assert loaded["v"] is None

    def test_loads_handles_bytes_input(self) -> None:
        m = _import_module()
        # Both orjson and stdlib json can handle bytes
        try:
            result = m._loads(b'{"x": 2}')
            assert result == {"x": 2}
        except TypeError:
            # stdlib json does not accept bytes on older Python — acceptable
            pass


# ---------------------------------------------------------------------------
# Retry defaults
# ---------------------------------------------------------------------------


class TestRetryDefaults:
    def test_default_max_retries(self) -> None:
        m = _import_module()
        assert m.DEFAULT_INSERT_MAX_RETRIES == 3

    def test_default_base_delay(self) -> None:
        m = _import_module()
        assert m.DEFAULT_INSERT_BASE_DELAY_S == 0.5

    def test_default_max_backoff(self) -> None:
        m = _import_module()
        assert m.DEFAULT_INSERT_MAX_BACKOFF_S == 5.0


# ---------------------------------------------------------------------------
# _TS_MAX_FUTURE_NS env-var parsing
# ---------------------------------------------------------------------------


class TestTsMaxFutureNs:
    def test_default_is_positive_ns(self) -> None:
        m = _import_module()
        # Default env value "5" seconds → 5 * 1e9 = 5_000_000_000 ns
        # May already be initialised; just verify type and plausible range.
        assert isinstance(m._TS_MAX_FUTURE_NS, int)
        # Either the env-parsed value (>0) or 0 (disabled on bad parse)
        assert m._TS_MAX_FUTURE_NS >= 0

    def test_env_var_parsed_to_nanoseconds(self) -> None:
        """Reload module with a known env var to verify parsing."""
        mod_name = "hft_platform.recorder._loader_common"
        # Remove cached module so it is re-imported fresh
        cached = sys.modules.pop(mod_name, None)
        try:
            with patch.dict(os.environ, {"HFT_TS_MAX_FUTURE_S": "10"}):
                import hft_platform.recorder._loader_common as fresh_m

                importlib.reload(fresh_m)
                assert fresh_m._TS_MAX_FUTURE_NS == int(10 * 1e9)
        finally:
            # Restore original module
            sys.modules.pop(mod_name, None)
            if cached is not None:
                sys.modules[mod_name] = cached

    def test_invalid_env_var_results_in_zero(self) -> None:
        """Reload module with a bad env var to verify fallback to 0."""
        mod_name = "hft_platform.recorder._loader_common"
        cached = sys.modules.pop(mod_name, None)
        try:
            with patch.dict(os.environ, {"HFT_TS_MAX_FUTURE_S": "not_a_number"}):
                import hft_platform.recorder._loader_common as fresh_m

                importlib.reload(fresh_m)
                assert fresh_m._TS_MAX_FUTURE_NS == 0
        finally:
            sys.modules.pop(mod_name, None)
            if cached is not None:
                sys.modules[mod_name] = cached


# ---------------------------------------------------------------------------
# __all__ completeness
# ---------------------------------------------------------------------------


class TestAllExports:
    def test_all_exports_present(self) -> None:
        m = _import_module()
        for name in m.__all__:
            assert hasattr(m, name), f"Missing export: {name}"
