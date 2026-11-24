"""scripts/latency/md_ab_bench.py: argument parsing, per-run criteria and the pre-registered P5 gate verdict.

The bench itself needs a live MarketDataService and minutes of wall time; these tests cover the pure parts a wrong
edit would silently turn into a false PASS: the sustained criteria, the capacity and cost halves of the gate, the
INCOMPLETE state, the workload mix and the CLI contract.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "latency" / "md_ab_bench.py"


@pytest.fixture(scope="module")
def bench():
    name = "md_ab_bench_under_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # @dataclass resolves its module through sys.modules
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


def _record(
    arm: str,
    interpreter: str,
    workers: int,
    rate: int,
    repeat: int,
    *,
    sustained: bool = True,
    cpu: float = 100.0,
    digest: str = "d1",
) -> dict[str, Any]:
    return {
        "schema": 1,
        "arm": arm,
        "interpreter": interpreter,
        "workers": workers,
        "rate": rate,
        "repeat": repeat,
        "criteria": {"sustained": sustained, "conserved": True},
        "counts": {"dropped": 0},
        "cpu": {"us_per_event": cpu},
        "committed_ev_s": float(rate),
        "queue_age_us": {"p50": 50.0, "p99": 900.0},
        "loop_probe_lag_us": {"p50": 300.0, "p99": 800.0, "max": 2000.0},
        "gil_enabled": interpreter == "py312",
        "python": "3.12.12" if interpreter == "py312" else "3.14.0",
        "workload_digest": digest,
    }


def _runs(arm: str, interpreter: str, workers: int, rate: int, flags: list[bool], cpu: float = 100.0) -> list[dict]:
    return [_record(arm, interpreter, workers, rate, i + 1, sustained=ok, cpu=cpu) for i, ok in enumerate(flags)]


def _passing_fixture(cost_314: float = 110.0) -> list[dict]:
    records = _runs("py312-n0", "py312", 0, 2000, [True] * 3, cpu=100.0)
    records += _runs("py314t-n0", "py314t", 0, 2000, [True] * 3, cpu=cost_314)
    records += _runs("py314t-n4", "py314t", 4, 11500, [True] * 3, cpu=150.0)
    return records


def _criteria(bench, **overrides: Any) -> dict[str, bool]:
    base: dict[str, Any] = {
        "offered": 100_000,
        "planned": 100_000,
        "rate": 10_000,
        "producer_elapsed_s": 10.0,
        "producer_error": None,
        "counts": {"committed": 100_000, "dropped": 0, "stale": 0, "rejected": 0},
        "drained": True,
        "committed_at_end": 99_900,
        "lag_p99_us": 800.0,
        "gil_enabled": False,
        "expect_gil": "off",
    }
    base.update(overrides)
    return bench.run_criteria(**base)


# -- argument parsing --------------------------------------------------------------------------------------


def test_parse_cpus_expands_ranges_and_lists_sorted(bench):
    assert bench.parse_cpus("0-3,6,5") == (0, 1, 2, 3, 5, 6)


@pytest.mark.parametrize("text", ["5-2", "1,,2", "-1", "", "a"])
def test_parse_cpus_rejects_malformed_input(bench, text):
    with pytest.raises(ValueError):
        bench.parse_cpus(text)


def test_parse_rates_sorts_and_drops_duplicates(bench):
    assert bench.parse_rates("8000,2000,8000") == (2000, 8000)


def test_parse_rates_rejects_zero_rate(bench):
    with pytest.raises(ValueError):
        bench.parse_rates("0,2000")


def test_select_arms_returns_requested_subset_in_order(bench):
    arms = bench.select_arms("py314t-n4,py312-n0")
    assert [a.name for a in arms] == ["py314t-n4", "py312-n0"]
    assert [a.workers for a in arms] == [4, 0]


def test_select_arms_rejects_unknown_arm(bench):
    with pytest.raises(ValueError, match="unknown arm"):
        bench.select_arms("py314t-n3")


def test_sweep_defaults_match_the_packet_matrix(bench):
    args = bench.build_parser().parse_args(["sweep", "--out-dir", "out"])
    assert args.rates == (2000, 4000, 8000, 11500, 16000)
    assert args.seconds == 30.0
    assert args.repeats == 3
    assert args.cpus == tuple(range(8))
    assert [a.name for a in args.arms] == [
        "py312-n0",
        "py314t-n0",
        "py314t-n1",
        "py314t-n2",
        "py314t-n4",
        "py314t-n8",
    ]


def test_run_parser_reads_rate_workers_and_expect_gil(bench):
    args = bench.build_parser().parse_args(
        ["run", "--workers", "4", "--rate", "11500", "--expect-gil", "off", "--out", "one.json"]
    )
    assert (args.workers, args.rate, args.expect_gil) == (4, 11500, "off")
    assert args.queue_size == 65_536


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--workers", "0", "--rate", "0", "--out", "x.json"],
        ["run", "--workers", "-1", "--rate", "100", "--out", "x.json"],
        ["run", "--workers", "0", "--rate", "100", "--bidask-fraction", "1.5", "--out", "x.json"],
        ["run", "--workers", "0", "--rate", "100", "--cpus", "4-1", "--out", "x.json"],
        ["sweep", "--out-dir", "o", "--arms", "nope"],
        ["sweep", "--out-dir", "o", "--repeats", "0"],
    ],
)
def test_parser_rejects_invalid_values_with_exit_code_2(bench, argv):
    with pytest.raises(SystemExit) as exc:
        bench.build_parser().parse_args(argv)
    assert exc.value.code == 2


# -- per-run criteria --------------------------------------------------------------------------------------


def test_run_is_sustained_when_every_condition_holds(bench):
    crit = _criteria(bench)
    assert crit["sustained"] is True
    assert crit["literal_sustained"] is True


def test_run_is_not_sustained_after_any_drop(bench):
    crit = _criteria(bench, counts={"committed": 99_999, "dropped": 1, "stale": 0, "rejected": 0})
    assert crit["zero_drops"] is False
    assert crit["sustained"] is False


def test_run_is_not_sustained_when_lag_p99_reaches_the_5ms_limit(bench):
    assert _criteria(bench, lag_p99_us=4_999.9)["sustained"] is True
    assert _criteria(bench, lag_p99_us=5_000.0)["sustained"] is False


def test_run_with_a_growing_backlog_passes_the_literal_rule_but_is_not_sustained(bench):
    crit = _criteria(bench, committed_at_end=50_000)
    assert crit["literal_sustained"] is True
    assert crit["keeps_up"] is False
    assert crit["sustained"] is False


def test_run_is_not_conserved_when_an_event_is_unaccounted(bench):
    crit = _criteria(bench, counts={"committed": 99_999, "dropped": 0, "stale": 0, "rejected": 0})
    assert crit["conserved"] is False
    assert crit["sustained"] is False


def test_run_is_not_conserved_when_the_pipeline_did_not_drain(bench):
    assert _criteria(bench, drained=False)["conserved"] is False


def test_run_requires_the_gil_state_the_arm_expects(bench):
    assert _criteria(bench, gil_enabled=True, expect_gil="off")["gil_ok"] is False
    assert _criteria(bench, gil_enabled=False, expect_gil="on")["gil_ok"] is False
    assert _criteria(bench, gil_enabled=True, expect_gil="on")["gil_ok"] is True
    assert _criteria(bench, gil_enabled=True, expect_gil="any")["gil_ok"] is True


def test_run_is_not_sustained_when_the_producer_missed_the_offered_rate(bench):
    crit = _criteria(bench, producer_elapsed_s=12.0)
    assert crit["offered_ok"] is False
    assert crit["sustained"] is False


def test_run_is_not_sustained_when_the_producer_raised(bench):
    assert _criteria(bench, producer_error="RuntimeError: boom")["offered_ok"] is False


def test_conservation_splits_the_unaccounted_events_into_rejected_and_residual(bench):
    cons = bench.conservation(offered=1000, committed=900, dropped=50, stale=10, rejected=15)
    assert cons == {"unaccounted": 40, "rejected": 15, "residual": 25}


def test_latency_stats_report_microseconds_and_nearest_rank_percentiles(bench):
    stats = bench.latency_stats_us(list(range(1000, 101_000, 1000)))
    assert stats == {"n": 100, "p50": 51.0, "p99": 99.0, "max": 100.0}
    assert bench.latency_stats_us([]) == {"n": 0, "p50": None, "p99": None, "max": None}


# -- workload ----------------------------------------------------------------------------------------------


def test_workload_weights_match_the_packet_mix_and_hit_the_top10_target(bench):
    spec = bench.WorkloadSpec()
    weights = bench.combined_weights(spec, bench.solve_options_exponent(spec))
    futures = sum(weights[: spec.n_futures])
    stocks = sum(weights[spec.n_futures : spec.n_futures + spec.n_stocks])
    options = sum(weights[spec.n_futures + spec.n_stocks :])
    assert len(weights) == 2900
    assert futures == pytest.approx(0.13 / 0.99, abs=1e-9)
    assert stocks == pytest.approx(0.12 / 0.99, abs=1e-9)
    assert options == pytest.approx(0.74 / 0.99, abs=1e-9)
    assert bench.top_share(weights) == pytest.approx(0.15, abs=0.002)


def test_symbols_are_unique_and_ordered_futures_then_stocks_then_options(bench):
    symbols, groups = bench.build_symbols(bench.WorkloadSpec())
    assert len(symbols) == len(set(symbols)) == 2900
    assert groups.count("future") == 30 and groups.count("stock") == 60 and groups.count("option") == 2810
    assert groups == sorted(groups, key=["future", "stock", "option"].index)
    assert all(s.startswith("TXO") for s, g in zip(symbols, groups, strict=True) if g == "option")


def test_plan_is_deterministic_per_seed_and_changes_with_the_seed(bench):
    spec = bench.WorkloadSpec(n_symbols=200)
    first = bench.build_plan(spec, 1000, 1.0, 2)
    second = bench.build_plan(spec, 1000, 1.0, 2)
    other = bench.build_plan(bench.WorkloadSpec(n_symbols=200, seed=7), 1000, 1.0, 2)
    assert first.digest == second.digest
    assert first.digest != other.digest
    assert len(first.events) == 1000
    assert len(first.warmup) == 400
    assert all(isinstance(e, dict) and e["code"] in set(first.symbols) for e in first.events)


def test_plan_payloads_carry_shioaji_fields_and_a_fresh_copy_is_needed_per_event(bench):
    plan = bench.build_plan(bench.WorkloadSpec(n_symbols=200), 500, 1.0, 1)
    ticks = [e for e in plan.events if "close" in e]
    bidasks = [e for e in plan.events if "bid_price" in e]
    assert ticks and bidasks and len(ticks) + len(bidasks) == 500
    assert all(len(b["bid_price"]) == len(b["ask_volume"]) == 5 for b in bidasks)
    assert all("ts" not in e and "_t" not in e for e in plan.events)


def test_plan_rejects_a_universe_smaller_than_the_futures_and_stocks(bench):
    with pytest.raises(ValueError):
        bench.build_plan(bench.WorkloadSpec(n_symbols=90), 100, 1.0, 1)


# -- gate --------------------------------------------------------------------------------------------------


def test_gate_passes_when_a_314t_arm_sustains_the_target_in_all_repeats_and_cost_is_within_15_percent(bench):
    gate = bench.evaluate_gate(_passing_fixture(cost_314=114.0))
    assert gate["status"] == "PASS"
    assert gate["capacity"]["qualifying"] == {"py314t-n4": [11500]}
    assert gate["cost"]["ratio"] == pytest.approx(1.14)


def test_gate_fails_when_one_repeat_at_the_target_is_not_sustained(bench):
    records = _runs("py312-n0", "py312", 0, 2000, [True] * 3)
    records += _runs("py314t-n0", "py314t", 0, 2000, [True] * 3)
    records += _runs("py314t-n4", "py314t", 4, 11500, [True, False, True])
    gate = bench.evaluate_gate(records)
    assert gate["status"] == "FAIL"
    assert gate["capacity"]["passed"] is False
    assert gate["cost"]["passed"] is True


def test_gate_fails_when_n0_cost_exceeds_15_percent(bench):
    gate = bench.evaluate_gate(_passing_fixture(cost_314=116.0))
    assert gate["status"] == "FAIL"
    assert gate["cost"]["passed"] is False
    assert gate["capacity"]["passed"] is True


def test_gate_cost_exactly_at_the_limit_still_passes(bench):
    assert bench.evaluate_gate(_passing_fixture(cost_314=115.0))["status"] == "PASS"


def test_gate_is_incomplete_when_the_target_step_has_fewer_than_all_repeats(bench):
    records = _runs("py312-n0", "py312", 0, 2000, [True] * 3)
    records += _runs("py314t-n0", "py314t", 0, 2000, [True] * 3)
    records += _runs("py314t-n4", "py314t", 4, 11500, [True, True])
    gate = bench.evaluate_gate(records)
    assert gate["status"] == "INCOMPLETE"
    assert gate["capacity"]["passed"] is None


def test_gate_is_incomplete_without_a_312_baseline(bench):
    records = _runs("py314t-n0", "py314t", 0, 2000, [True] * 3)
    records += _runs("py314t-n4", "py314t", 4, 11500, [True] * 3)
    gate = bench.evaluate_gate(records)
    assert gate["status"] == "INCOMPLETE"
    assert gate["cost"]["passed"] is None


def test_gate_does_not_count_a_312_arm_toward_capacity(bench):
    records = _runs("py312-n0", "py312", 0, 2000, [True] * 3)
    records += _runs("py312-n0", "py312", 0, 11500, [True] * 3)
    records += _runs("py314t-n0", "py314t", 0, 2000, [True] * 3)
    records += _runs("py314t-n4", "py314t", 4, 11500, [False] * 3)
    gate = bench.evaluate_gate(records)
    assert gate["status"] == "FAIL"
    assert gate["capacity"]["qualifying"] == {}


def test_gate_counts_a_step_above_the_target(bench):
    records = _runs("py312-n0", "py312", 0, 2000, [True] * 3)
    records += _runs("py314t-n0", "py314t", 0, 2000, [True] * 3)
    records += _runs("py314t-n8", "py314t", 8, 11500, [False] * 3)
    records += _runs("py314t-n8", "py314t", 8, 16000, [True] * 3)
    gate = bench.evaluate_gate(records)
    assert gate["status"] == "PASS"
    assert gate["capacity"]["qualifying"] == {"py314t-n8": [16000]}


def test_gate_cost_reads_the_highest_step_where_both_n0_arms_are_sustained(bench):
    records = _runs("py312-n0", "py312", 0, 2000, [True] * 3, cpu=100.0)
    records += _runs("py314t-n0", "py314t", 0, 2000, [True] * 3, cpu=130.0)
    records += _runs("py312-n0", "py312", 0, 4000, [True] * 3, cpu=80.0)
    records += _runs("py314t-n0", "py314t", 0, 4000, [True] * 3, cpu=88.0)
    records += _runs("py312-n0", "py312", 0, 8000, [False] * 3, cpu=60.0)
    records += _runs("py314t-n0", "py314t", 0, 8000, [True] * 3, cpu=90.0)
    records += _runs("py314t-n4", "py314t", 4, 11500, [True] * 3)
    gate = bench.evaluate_gate(records)
    assert gate["cost"]["reference_rate"] == 4000
    assert gate["cost"]["ratio"] == pytest.approx(1.10)
    assert gate["cost"]["per_rate"]["2000"] == pytest.approx(1.30)
    assert gate["status"] == "PASS"


def test_gate_cost_falls_back_to_the_lowest_common_step_when_none_is_clean(bench):
    records = _runs("py312-n0", "py312", 0, 2000, [False] * 3, cpu=100.0)
    records += _runs("py314t-n0", "py314t", 0, 2000, [False] * 3, cpu=100.0)
    records += _runs("py312-n0", "py312", 0, 4000, [False] * 3, cpu=100.0)
    records += _runs("py314t-n0", "py314t", 0, 4000, [False] * 3, cpu=200.0)
    gate = bench.evaluate_gate(records)
    assert gate["cost"]["reference_rate"] == 2000
    assert gate["cost"]["clean_steps"] == []


# -- summary -----------------------------------------------------------------------------------------------


def test_summary_max_sustained_rate_needs_every_repeat_sustained(bench):
    records = _runs("py314t-n2", "py314t", 2, 2000, [True] * 3)
    records += _runs("py314t-n2", "py314t", 2, 4000, [True] * 3)
    records += _runs("py314t-n2", "py314t", 2, 8000, [True, True, False])
    summary = bench.summarize(records, repeats=3)
    assert summary["arms"]["py314t-n2"]["max_sustained_rate"] == 4000
    row = next(r for r in summary["rows"] if r["rate"] == 8000)
    assert (row["sustained_runs"], row["runs"]) == (2, 3)


def test_summary_counts_a_failed_child_as_unsustained_and_incomplete(bench):
    failed = {
        "schema": 1,
        "arm": "py314t-n1",
        "interpreter": "py314t",
        "workers": 1,
        "rate": 2000,
        "repeat": 3,
        "error": "child exit 1",
        "criteria": {"sustained": False},
    }
    records = _runs("py314t-n1", "py314t", 1, 2000, [True, True]) + [failed]
    summary = bench.summarize(records, repeats=3)
    row = summary["rows"][0]
    assert (row["completed"], row["sustained_runs"], row["runs"]) == (2, 2, 3)
    assert summary["arms"]["py314t-n1"]["max_sustained_rate"] is None


def test_summary_flags_arms_that_did_not_see_the_same_workload(bench):
    records = [
        _record("py312-n0", "py312", 0, 2000, 1, digest="a"),
        _record("py314t-n0", "py314t", 0, 2000, 1, digest="b"),
    ]
    records += [
        _record("py312-n0", "py312", 0, 4000, 1, digest="c"),
        _record("py314t-n0", "py314t", 0, 4000, 1, digest="c"),
    ]
    identical = bench.summarize(records, repeats=1)["workload_identical_per_rate"]
    assert identical == {"2000": False, "4000": True}


# -- sweep plumbing ----------------------------------------------------------------------------------------


def test_child_env_drops_inherited_hft_shioaji_and_python_variables(bench, monkeypatch, tmp_path):
    monkeypatch.setenv("HFT_ORDER_MODE", "live")
    monkeypatch.setenv("SHIOAJI_API_KEY", "x")
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    env = bench.child_env(tmp_path)
    assert "HFT_ORDER_MODE" not in env and "SHIOAJI_API_KEY" not in env
    assert env["PYTHONPATH"] == str(tmp_path)
    assert env["HFT_MD_RECORD_DIRECT"] == "1"
    assert env["HFT_MONITOR_LIVE_ENABLED"] == "0"


def test_child_command_expects_the_gil_off_only_for_the_314t_arms(bench):
    args = bench.build_parser().parse_args(["sweep", "--out-dir", "o"])
    py312 = bench.child_command("python3.12", Path("s.py"), bench.DEFAULT_ARMS[0], 2000, 1, Path("o.json"), args)
    py314t = bench.child_command("python3.14t", Path("s.py"), bench.DEFAULT_ARMS[4], 8000, 2, Path("o.json"), args)
    assert py312[py312.index("--expect-gil") + 1] == "on"
    assert py314t[py314t.index("--expect-gil") + 1] == "off"
    assert py314t[py314t.index("--workers") + 1] == "4"
    assert py314t[py314t.index("--cpus") + 1] == "0,1,2,3,4,5,6,7"


def test_summarize_command_prints_the_verdict_and_maps_it_to_the_exit_code(bench, tmp_path, capsys):
    for name, records, code, verdict in (
        ("pass", _passing_fixture(110.0), 0, "VERDICT: PASS"),
        ("fail", _passing_fixture(130.0), 1, "VERDICT: FAIL"),
        ("incomplete", _runs("py314t-n4", "py314t", 4, 11500, [True] * 3), 2, "VERDICT: INCOMPLETE"),
    ):
        folder = tmp_path / name
        folder.mkdir()
        for i, record in enumerate(records):
            (folder / f"r{i}.json").write_text(json.dumps(record), encoding="utf-8")
        assert bench.main(["summarize", "--in-dir", str(folder)]) == code
        assert verdict in capsys.readouterr().out


def test_run_once_with_sharded_workers_reports_conserved_counts(bench, tmp_path):
    """Regression: the sharded arm crashed on a counter P3 removed (ShardWorker.stale_skipped)."""
    out = tmp_path / "w1.json"
    args = bench.build_parser().parse_args(
        ["run", "--workers", "1", "--rate", "200", "--seconds", "0.5", "--symbols", "120", "--out", str(out)]
    )
    result = bench.run_once(args)
    assert result["workers"] == 1
    assert result["counts"]["stale"] == 0
    assert result["counts"]["unaccounted"] == 0
