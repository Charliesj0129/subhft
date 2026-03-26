"""Integration tests: FeatureEngine CI parity — schema + backend value agreement.

Unit 9: Feature Engine CI Parity Test + Shadow Metrics.

Tests:
1. Schema parity — Python FeatureRegistry vs Rust registry (if available).
2. Backend parity on synthetic data — Python vs Rust FeatureEngine produce identical outputs.
3. Mismatch detection — ParityReport correctly identifies deliberate discrepancies.
"""

from __future__ import annotations

import importlib
import random

import pytest

from hft_platform.events import LOBStatsEvent
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feature.parity import (
    ParityMismatch,
    ParityReport,
    check_backend_parity,
    check_schema_parity,
)
from hft_platform.feature.registry import default_feature_registry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SYMBOL = "TEST.SIM"
_BASE_PRICE = 180_000_000  # 18000.0000 in x10000 scaled units (e.g. TAIEX futures)
_TICK = 10_000  # 1 point in x10000 units


def _run_engine_on_events(
    events: list[LOBStatsEvent],
    backend: str = "python",
    symbol: str = _SYMBOL,
    feature_set_id: str | None = None,
) -> dict[str, float]:
    """Run a FeatureEngine on events and return the last feature dict for *symbol*.

    *feature_set_id* can be used to override the default feature set (e.g. to test
    Python vs Rust parity on the v1 feature set that the Rust kernel supports).
    """
    registry = default_feature_registry()
    engine = FeatureEngine(
        registry=registry,
        feature_set_id=feature_set_id,
        kernel_backend=backend,
        emit_events=False,
    )
    for ev in events:
        engine.process_lob_stats(ev)
    tup = engine.get_feature_tuple(symbol)
    if tup is None:
        return {}
    return dict(zip(engine.feature_ids(), tup))


def _rust_available() -> bool:
    """Return True if the Rust extension module is compiled and loadable."""
    try:
        try:
            mod = importlib.import_module("hft_platform.rust_core")
        except Exception:
            mod = importlib.import_module("rust_core")
        return getattr(mod, "LobFeatureKernelV1", None) is not None
    except Exception:
        return False


def _make_lob_stats_event(
    ts: int,
    best_bid: int,
    best_ask: int,
    bid_depth: int,
    ask_depth: int,
    imbalance: float | None = None,
    symbol: str = _SYMBOL,
) -> LOBStatsEvent:
    """Construct a realistic LOBStatsEvent from integer scaled prices."""
    if imbalance is None:
        total = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / float(total) if total > 0 else 0.0

    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=imbalance,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
    )


def _make_synthetic_events(n: int = 100, seed: int = 42) -> list[LOBStatsEvent]:
    """Generate *n* synthetic LOBStatsEvents with realistic spread/imbalance ranges."""
    rng = random.Random(seed)
    events: list[LOBStatsEvent] = []

    mid = _BASE_PRICE
    ts = 1_700_000_000_000_000_000  # arbitrary epoch ns

    for i in range(n):
        # Simulate a random walk on mid price with ±1–3 ticks per step.
        mid += rng.randint(-3, 3) * _TICK
        spread_ticks = rng.randint(1, 4)
        half_spread = (spread_ticks * _TICK) // 2
        best_bid = mid - half_spread
        best_ask = mid + half_spread

        bid_depth = rng.randint(50, 2000)
        ask_depth = rng.randint(50, 2000)

        ts += rng.randint(1_000_000, 100_000_000)  # 1 ms – 100 ms increments

        events.append(
            _make_lob_stats_event(
                ts=ts,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
            )
        )

    return events


# ---------------------------------------------------------------------------
# Schema parity tests
# ---------------------------------------------------------------------------


class TestSchemaParity:
    """Validate registry schema metadata and the real runtime backend contract."""

    def test_runtime_status_reports_default_registry_schema_metadata(self) -> None:
        """FeatureEngine runtime status must expose the default registry schema metadata."""
        registry = default_feature_registry()
        feature_set = registry.get_default()
        engine = FeatureEngine(registry=registry, kernel_backend="python", emit_events=False)
        status = engine.runtime_status()

        assert status["feature_set_id"] == feature_set.feature_set_id
        assert status["schema_version"] == feature_set.schema_version
        assert status["kernel_backend"] == "python"
        assert status["rust_backend_available"] is _rust_available()
        assert status["tracked_symbols"] == 0

    def test_check_schema_parity_reports_empty_skip_without_rust(self) -> None:
        """When Rust is unavailable, schema parity reports an honest empty skip result."""
        if _rust_available():
            pytest.skip("Rust backend is available; this test targets unavailable-Rust path")
        rust_requested_engine = FeatureEngine(
            registry=default_feature_registry(),
            kernel_backend="rust",
            emit_events=False,
        )
        report = check_schema_parity({}, {})
        assert rust_requested_engine.kernel_backend() == "python"
        assert rust_requested_engine.runtime_status()["rust_backend_available"] is False
        assert report == ParityReport(total_events=0, mismatches=(), passed=True)

    def test_rust_requested_runtime_status_uses_actual_backend(self) -> None:
        """A rust-requested engine must report the real runtime backend and schema metadata."""
        registry = default_feature_registry()
        feature_set = registry.get_default()
        engine = FeatureEngine(registry=registry, kernel_backend="rust", emit_events=False)
        status = engine.runtime_status()

        assert status["feature_set_id"] == feature_set.feature_set_id
        assert status["schema_version"] == feature_set.schema_version
        assert status["rust_backend_available"] is _rust_available()
        assert status["kernel_backend"] == ("rust" if _rust_available() else "python")

    def test_python_registry_has_expected_feature_ids(self) -> None:
        """Smoke-test: Python default registry has the expected 19 canonical feature IDs (v2)."""
        registry = default_feature_registry()
        fs = registry.get_default()
        feature_ids = fs.feature_ids

        expected = {
            "best_bid",
            "best_ask",
            "mid_price_x2",
            "spread_scaled",
            "bid_depth",
            "ask_depth",
            "depth_imbalance_ppm",
            "microprice_x2",
            "l1_bid_qty",
            "l1_ask_qty",
            "l1_imbalance_ppm",
            "ofi_l1_raw",
            "ofi_l1_cum",
            "ofi_l1_ema8",
            "spread_ema8_scaled",
            "depth_imbalance_ema8_ppm",
            # v2 additions
            "ofi_depth_norm_ppm",
            "ret_autocov_5s_x1e6",
            "tob_survival_ms",
        }

        assert set(feature_ids) == expected
        assert len(feature_ids) == 19

    def test_python_registry_warmup_semantics(self) -> None:
        """Rolling features (OFI, EMA) require warmup_min_events >= 2."""
        registry = default_feature_registry()
        fs = registry.get_default()
        rolling_features = {
            "ofi_l1_raw",
            "ofi_l1_cum",
            "ofi_l1_ema8",
            "spread_ema8_scaled",
            "depth_imbalance_ema8_ppm",
        }
        for spec in fs.features:
            if spec.feature_id in rolling_features:
                assert spec.warmup_min_events >= 2, (
                    f"Rolling feature {spec.feature_id!r} has warmup_min_events="
                    f"{spec.warmup_min_events} (expected >= 2)"
                )


# ---------------------------------------------------------------------------
# Backend parity tests
# ---------------------------------------------------------------------------


class TestBackendParity:
    """Validate that Python and Rust FeatureEngine produce identical outputs."""

    def test_check_backend_parity_returns_parity_report(self) -> None:
        """check_backend_parity() must always return a ParityReport."""
        events = _make_synthetic_events(5)
        py_features = _run_engine_on_events(events, backend="python")
        mismatched_features = dict(py_features)
        mismatched_features["best_bid"] = float(mismatched_features["best_bid"]) + 1_000.0
        report = check_backend_parity(py_features, mismatched_features)
        assert isinstance(report, ParityReport)
        assert report.passed is False
        assert any(m.feature_id == "best_bid" for m in report.mismatches)

    def test_check_backend_parity_reports_empty_skip_without_rust(self) -> None:
        """When Rust is unavailable, backend parity reports an honest empty skip result."""
        if _rust_available():
            pytest.skip("Rust backend is available; this test targets unavailable-Rust path")
        rust_requested_engine = FeatureEngine(
            registry=default_feature_registry(),
            kernel_backend="rust",
            emit_events=False,
        )
        report = check_backend_parity({}, {})
        assert rust_requested_engine.kernel_backend() == "python"
        assert rust_requested_engine.runtime_status()["rust_backend_available"] is False
        assert report == ParityReport(total_events=0, mismatches=(), passed=True)

    @pytest.mark.skipif(not _rust_available(), reason="Rust backend not compiled")
    def test_check_backend_parity_zero_mismatches_on_synthetic_data(self) -> None:
        """Python and Rust backends must produce identical integer feature vectors on 100 events.

        Uses lob_shared_v1 because LobFeatureKernelV1 (Rust) only implements the v1 feature set.
        """
        events = _make_synthetic_events(100)
        py_features = _run_engine_on_events(events, backend="python", feature_set_id="lob_shared_v1")
        rust_features = _run_engine_on_events(events, backend="rust", feature_set_id="lob_shared_v1")
        report = check_backend_parity(py_features, rust_features)

        assert report.passed, f"Backend parity failed with {len(report.mismatches)} mismatch(es):\n" + "\n".join(
            f"  {m.feature_id!r}: py={m.python_value} rust={m.rust_value}" for m in report.mismatches[:20]
        )
        assert report.mismatches == ()

    @pytest.mark.skipif(not _rust_available(), reason="Rust backend not compiled")
    def test_check_backend_parity_multi_symbol(self) -> None:
        """Parity holds when events span multiple symbols.

        Uses lob_shared_v1 because LobFeatureKernelV1 (Rust) only implements the v1 feature set.
        """
        symbols = ["SYM_A", "SYM_B", "SYM_C"]
        rng = random.Random(99)
        events: list[LOBStatsEvent] = []
        ts = 1_700_000_000_000_000_000
        mid = _BASE_PRICE
        for i in range(60):
            sym = symbols[i % len(symbols)]
            mid += rng.randint(-2, 2) * _TICK
            spread = rng.randint(1, 3) * _TICK
            events.append(
                _make_lob_stats_event(
                    ts=ts + i * 5_000_000,
                    best_bid=mid - spread // 2,
                    best_ask=mid + spread // 2,
                    bid_depth=rng.randint(100, 1000),
                    ask_depth=rng.randint(100, 1000),
                    symbol=sym,
                )
            )

        for symbol in symbols:
            py_features = _run_engine_on_events(events, backend="python", symbol=symbol, feature_set_id="lob_shared_v1")
            rust_features = _run_engine_on_events(events, backend="rust", symbol=symbol, feature_set_id="lob_shared_v1")
            assert py_features, f"Expected Python features for {symbol}"
            assert rust_features, f"Expected Rust features for {symbol}"
            report = check_backend_parity(py_features, rust_features)
            assert report.passed, (
                f"Multi-symbol backend parity failed for {symbol}: {len(report.mismatches)} mismatch(es)"
            )

    @pytest.mark.skipif(not _rust_available(), reason="Rust backend not compiled")
    def test_check_backend_parity_edge_cases(self) -> None:
        """Parity holds for edge-case inputs: zero depths, equal bid/ask, single event.

        Uses lob_shared_v1 because LobFeatureKernelV1 (Rust) only implements the v1 feature set.
        """
        ts = 1_700_000_000_000_000_000
        edge_events = [
            # Equal bid/ask (zero spread)
            _make_lob_stats_event(
                ts=ts,
                best_bid=_BASE_PRICE,
                best_ask=_BASE_PRICE,
                bid_depth=0,
                ask_depth=0,
            ),
            # One-sided liquidity
            _make_lob_stats_event(
                ts=ts + 1_000_000,
                best_bid=_BASE_PRICE - _TICK,
                best_ask=_BASE_PRICE + _TICK,
                bid_depth=5000,
                ask_depth=0,
            ),
            # Very wide spread
            _make_lob_stats_event(
                ts=ts + 2_000_000,
                best_bid=_BASE_PRICE - 100 * _TICK,
                best_ask=_BASE_PRICE + 100 * _TICK,
                bid_depth=1,
                ask_depth=1,
            ),
        ]
        py_features = _run_engine_on_events(edge_events, backend="python", feature_set_id="lob_shared_v1")
        rust_features = _run_engine_on_events(edge_events, backend="rust", feature_set_id="lob_shared_v1")
        report = check_backend_parity(py_features, rust_features)
        assert report.passed, f"Edge-case backend parity failed: {len(report.mismatches)} mismatch(es)\n" + "\n".join(
            str(m) for m in report.mismatches
        )


# ---------------------------------------------------------------------------
# Mismatch detection test
# ---------------------------------------------------------------------------


class TestMismatchDetection:
    """Validate that ParityReport correctly identifies deliberate discrepancies."""

    def test_parity_report_correctly_identifies_mismatches(self) -> None:
        """A ParityReport built with known mismatches reports passed=False."""
        mismatches = (
            ParityMismatch(
                event_idx=3,
                feature_id="spread_scaled",
                python_value=40000.0,
                rust_value=50000.0,
            ),
            ParityMismatch(
                event_idx=7,
                feature_id="ofi_l1_raw",
                python_value=-200.0,
                rust_value=0.0,
            ),
        )
        report = ParityReport(
            total_events=100,
            mismatches=mismatches,
            passed=False,
        )
        assert report.passed is False
        assert len(report.mismatches) == 2
        assert report.mismatches[0].feature_id == "spread_scaled"
        assert report.mismatches[1].feature_id == "ofi_l1_raw"
        assert report.mismatches[0].python_value == pytest.approx(40000.0)
        assert report.mismatches[0].rust_value == pytest.approx(50000.0)

    def test_parity_report_is_frozen(self) -> None:
        """ParityReport (frozen=True) must be immutable."""
        report = ParityReport(total_events=10, mismatches=(), passed=True)
        with pytest.raises((AttributeError, TypeError)):
            report.passed = False  # type: ignore[misc]

    def test_parity_mismatch_is_frozen(self) -> None:
        """ParityMismatch (frozen=True) must be immutable."""
        m = ParityMismatch(event_idx=0, feature_id="best_bid", python_value=1.0, rust_value=2.0)
        with pytest.raises((AttributeError, TypeError)):
            m.python_value = 99.0  # type: ignore[misc]

    def test_empty_event_list_produces_no_mismatches(self) -> None:
        """Empty input produces a trivially passing report regardless of backend."""
        report = check_backend_parity({}, {})
        # Empty feature dicts: no comparisons, no mismatches.
        assert report.passed is True
        assert report.mismatches == ()

    def test_parity_report_with_no_mismatches_passes(self) -> None:
        """A ParityReport with an empty mismatches tuple and passed=True is consistent."""
        report = ParityReport(total_events=50, mismatches=(), passed=True)
        assert report.passed is True
        assert len(report.mismatches) == 0

    @pytest.mark.skipif(not _rust_available(), reason="Rust backend not compiled")
    def test_requested_runtime_backend_matches_python_reference(self) -> None:
        """A rust-requested engine must use the real runtime backend and stay in parity.

        Uses lob_shared_v1 because LobFeatureKernelV1 (Rust) only implements the v1 feature set.
        The v2 features (ofi_depth_norm_ppm, ret_autocov_5s_x1e6, tob_survival_ms) are Python-only
        until a Rust v2 kernel is implemented.
        """
        events = _make_synthetic_events(30)
        registry = default_feature_registry()

        python_engine = FeatureEngine(
            registry=registry, feature_set_id="lob_shared_v1", kernel_backend="python", emit_events=False
        )
        runtime_engine = FeatureEngine(
            registry=registry, feature_set_id="lob_shared_v1", kernel_backend="rust", emit_events=False
        )

        for event in events:
            python_engine.process_lob_stats(event)
            runtime_engine.process_lob_stats(event)

        python_features = python_engine.get_feature_tuple(_SYMBOL)
        runtime_features = runtime_engine.get_feature_tuple(_SYMBOL)

        assert python_features is not None
        assert runtime_features is not None
        assert runtime_engine.kernel_backend() == "rust"
        report = check_backend_parity(
            dict(zip(python_engine.feature_ids(), python_features)),
            dict(zip(runtime_engine.feature_ids(), runtime_features)),
        )
        assert report.passed, f"Runtime backend drifted from Python reference: {report.mismatches}"

    def test_synthetic_parity_check_consistent_with_python_only(self) -> None:
        """Python FeatureEngine fed the same events twice must produce identical values."""
        events = _make_synthetic_events(30)
        registry = default_feature_registry()

        engine_a = FeatureEngine(registry=registry, kernel_backend="python", emit_events=False)
        engine_b = FeatureEngine(registry=registry, kernel_backend="python", emit_events=False)

        for event in events:
            engine_a.process_lob_stats(event)
            engine_b.process_lob_stats(event)

        values_a = engine_a.get_feature_tuple(_SYMBOL)
        values_b = engine_b.get_feature_tuple(_SYMBOL)

        assert values_a is not None
        assert values_b is not None
        assert values_a == values_b, "Two identical Python engines diverged — determinism broken"

    def test_parity_schema_report_no_mismatches_for_python_only(self) -> None:
        """When Rust is unavailable, check_schema_parity must have zero mismatches."""
        if _rust_available():
            pytest.skip("Rust backend is available; this test targets unavailable-Rust path")
        report = check_schema_parity({}, {})
        assert report.mismatches == ()
        assert report.passed is True
