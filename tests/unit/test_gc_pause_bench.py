from __future__ import annotations

import gc
import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "latency" / "gc_pause_bench.py"
    spec = importlib.util.spec_from_file_location("gc_pause_bench", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


@pytest.fixture(autouse=True)
def isolated_gc_state():
    was_enabled = gc.isenabled()
    threshold = gc.get_threshold()
    gc.enable()
    yield
    gc.set_threshold(*threshold)
    if was_enabled:
        gc.enable()
    else:
        gc.disable()


REQUIRED_KEYS = {
    "python_version",
    "gil_enabled",
    "mode",
    "pause_count",
    "pause_p50_ns",
    "pause_p99_ns",
    "pause_max_ns",
    "ticker_max_gap_ns",
    "gate",
    "full_collect_probe_ns",
    "post_run_full_collect_ns",
    "heap",
    "workload",
}


def _tiny_args(*extra: str):
    return MODULE.build_parser().parse_args(
        [
            "--contracts",
            "50",
            "--symbols",
            "5",
            "--ring-size",
            "64",
            "--retain-size",
            "32",
            "--warmup-ticks",
            "200",
            "--probe-collections",
            "1",
            "--seconds",
            "0.02",
            "--tick-rate",
            "0",
            *extra,
        ]
    )


def test_parser_defaults_match_preregistered_limits():
    args = MODULE.build_parser().parse_args([])
    assert args.mode == "default"
    assert args.seconds == 20.0
    assert args.contracts == 54_000
    assert args.symbols == 2_900
    assert args.p99_limit_ns == 1_000_000
    assert args.max_limit_ns == 5_000_000


def test_parser_accepts_every_arm():
    parsed = [MODULE.build_parser().parse_args(["--mode", mode]).mode for mode in MODULE.MODES]
    assert parsed == ["default", "freeze", "threshold", "disabled"]


def test_parser_rejects_unknown_mode():
    with pytest.raises(SystemExit) as exc:
        MODULE.build_parser().parse_args(["--mode", "bogus"])
    assert exc.value.code == 2


@pytest.mark.parametrize("flag", ["--seconds", "--symbols", "--depth", "--cycle-every"])
def test_parser_rejects_non_positive_values(flag):
    with pytest.raises(SystemExit) as exc:
        MODULE.build_parser().parse_args([flag, "0"])
    assert exc.value.code == 2


def test_percentile_uses_nearest_rank():
    values = list(range(1, 101))
    assert MODULE.percentile_ns(values, 50) == 50
    assert MODULE.percentile_ns(values, 99) == 99
    assert MODULE.percentile_ns(values, 100) == 100
    assert MODULE.percentile_ns([], 99) == 0


def test_percentile_rounds_the_rank_up_when_the_list_is_short():
    assert MODULE.percentile_ns(list(range(1, 11)), 99) == 10
    assert MODULE.percentile_ns([1, 2, 3], 50) == 2
    assert MODULE.percentile_ns([7], 99) == 7


def test_gate_passes_when_pauses_within_limits():
    gate = MODULE.evaluate_gate([100_000, 900_000, 4_000_000], 5_000_000, 5_000_000)
    assert gate["passed"] is True
    assert gate["p99_ok"] is True
    assert gate["max_ok"] is True


def test_gate_fails_when_max_exceeds_limit():
    gate = MODULE.evaluate_gate([100_000] * 99 + [5_000_001], 1_000_000, 5_000_000)
    assert gate["p99_ok"] is True
    assert gate["max_ok"] is False
    assert gate["passed"] is False


def test_gate_fails_when_p99_exceeds_limit():
    gate = MODULE.evaluate_gate([2_000_000] * 10, 1_000_000, 5_000_000)
    assert gate["p99_ok"] is False
    assert gate["passed"] is False


def test_gate_passes_with_no_pauses():
    gate = MODULE.evaluate_gate([], 1_000_000, 5_000_000)
    assert gate["passed"] is True


def test_pause_recorder_ignores_collections_while_inactive():
    recorder = MODULE.PauseRecorder()
    recorder("start", {"generation": 2})
    recorder("stop", {"generation": 2})
    assert recorder.pauses_ns == []


def test_pause_recorder_records_generation_and_duration_while_active():
    recorder = MODULE.PauseRecorder()
    recorder.active = True
    recorder("start", {"generation": 1})
    recorder("stop", {"generation": 1})
    assert len(recorder.pauses_ns) == 1
    assert recorder.pauses_ns[0] >= 0
    assert recorder.generations == [1]


@pytest.mark.parametrize("mode", MODULE.MODES)
def test_run_bench_report_has_json_shape_in_every_arm(mode):
    report = MODULE.run_bench(_tiny_args("--mode", mode))
    assert REQUIRED_KEYS <= set(report)
    assert report["mode"] == mode
    assert report["pause_count"] >= 0
    assert report["ticks_processed"] > 0
    assert report["gate"]["passed"] in (True, False)
    assert json.loads(json.dumps(report)) == report


def _baseline_frozen_objects() -> int:
    # CPython 3.12 moves its immortal objects into the permanent generation on every full
    # collection, so the baseline is only stable right after one.
    gc.collect()
    return gc.get_freeze_count()


@pytest.mark.parametrize("mode", MODULE.MODES)
def test_run_bench_restores_gc_enabled_threshold_and_callbacks_after_every_arm(mode):
    threshold_before = gc.get_threshold()
    callbacks_before = list(gc.callbacks)
    MODULE.run_bench(_tiny_args("--mode", mode))
    assert gc.isenabled() is True
    assert gc.get_threshold() == threshold_before
    assert gc.callbacks == callbacks_before


def test_disabled_arm_sees_no_collection_in_window():
    report = MODULE.run_bench(_tiny_args("--mode", "disabled"))
    assert report["gc"]["enabled"] is False
    assert report["pause_count"] == 0
    assert report["pause_max_ns"] == 0
    assert report["gate"]["passed"] is True


def test_freeze_arm_freezes_the_heap_and_unfreezes_it_afterwards():
    baseline = _baseline_frozen_objects()
    report = MODULE.run_bench(_tiny_args("--mode", "freeze"))
    assert report["gc"]["frozen_objects"] > baseline
    assert gc.get_freeze_count() < report["gc"]["frozen_objects"]


@pytest.mark.parametrize("mode", ["default", "threshold", "disabled"])
def test_other_arms_do_not_freeze_anything(mode):
    baseline = _baseline_frozen_objects()
    report = MODULE.run_bench(_tiny_args("--mode", mode))
    assert report["gc"]["frozen_objects"] == baseline


def test_threshold_arm_raises_threshold0():
    report = MODULE.run_bench(_tiny_args("--mode", "threshold", "--threshold", "123456"))
    assert report["gc"]["threshold"][0] == 123456


def test_collection_inside_window_is_counted_and_timed(monkeypatch):
    real_run_window = MODULE.run_window

    def window_with_collection(heap, start, seconds, tick_rate):
        gc.collect()
        return real_run_window(heap, start, seconds, tick_rate)

    monkeypatch.setattr(MODULE, "run_window", window_with_collection)
    report = MODULE.run_bench(_tiny_args("--mode", "default"))
    assert report["pause_count"] >= 1
    assert report["pause_max_ns"] > 0
    assert report["pause_p99_ns"] <= report["pause_max_ns"]
    assert report["pause_p50_ns"] <= report["pause_p99_ns"]


def test_main_prints_single_json_line_and_returns_zero_when_gate_passes(capsys):
    code = MODULE.main(
        [
            "--mode",
            "disabled",
            "--contracts",
            "10",
            "--symbols",
            "3",
            "--ring-size",
            "16",
            "--retain-size",
            "8",
            "--warmup-ticks",
            "50",
            "--probe-collections",
            "0",
            "--seconds",
            "0.02",
            "--tick-rate",
            "0",
        ]
    )
    out_lines = capsys.readouterr().out.strip().splitlines()
    assert code == 0
    assert len(out_lines) == 1
    assert json.loads(out_lines[0])["mode"] == "disabled"


def test_main_returns_one_when_gate_fails(monkeypatch, capsys):
    monkeypatch.setattr(MODULE, "run_bench", lambda args: {"gate": {"passed": False}})
    assert MODULE.main([]) == 1
    assert json.loads(capsys.readouterr().out)["gate"]["passed"] is False
