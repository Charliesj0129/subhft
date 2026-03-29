"""Extended coverage for feature/engine.py — init variants, profile, warmup, reset_all, emit toggle."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from hft_platform.events import LOBStatsEvent
from hft_platform.feature.engine import (
    QUALITY_FLAG_GAP,
    QUALITY_FLAG_STALE_INPUT,
    QUALITY_FLAG_STATE_RESET,
    FeatureEngine,
    _StatsTupleProxy,
)
from hft_platform.feature.profile import FeatureProfile
from hft_platform.feature.registry import (
    FeatureRegistry,
    FeatureSet,
    FeatureSpec,
    build_default_lob_feature_set_v1,
    default_feature_registry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stats(
    symbol: str = "TXFD6",
    ts: int = 1_000_000_000,
    bid: int = 200_000_000,
    ask: int = 200_010_000,
    bq: int = 50,
    aq: int = 30,
) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.0,
        best_bid=bid,
        best_ask=ask,
        bid_depth=bq,
        ask_depth=aq,
    )


def _make_profile(
    *,
    feature_set_id: str = "lob_shared_v3",
    schema_version: int | None = 3,
    params: dict | None = None,
) -> FeatureProfile:
    return FeatureProfile(
        profile_id="test-profile",
        feature_set_id=feature_set_id,
        schema_version=schema_version,
        params=params or {},
    )


# ---------------------------------------------------------------------------
# FeatureEngine __init__ variants
# ---------------------------------------------------------------------------


class TestFeatureEngineInit:
    def test_default_init(self) -> None:
        eng = FeatureEngine()
        assert eng.feature_set_id() == "lob_shared_v3"
        assert eng.schema_version() == 3
        assert eng.kernel_backend() == "python"
        assert eng.active_profile_id() is None

    def test_init_with_explicit_registry(self) -> None:
        reg = default_feature_registry()
        eng = FeatureEngine(registry=reg)
        assert eng.feature_set_id() == "lob_shared_v3"

    def test_init_with_feature_set_id(self) -> None:
        eng = FeatureEngine(feature_set_id="lob_shared_v2")
        assert eng.feature_set_id() == "lob_shared_v2"

    def test_init_emit_events_false(self) -> None:
        eng = FeatureEngine(emit_events=False)
        result = eng.process_lob_stats(_stats())
        assert result is None  # no event emitted

    def test_init_emit_events_true(self) -> None:
        eng = FeatureEngine(emit_events=True)
        result = eng.process_lob_stats(_stats())
        assert result is not None

    def test_init_emit_events_env_off(self) -> None:
        with patch.dict(os.environ, {"HFT_FEATURE_ENGINE_EMIT_EVENTS": "0"}):
            eng = FeatureEngine()
        result = eng.process_lob_stats(_stats())
        assert result is None

    def test_init_emit_events_env_false(self) -> None:
        with patch.dict(os.environ, {"HFT_FEATURE_ENGINE_EMIT_EVENTS": "false"}):
            eng = FeatureEngine()
        result = eng.process_lob_stats(_stats())
        assert result is None

    def test_init_kernel_backend_invalid_falls_back_python(self) -> None:
        eng = FeatureEngine(kernel_backend="invalid_backend_xyz")
        assert eng.kernel_backend() == "python"

    def test_init_kernel_backend_rust_fallback_when_unavailable(self) -> None:
        with patch("hft_platform.feature.engine._RUST_LOB_FEATURE_KERNEL_V1", None):
            eng = FeatureEngine(kernel_backend="rust")
        assert eng.kernel_backend() == "python"

    def test_init_with_profile(self) -> None:
        profile = _make_profile(params={"ema_window": 4})
        eng = FeatureEngine(feature_profile=profile)
        assert eng.active_profile_id() == "test-profile"
        params = eng.profile_params()
        assert params["ema_window"] == 4


# ---------------------------------------------------------------------------
# FeatureProfile apply/reject
# ---------------------------------------------------------------------------


class TestFeatureEngineProfile:
    def test_apply_profile_sets_ema_alpha(self) -> None:
        eng = FeatureEngine()
        profile = _make_profile(params={"ema_window": 4})
        eng.apply_profile(profile)
        # ema_alpha = 2/(4+1) = 0.4
        assert eng._ema_alpha == pytest.approx(0.4)

    def test_apply_profile_ofi_disabled(self) -> None:
        eng = FeatureEngine()
        profile = _make_profile(params={"ofi_enabled": "false"})
        eng.apply_profile(profile)
        assert eng._ofi_enabled is False

    def test_apply_profile_ofi_zero_disabled(self) -> None:
        eng = FeatureEngine()
        profile = _make_profile(params={"ofi_enabled": "0"})
        eng.apply_profile(profile)
        assert eng._ofi_enabled is False

    def test_apply_profile_wrong_feature_set_id_raises(self) -> None:
        eng = FeatureEngine()
        bad_profile = _make_profile(feature_set_id="wrong_set")
        with pytest.raises(ValueError, match="wrong_set"):
            eng.apply_profile(bad_profile)

    def test_apply_profile_schema_version_too_high_raises(self) -> None:
        eng = FeatureEngine()
        future_profile = _make_profile(schema_version=999)
        with pytest.raises(ValueError, match="schema_version=999"):
            eng.apply_profile(future_profile)

    def test_apply_profile_schema_version_none_accepted(self) -> None:
        eng = FeatureEngine()
        profile = _make_profile(schema_version=None)
        eng.apply_profile(profile)
        assert eng.active_profile_id() == "test-profile"

    def test_profile_params_empty_when_no_profile(self) -> None:
        eng = FeatureEngine()
        assert eng.profile_params() == {}


# ---------------------------------------------------------------------------
# runtime_status
# ---------------------------------------------------------------------------


class TestRuntimeStatus:
    def test_runtime_status_keys(self) -> None:
        eng = FeatureEngine()
        status = eng.runtime_status()
        assert "feature_set_id" in status
        assert "schema_version" in status
        assert "kernel_backend" in status
        assert "rust_backend_available" in status
        assert "emit_events" in status
        assert "active_profile_id" in status
        assert "profile_params" in status
        assert "tracked_symbols" in status

    def test_runtime_status_tracked_symbols_increments(self) -> None:
        eng = FeatureEngine()
        assert eng.runtime_status()["tracked_symbols"] == 0
        eng.process_lob_stats(_stats(symbol="SYM_A"))
        assert eng.runtime_status()["tracked_symbols"] == 1
        eng.process_lob_stats(_stats(symbol="SYM_B"))
        assert eng.runtime_status()["tracked_symbols"] == 2


# ---------------------------------------------------------------------------
# reset_all
# ---------------------------------------------------------------------------


class TestResetAll:
    def test_reset_all_clears_all_symbols(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="A"))
        eng.process_lob_stats(_stats(symbol="B"))
        assert eng.has_symbol("A")
        assert eng.has_symbol("B")

        eng.reset_all()
        assert not eng.has_symbol("A")
        assert not eng.has_symbol("B")

    def test_reset_all_sets_quality_flag_on_next_update(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="A"))
        eng.reset_all()
        evt = eng.process_lob_stats(_stats(symbol="A"))
        assert evt is not None
        assert evt.quality_flags & QUALITY_FLAG_STATE_RESET


# ---------------------------------------------------------------------------
# _StatsTupleProxy
# ---------------------------------------------------------------------------


class TestStatsTupleProxy:
    def test_all_properties(self) -> None:
        t = ("TXFD6", 123456, 400000000, 10000, 0.5, 200000000, 200010000, 50, 30)
        proxy = _StatsTupleProxy(t)
        assert proxy.symbol == "TXFD6"
        assert proxy.ts == 123456
        assert proxy.mid_price_x2 == 400000000
        assert proxy.spread_scaled == 10000
        assert proxy.imbalance == 0.5
        assert proxy.best_bid == 200000000
        assert proxy.best_ask == 200010000
        assert proxy.bid_depth == 50
        assert proxy.ask_depth == 30


# ---------------------------------------------------------------------------
# warmup_ready_mask
# ---------------------------------------------------------------------------


class TestWarmupReadyMask:
    def test_first_tick_partial_warmup(self) -> None:
        eng = FeatureEngine(emit_events=True)
        evt = eng.process_lob_stats(_stats())
        assert evt is not None
        # On first tick (warm_count=1), features with warmup_min_events=1 are ready,
        # features with warmup_min_events=2 (OFI, EMA) are NOT
        fs = build_default_lob_feature_set_v1()
        for i, spec in enumerate(fs.features):
            bit = (evt.warmup_ready_mask >> i) & 1
            if spec.warmup_min_events <= 1:
                assert bit == 1, f"{spec.feature_id} should be warm"
            else:
                assert bit == 0, f"{spec.feature_id} should NOT be warm"

    def test_second_tick_all_warm(self) -> None:
        eng = FeatureEngine(emit_events=True, feature_set_id="lob_shared_v1")
        eng.process_lob_stats(_stats(ts=1))
        evt = eng.process_lob_stats(_stats(ts=2))
        assert evt is not None
        # All v1 features have warmup_min_events <= 2
        fs = build_default_lob_feature_set_v1()
        expected_mask = (1 << len(fs.features)) - 1
        assert evt.warmup_ready_mask == expected_mask


# ---------------------------------------------------------------------------
# OFI disabled via profile
# ---------------------------------------------------------------------------


class TestOfiDisabledProfile:
    def test_ofi_fields_zero_when_disabled(self) -> None:
        profile = _make_profile(params={"ofi_enabled": "off"})
        eng = FeatureEngine(feature_profile=profile)
        eng.process_lob_stats(_stats(ts=1))
        evt = eng.process_lob_stats(_stats(ts=2, bid=200_010_000, ask=200_020_000))
        assert evt is not None
        assert evt.get("ofi_l1_raw") == 0
        assert evt.get("ofi_l1_cum") == 0
        assert evt.get("ofi_l1_ema8") == 0


# ---------------------------------------------------------------------------
# get_feature_view
# ---------------------------------------------------------------------------


class TestGetFeatureView:
    def test_view_contains_all_fields(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(ts=42))
        view = eng.get_feature_view("TXFD6")
        assert view is not None
        assert view["symbol"] == "TXFD6"
        assert view["feature_set_id"] == "lob_shared_v3"
        assert view["schema_version"] == 3
        assert view["seq"] == 1
        assert "feature_ids" in view
        assert "values" in view
        assert len(view["values"]) == len(view["feature_ids"])

    def test_view_with_profile(self) -> None:
        profile = _make_profile()
        eng = FeatureEngine(feature_profile=profile)
        eng.process_lob_stats(_stats())
        view = eng.get_feature_view("TXFD6")
        assert view is not None
        assert view["feature_profile_id"] == "test-profile"


# ---------------------------------------------------------------------------
# has_symbol
# ---------------------------------------------------------------------------


class TestHasSymbol:
    def test_false_before_processing(self) -> None:
        eng = FeatureEngine()
        assert eng.has_symbol("TXFD6") is False

    def test_true_after_processing(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats())
        assert eng.has_symbol("TXFD6") is True

    def test_false_after_reset(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats())
        eng.reset_symbol("TXFD6")
        assert eng.has_symbol("TXFD6") is False


# ---------------------------------------------------------------------------
# Multi-symbol tracking
# ---------------------------------------------------------------------------


class TestMultiSymbol:
    def test_independent_symbol_states(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="A", bid=100_0000, ask=101_0000))
        eng.process_lob_stats(_stats(symbol="B", bid=200_0000, ask=201_0000))
        assert eng.get_feature("A", "best_bid") == 100_0000
        assert eng.get_feature("B", "best_bid") == 200_0000

    def test_reset_one_does_not_affect_other(self) -> None:
        eng = FeatureEngine()
        eng.process_lob_stats(_stats(symbol="A"))
        eng.process_lob_stats(_stats(symbol="B"))
        eng.reset_symbol("A")
        assert not eng.has_symbol("A")
        assert eng.has_symbol("B")


# ---------------------------------------------------------------------------
# FeatureEngine quality flags — manual injection
# ---------------------------------------------------------------------------


class TestQualityFlagsManual:
    def test_quality_flags_next_injected(self) -> None:
        eng = FeatureEngine()
        eng._quality_flags_next["TXFD6"] = QUALITY_FLAG_GAP | QUALITY_FLAG_STALE_INPUT
        evt = eng.process_lob_stats(_stats(ts=1))
        assert evt is not None
        assert evt.quality_flags & QUALITY_FLAG_GAP
        assert evt.quality_flags & QUALITY_FLAG_STALE_INPUT
        # Consumed after one tick (ts must be >= previous to avoid OUT_OF_ORDER)
        evt2 = eng.process_lob_stats(_stats(ts=2))
        assert evt2 is not None
        assert evt2.quality_flags == 0


# ---------------------------------------------------------------------------
# FeatureRegistry
# ---------------------------------------------------------------------------


class TestFeatureRegistry:
    def test_register_and_get(self) -> None:
        reg = FeatureRegistry()
        fs = FeatureSet(
            feature_set_id="test_v1",
            schema_version=1,
            features=(FeatureSpec("f1", "i64"),),
        )
        reg.register(fs, make_default=True)
        assert reg.get("test_v1") is fs
        assert reg.get_default() is fs

    def test_get_unknown_raises(self) -> None:
        reg = FeatureRegistry()
        with pytest.raises(KeyError, match="Unknown feature_set_id"):
            reg.get("nonexistent")

    def test_get_default_empty_raises(self) -> None:
        reg = FeatureRegistry()
        with pytest.raises(RuntimeError, match="no registered"):
            reg.get_default()

    def test_set_default(self) -> None:
        reg = FeatureRegistry()
        fs1 = FeatureSet("a", 1, (FeatureSpec("f1", "i64"),))
        fs2 = FeatureSet("b", 1, (FeatureSpec("f2", "i64"),))
        reg.register(fs1)
        reg.register(fs2)
        reg.set_default("b")
        assert reg.get_default() is fs2

    def test_set_default_unknown_raises(self) -> None:
        reg = FeatureRegistry()
        fs = FeatureSet("a", 1, (FeatureSpec("f1", "i64"),))
        reg.register(fs)
        with pytest.raises(KeyError):
            reg.set_default("nonexistent")

    def test_ids_sorted(self) -> None:
        reg = FeatureRegistry()
        reg.register(FeatureSet("z_set", 1, ()))
        reg.register(FeatureSet("a_set", 1, ()))
        assert reg.ids() == ("a_set", "z_set")

    def test_to_dict_structure(self) -> None:
        reg = default_feature_registry()
        d = reg.to_dict()
        assert d["default"] == "lob_shared_v3"
        assert "lob_shared_v1" in d["feature_sets"]
        assert "lob_shared_v2" in d["feature_sets"]
        assert "lob_shared_v3" in d["feature_sets"]
        fs_v2 = d["feature_sets"]["lob_shared_v2"]
        assert fs_v2["schema_version"] == 2
        assert len(fs_v2["features"]) == 22
        fs_v3 = d["feature_sets"]["lob_shared_v3"]
        assert fs_v3["schema_version"] == 3
        assert len(fs_v3["features"]) == 27

    def test_from_sets_factory(self) -> None:
        fs1 = FeatureSet("a", 1, ())
        fs2 = FeatureSet("b", 2, ())
        reg = FeatureRegistry.from_sets([fs1, fs2], default_id="b")
        assert reg.get_default() is fs2

    def test_from_sets_auto_default(self) -> None:
        fs1 = FeatureSet("x", 1, ())
        reg = FeatureRegistry.from_sets([fs1])
        assert reg.get_default() is fs1


# ---------------------------------------------------------------------------
# FeatureSet properties
# ---------------------------------------------------------------------------


class TestFeatureSet:
    def test_feature_ids_order(self) -> None:
        fs = build_default_lob_feature_set_v1()
        ids = fs.feature_ids
        assert ids[0] == "best_bid"
        assert ids[1] == "best_ask"
        assert len(ids) == 16

    def test_index_by_id_mapping(self) -> None:
        fs = build_default_lob_feature_set_v1()
        idx_map = fs.index_by_id
        assert idx_map["best_bid"] == 0
        assert idx_map["ofi_l1_raw"] == 11
