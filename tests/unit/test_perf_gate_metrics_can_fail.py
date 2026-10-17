"""Three of the perf-regression gate's baseline entries could not fail, so the gate reported green over them.

* ``strategy_noop_metrics_on_us_per_event`` replayed an event stamped 1 ns after the epoch, so the runner's staleness
  guard dropped every event and the gate timed that early return (~1.5 us), not strategy dispatch (~5 us).
* ``feature_engine_parity_mismatch_rate`` compared the 16 values of a hand-written reference with the 27 the engine
  now emits: every event "mismatched" (rate 1.0), and its ceiling was 1.0, so total disagreement passed.
* The two Rust drills returned 0.0 when the Rust kernel was not loaded, which is under every ceiling, and compared
  Python's 27 values with Rust's 16 computed plus 11 zero-padded.

These tests load the gate script and pin that each metric measures what it names, refuses to report when it cannot,
and fails when degraded.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.core import timebase

ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = ROOT / "tests" / "benchmark" / "perf_regression_gate.py"
BASELINES = ROOT / "tests" / "benchmark" / "perf_baselines.json"
TEN_HOURS_NS = 10 * 3600 * 1_000_000_000


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("perf_regression_gate_under_test", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _runner_clock_ahead(offset_ns: int):
    """Make ``StrategyRunner`` see a clock ``offset_ns`` ahead of the one the bench stamps its event with."""
    import hft_platform.strategy.runner as runner_module

    fake = MagicMock(wraps=timebase)
    fake.now_ns.side_effect = lambda: timebase.now_ns() + offset_ns
    return patch.object(runner_module, "timebase", fake)


# --- strategy_noop measures dispatch --------------------------------------------------------------------------------


def test_strategy_noop_hands_every_event_to_the_strategy(gate) -> None:
    us_per_event, dispatched, stale_skips = gate._strategy_noop_run(300)

    assert (dispatched, stale_skips) == (300, 0)
    assert us_per_event > 0


def test_strategy_noop_still_dispatches_when_the_run_outlasts_the_default_stale_threshold(gate) -> None:
    """The runner drops events older than 500 ms by default; a long run on a slow runner must not slide into that."""
    with _runner_clock_ahead(2_000_000_000):  # two seconds later than the event's stamp
        _, dispatched, stale_skips = gate._strategy_noop_run(200)

    assert (dispatched, stale_skips) == (200, 0)


def test_strategy_noop_refuses_to_report_a_run_whose_events_were_dropped_as_stale(gate) -> None:
    with _runner_clock_ahead(TEN_HOURS_NS):
        with pytest.raises(RuntimeError, match=r"0/200 events reached the strategy, 200 dropped as stale"):
            gate.bench_strategy_noop(200)


def test_strategy_noop_refuses_to_report_a_run_where_only_some_events_reached_the_strategy(gate) -> None:
    """Not every missing dispatch is a stale drop (a symbol filter, a disabled strategy): half is not a measurement."""
    calls = {"n": 0}
    real = gate._NoopStrategy.on_tick

    def every_other_event(self, event):
        calls["n"] += 1
        if calls["n"] % 2:
            real(self, event)

    with patch.object(gate._NoopStrategy, "on_tick", every_other_event):
        with pytest.raises(RuntimeError, match=r"100/200 events reached the strategy, 0 dropped as stale"):
            gate.bench_strategy_noop(200)


def test_strategy_noop_reports_microseconds_per_event(gate) -> None:
    """The ceiling is in microseconds per event: 0.5 s over 100 events is 5000 us, whatever the machine."""
    clock = iter([0.0, 0.5])
    with patch.object(gate, "time", SimpleNamespace(perf_counter=lambda: next(clock))):
        us_per_event, dispatched, _ = gate._strategy_noop_run(100)

    assert dispatched == 100
    assert us_per_event == pytest.approx(5000.0)


# --- the parity reference is compared by feature name -----------------------------------------------------------------


def test_feature_parity_finds_no_mismatch_in_the_features_the_reference_covers(gate) -> None:
    assert gate.bench_feature_engine_parity_mismatch_rate(400) == 0.0


def test_feature_parity_counts_a_reference_value_that_is_wrong(gate) -> None:
    real = gate._feature_ref_values

    def wrong(st, evt, stats):
        values = list(real(st, evt, stats))
        values[-1] += 1
        return tuple(values)

    with patch.object(gate, "_feature_ref_values", wrong):
        assert gate.bench_feature_engine_parity_mismatch_rate(50) == 1.0


def test_feature_parity_counts_an_event_the_engine_produced_no_update_for(gate) -> None:
    class _SilentEngine:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def process_lob_update(self, evt, stats, local_ts_ns: int):
            return None

    with patch.object(gate, "FeatureEngine", _SilentEngine):
        assert gate.bench_feature_engine_parity_mismatch_rate(20) == 1.0


def test_feature_values_by_id_ignores_extra_features_and_reports_a_missing_one(gate) -> None:
    ids = gate._FEATURE_REF_IDS
    extra = SimpleNamespace(feature_ids=(*ids, "new_feature"), values=(*range(len(ids)), 999))
    missing = SimpleNamespace(feature_ids=ids[:-1], values=tuple(range(len(ids) - 1)))

    assert gate._feature_values_by_id(extra) == tuple(range(len(ids)))
    assert gate._feature_values_by_id(missing) is None
    assert gate._feature_values_by_id(None) is None


def test_feature_values_by_id_follows_the_names_not_the_positions(gate) -> None:
    ids = gate._FEATURE_REF_IDS
    shuffled = SimpleNamespace(feature_ids=tuple(reversed(ids)), values=tuple(range(len(ids))))

    assert gate._feature_values_by_id(shuffled) == tuple(reversed(range(len(ids))))


def test_no_rate_ceiling_allows_total_mismatch() -> None:
    baselines = json.loads(BASELINES.read_text(encoding="utf-8"))
    rates = {key: value for key, value in baselines.items() if "mismatch_rate" in key}

    assert rates, "the baselines no longer carry a mismatch rate to check"
    for key, ceiling in rates.items():
        assert ceiling < 1.0, f"{key}={ceiling} permits every event to disagree"


def test_parity_mismatch_ceilings_are_zero() -> None:
    """Both parity drills replay a seeded sequence, so a disagreement with the reference is a bug, not noise."""
    baselines = json.loads(BASELINES.read_text(encoding="utf-8"))
    parity = {key: value for key, value in baselines.items() if key.endswith("parity_mismatch_rate_max")}

    assert sorted(parity) == [
        "feature_engine_parity_mismatch_rate_max",
        "feature_engine_python_vs_rust_parity_mismatch_rate_max",
    ]
    assert set(parity.values()) == {0.0}


# --- the Rust drills refuse to run without Rust, and compare what both kernels compute ---------------------------------


class _PythonOnlyEngine:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def kernel_backend(self) -> str:
        return "python"


@pytest.mark.parametrize(
    "name", ["bench_feature_engine_lob_update_rust", "bench_feature_engine_python_vs_rust_parity_mismatch_rate"]
)
def test_rust_drills_refuse_to_report_when_the_rust_kernel_is_not_loaded(gate, name: str) -> None:
    with patch.object(gate, "FeatureEngine", _PythonOnlyEngine):
        with pytest.raises(RuntimeError, match="Rust feature kernel is not loaded"):
            getattr(gate, name)(10)


def test_rust_latency_drill_builds_a_rust_engine(gate) -> None:
    built: list[str] = []

    class _RustEngine:
        def __init__(self, *args, kernel_backend: str = "python", **kwargs) -> None:
            built.append(kernel_backend)

        def kernel_backend(self) -> str:
            return "rust"

        def process_lob_update(self, evt, stats, local_ts_ns: int):
            return None

    with patch.object(gate, "FeatureEngine", _RustEngine):
        assert gate.bench_feature_engine_lob_update_rust(50) >= 0.0

    assert built == ["rust"]


def _two_kernel_engines(gate, rust_first_value: int = 0):
    """Fake engines: Python emits 27 values, Rust the same first 16 and 11 zero-padded ones (as the real kernel does)."""
    ids = (*gate._FEATURE_REF_IDS, *(f"extra_{i}" for i in range(11)))
    shared = tuple(range(1, len(gate._FEATURE_REF_IDS) + 1))

    class _Engine:
        def __init__(self, *args, kernel_backend: str = "python", **kwargs) -> None:
            self._backend = kernel_backend

        def kernel_backend(self) -> str:
            return self._backend

        def process_lob_update(self, evt, stats, local_ts_ns: int):
            if self._backend == "rust":
                first = rust_first_value or shared[0]
                values = (first, *shared[1:], *([0] * 11))
            else:
                values = (*shared, *range(100, 111))
            return SimpleNamespace(feature_ids=ids, values=values)

    return _Engine


def test_python_vs_rust_parity_compares_only_the_features_both_kernels_compute(gate) -> None:
    with patch.object(gate, "FeatureEngine", _two_kernel_engines(gate)):
        assert gate.bench_feature_engine_python_vs_rust_parity_mismatch_rate(20) == 0.0


def test_python_vs_rust_parity_counts_a_difference_in_a_shared_feature(gate) -> None:
    with patch.object(gate, "FeatureEngine", _two_kernel_engines(gate, rust_first_value=99)):
        assert gate.bench_feature_engine_python_vs_rust_parity_mismatch_rate(20) == 1.0


def test_python_vs_rust_parity_counts_an_event_neither_kernel_produced(gate) -> None:
    """Two missing updates are not two equal ones: an event nobody computed is not agreement."""
    engines = _two_kernel_engines(gate)
    engines.process_lob_update = lambda self, evt, stats, local_ts_ns: None

    with patch.object(gate, "FeatureEngine", engines):
        assert gate.bench_feature_engine_python_vs_rust_parity_mismatch_rate(20) == 1.0


# --- the gate fails when a metric degrades ----------------------------------------------------------------------------


def _run_gate_payload(gate, tmp_path: Path, overrides: dict[str, Any], *flags: str) -> dict:
    """Run ``main()`` with every benchmark stubbed to 0.0 except ``overrides`` (a number, or a callable to run)."""
    out = tmp_path / "gate.json"
    stubs = {name: (lambda *a, **k: 0.0) for name in dir(gate) if name.startswith("bench_")}
    stubs.update(
        {name: (value if callable(value) else (lambda *a, _v=value, **k: _v)) for name, value in overrides.items()}
    )
    argv = ["perf_regression_gate", "--runs", "1", "--baseline", str(BASELINES), "--json", str(out), *flags]
    with patch.multiple(gate, **stubs), patch.object(sys, "argv", argv), patch("builtins.print"):
        code = gate.main()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert (code != 0) == bool(payload["failures"])
    return payload


def _run_gate(gate, tmp_path: Path, overrides: dict[str, Any], *flags: str) -> list[str]:
    return _run_gate_payload(gate, tmp_path, overrides, *flags)["failures"]


def test_gate_passes_when_every_metric_is_within_its_ceiling(gate, tmp_path: Path) -> None:
    assert _run_gate(gate, tmp_path, {}, "--include-feature-rust") == []


def test_every_metric_the_gate_reports_has_a_ceiling_to_fail_against(gate, tmp_path: Path) -> None:
    """The gate skips a metric with no ``_max`` entry, so a missing ceiling is a metric that cannot fail."""
    payload = _run_gate_payload(
        gate, tmp_path, {}, "--include-feature-rust", "--include-recorder-io", "--include-risk-heavy"
    )
    baselines = json.loads(BASELINES.read_text(encoding="utf-8"))

    assert payload["results"], "the gate reported no metrics"
    assert sorted(key for key in payload["results"] if f"{key}_max" not in baselines) == []


_ALL_FLAGS = ("--include-feature-rust", "--include-recorder-io", "--include-risk-heavy")


def test_every_metric_the_gate_reports_fails_when_it_degrades_far_past_its_ceiling(gate, tmp_path: Path) -> None:
    """A ceiling in the baselines is not enough: the metric also has to be compared against it in ``main()``."""
    huge = {name: (lambda *a, **k: 1e9) for name in dir(gate) if name.startswith("bench_")}
    payload = _run_gate_payload(gate, tmp_path, huge, *_ALL_FLAGS)
    failed = {failure.split("=", 1)[0] for failure in payload["failures"]}

    assert payload["results"], "the gate reported no metrics"
    assert sorted(set(payload["results"]) - failed) == []


def test_every_ceiling_in_the_baselines_belongs_to_a_metric_the_gate_reports(gate, tmp_path: Path) -> None:
    """The other direction: a ceiling whose metric is no longer reported is dead, and looks like protection."""
    payload = _run_gate_payload(gate, tmp_path, {}, *_ALL_FLAGS)
    baselines = json.loads(BASELINES.read_text(encoding="utf-8"))
    ceilings = {key[: -len("_max")] for key in baselines if key.endswith("_max")}

    assert sorted(ceilings - set(payload["results"])) == []


def test_gate_fails_when_the_parity_mismatch_rate_degrades(gate, tmp_path: Path) -> None:
    failures = _run_gate(gate, tmp_path, {"bench_feature_engine_parity_mismatch_rate": 0.02})

    assert any(f.startswith("feature_engine_parity_mismatch_rate=") for f in failures), failures


def test_gate_fails_when_strategy_dispatch_gets_slower_than_its_ceiling(gate, tmp_path: Path) -> None:
    failures = _run_gate(gate, tmp_path, {"bench_strategy_noop": 20.0})

    assert any(f.startswith("strategy_noop_metrics_on_us_per_event=") for f in failures), failures


def test_gate_does_not_swallow_a_benchmark_that_refuses_to_report(gate, tmp_path: Path) -> None:
    def refuse(*args, **kwargs):
        raise RuntimeError("strategy_noop did not measure dispatch")

    with pytest.raises(RuntimeError, match="did not measure dispatch"):
        _run_gate(gate, tmp_path, {"bench_strategy_noop": refuse})


def test_gate_fails_when_the_rust_drills_degrade(gate, tmp_path: Path) -> None:
    failures = _run_gate(
        gate,
        tmp_path,
        {
            "bench_feature_engine_lob_update_rust": 25.0,
            "bench_feature_engine_python_vs_rust_parity_mismatch_rate": 0.001,
        },
        "--include-feature-rust",
    )

    assert any(f.startswith("feature_engine_lob_update_rust_us_per_event=") for f in failures), failures
    assert any(f.startswith("feature_engine_python_vs_rust_parity_mismatch_rate=") for f in failures), failures
