from __future__ import annotations

import json
from argparse import Namespace

import numpy as np
import pytest

import hft_platform.cli as cli
import research.combinatorial.smma_runner as runner
from research.combinatorial.bidask import build_bidask_family_features
from research.combinatorial.expression_eval import evaluate_family_expression
from research.combinatorial.kbar import build_kbar_family_features
from research.combinatorial.smma import build_smma_family_features, evaluate_smma_expression
from research.combinatorial.smma_dataset import BarDataset, save_governed_dataset
from research.combinatorial.smma_runner import (
    FAMILY_REGISTRY,
    MiningRun,
    RunConfig,
    code_fingerprint,
    mining_status,
)
from research.combinatorial.smma_validation import HarnessControlSummary
from research.combinatorial.tick import build_tick_family_features
from research.combinatorial.tick_dataset import (
    TICK_ROOTS,
    TickBarDataset,
    TickDatasetGovernanceError,
    save_governed_tick_dataset,
)


def _bars(days: int = 30) -> BarDataset:
    count = days * 2
    close = 100.0 + np.sin(np.arange(count) / 3.0) + np.arange(count) * 0.05
    trading_days = np.repeat([f"2026-06-{index + 1:02d}" for index in range(days)], 2)
    reset: np.ndarray = np.zeros(count, dtype=bool)
    reset[0] = True
    session_close: np.ndarray = np.zeros(count, dtype=bool)
    session_close[1::2] = True
    return BarDataset(
        root=np.full(count, "TXF", dtype="<U3"),
        timeframe_min=np.full(count, 60, dtype=np.int16),
        contract=np.full(count, "TXFG6", dtype="<U8"),
        trading_day=trading_days,
        session=np.full(count, "day", dtype="<U5"),
        ts_ns=np.arange(count, dtype=np.int64) * 3_600_000_000_000,
        open=close - 0.1,
        high=close + 0.8,
        low=close - 0.8,
        close=close,
        volume=np.arange(count, dtype=float) + 1.0,
        bid_open=close - 0.2,
        ask_open=close + 0.2,
        bid_qty_open=10.0 + np.arange(count, dtype=float) % 7,
        ask_qty_open=12.0 + np.arange(count, dtype=float) % 5,
        bid_close=close - 0.1,
        ask_close=close + 0.1,
        bid_qty_close=8.0 + np.arange(count, dtype=float) % 3,
        ask_qty_close=9.0 + np.arange(count, dtype=float) % 4,
        reset=reset,
        session_close=session_close,
    )


def _tick_bars(days: int = 30) -> TickBarDataset:
    count = days * 2
    index = np.arange(count, dtype=np.float64)
    close = 100.0 + np.sin(index / 3.0) + index * 0.05
    trading_days = np.repeat([f"2026-06-{day + 1:02d}" for day in range(days)], 2)
    reset: np.ndarray = np.zeros(count, dtype=bool)
    reset[0] = True
    session_close: np.ndarray = np.zeros(count, dtype=bool)
    session_close[1::2] = True
    buy = 30.0 + index % 5
    sell = 25.0 + index % 3
    unknown = 1.0 + (index % 2) * 0.25
    return TickBarDataset(
        root=np.full(count, "TXF", dtype="<U3"),
        timeframe_min=np.full(count, 60, dtype=np.int16),
        contract=np.full(count, "TXFG6", dtype="<U8"),
        trading_day=trading_days,
        session=np.full(count, "day", dtype="<U5"),
        ts_ns=np.arange(count, dtype=np.int64) * 3_600_000_000_000,
        open=close - 0.1,
        high=close + 0.8,
        low=close - 0.8,
        close=close,
        bid_open=close - 0.2,
        ask_open=close + 0.2,
        bid_close=close - 0.1,
        ask_close=close + 0.1,
        trade_tick_count=buy + sell + unknown,
        quote_update_count=200.0 + index * 4.0,
        buy_tick_count=buy,
        sell_tick_count=sell,
        unknown_tick_count=unknown,
        buy_tick_volume=buy * 2.0,
        sell_tick_volume=sell * 3.0,
        reset=reset,
        session_close=session_close,
    )


def _stub_campaign_preflight(monkeypatch) -> None:
    def fake_load(self):
        sidecar = self.dataset_path.with_suffix(".npz.meta.json")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"trading_day_count": 80}))
        return _bars()

    coverage = {
        "observed_contracts": ["TXFG6"],
        "profiled_contracts": [],
        "missing_contracts": ["TXFG6"],
        "complete": False,
    }
    monkeypatch.setattr(MiningRun, "_load_or_export_dataset", fake_load)
    monkeypatch.setattr(MiningRun, "_validate_frozen_dataset_scope", lambda _self, _dataset: None)
    monkeypatch.setattr(MiningRun, "_cost_profile_coverage", staticmethod(lambda _dataset: coverage))
    passing_controls = HarnessControlSummary(
        schema="alpha_mining_harness_controls.v1",
        seed=20260802,
        resample_samples=2_000,
        effective_trials=20,
        positive_cases=20,
        positive_passes=20,
        positive_locked_passes=20,
        positive_minimum_passes=18,
        null_cases=100,
        null_survivors=0,
        null_locked_passes=0,
        null_maximum_survivors=10,
        positive_gate_pass_counts={},
        null_gate_pass_counts={},
        passed=True,
        interpretation="conditional_harness_calibration_only_not_alpha_evidence",
        cases=(),
    )
    monkeypatch.setattr(
        "research.combinatorial.smma_validation.run_locked_harness_controls",
        lambda **_kwargs: passing_controls,
    )


def test_family_registry_exposes_exactly_the_four_supported_families() -> None:
    assert sorted(FAMILY_REGISTRY) == ["bidask", "kbar", "smma", "tick"]


def test_smma_registry_adapter_reproduces_the_direct_builder_bit_for_bit() -> None:
    bars = _bars()
    config = RunConfig(run_dir=runner.Path("unused"))

    adapted = FAMILY_REGISTRY["smma"].build_features(bars, config)
    direct = build_smma_family_features(
        open_=bars.open,
        high=bars.high,
        low=bars.low,
        close=bars.close,
        reset_mask=bars.reset,
        lengths=config.smma_lengths,
    )

    assert adapted.keys() == direct.keys()
    for name, values in direct.items():
        np.testing.assert_array_equal(adapted[name], values, err_msg=name)


def test_smma_registry_expression_adapter_reproduces_the_direct_builder_bit_for_bit() -> None:
    bars = _bars()
    expression = "close_l3_atr14_distance"

    adapted = FAMILY_REGISTRY["smma"].build_features_for_expression(bars, expression)
    direct = build_smma_family_features(
        open_=bars.open,
        high=bars.high,
        low=bars.low,
        close=bars.close,
        reset_mask=bars.reset,
        lengths=runner.smma_lengths_from_expression(expression),
    )

    assert adapted.keys() == direct.keys()
    for name, values in direct.items():
        np.testing.assert_array_equal(adapted[name], values, err_msg=name)


def test_smma_registry_keeps_the_smma_specific_expression_evaluator() -> None:
    assert FAMILY_REGISTRY["smma"].evaluate_expression is evaluate_smma_expression
    assert FAMILY_REGISTRY["bidask"].evaluate_expression is evaluate_family_expression
    assert FAMILY_REGISTRY["kbar"].evaluate_expression is evaluate_family_expression
    assert FAMILY_REGISTRY["tick"].evaluate_expression is evaluate_family_expression


def test_bidask_registry_adapter_reproduces_the_direct_builder_bit_for_bit() -> None:
    bars = _bars()
    config = RunConfig(run_dir=runner.Path("unused"), family="bidask")

    adapted = FAMILY_REGISTRY["bidask"].build_features(bars, config)
    direct = build_bidask_family_features(
        bid_open=bars.bid_open,
        ask_open=bars.ask_open,
        bid_qty_open=bars.bid_qty_open,
        ask_qty_open=bars.ask_qty_open,
        bid_close=bars.bid_close,
        ask_close=bars.ask_close,
        bid_qty_close=bars.bid_qty_close,
        ask_qty_close=bars.ask_qty_close,
        reset_mask=bars.reset,
    )

    assert adapted.keys() == direct.keys()
    for name, values in direct.items():
        np.testing.assert_array_equal(adapted[name], values, err_msg=name)


def test_kbar_registry_adapter_reproduces_the_direct_builder_bit_for_bit() -> None:
    bars = _bars()
    config = RunConfig(run_dir=runner.Path("unused"), family="kbar")

    adapted = FAMILY_REGISTRY["kbar"].build_features(bars, config)
    direct = build_kbar_family_features(
        open_=bars.open,
        high=bars.high,
        low=bars.low,
        close=bars.close,
        volume=bars.volume,
        reset_mask=bars.reset,
    )

    assert adapted.keys() == direct.keys()
    for name, values in direct.items():
        np.testing.assert_array_equal(adapted[name], values, err_msg=name)


def test_kbar_family_reuses_smma_dataset_io_with_its_wider_window() -> None:
    kbar_dataset = FAMILY_REGISTRY["kbar"].dataset
    smma_dataset = FAMILY_REGISTRY["smma"].dataset
    assert (kbar_dataset.date_from, kbar_dataset.date_to) == ("2026-01-27", "2026-07-29")
    assert kbar_dataset.export is smma_dataset.export
    assert kbar_dataset.load is smma_dataset.load
    assert kbar_dataset.roots == smma_dataset.roots


def test_kbar_bounded_run_writes_a_screen_only_report(monkeypatch, tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    save_governed_dataset(
        run_dir / "dataset.npz",
        _bars(),
        query_evidence=[{"query_sha256": "test", "guard_overall": "pass"}],
        code_fingerprint=code_fingerprint(),
    )
    monkeypatch.setattr(runner, "apply_resource_policy", lambda: {"test": True})
    monkeypatch.setattr(MiningRun, "_validate_frozen_dataset_scope", lambda _self, _dataset: None)

    report = MiningRun(
        RunConfig(
            run_dir=run_dir,
            family="kbar",
            max_candidates=1,
            workers=1,
            seeds=(1, 2, 3),
            posthoc_diagnostic=True,
            cost_mode="root_proxy",
        )
    ).run()

    assert report["screen_only"] is True
    assert report["unique_hypotheses"] == 1
    status = mining_status(run_dir)
    assert status["run_manifest"]["family"] == "kbar"
    assert status["report"]["report_hash"] == report["report_hash"]


def test_tick_registry_adapter_reproduces_the_direct_builder_bit_for_bit() -> None:
    bars = _tick_bars()
    config = RunConfig(run_dir=runner.Path("unused"), family="tick")

    adapted = FAMILY_REGISTRY["tick"].build_features(bars, config)
    direct = build_tick_family_features(
        trade_tick_count=bars.trade_tick_count,
        quote_update_count=bars.quote_update_count,
        buy_tick_count=bars.buy_tick_count,
        sell_tick_count=bars.sell_tick_count,
        buy_tick_volume=bars.buy_tick_volume,
        sell_tick_volume=bars.sell_tick_volume,
        reset_mask=bars.reset,
    )

    assert adapted.keys() == direct.keys()
    for name, values in direct.items():
        np.testing.assert_array_equal(adapted[name], values, err_msg=name)


def test_tick_family_dataset_scope_is_the_tick_contract_not_the_smma_one() -> None:
    tick_dataset = FAMILY_REGISTRY["tick"].dataset
    assert (tick_dataset.date_from, tick_dataset.date_to) == ("2026-04-07", "2026-07-29")
    assert tick_dataset.roots == TICK_ROOTS
    assert tick_dataset.load is not FAMILY_REGISTRY["smma"].dataset.load
    bidask_dataset = FAMILY_REGISTRY["bidask"].dataset
    assert (bidask_dataset.date_from, bidask_dataset.date_to) == ("2026-01-27", "2026-07-29")
    assert bidask_dataset.load is FAMILY_REGISTRY["smma"].dataset.load


def test_new_family_modules_are_covered_by_the_resume_code_fingerprint() -> None:
    covered = set(runner._CODE_FILES)
    assert {
        "research/combinatorial/bidask.py",
        "research/combinatorial/kbar.py",
        "research/combinatorial/tick.py",
        "research/combinatorial/tick_dataset.py",
        "research/combinatorial/taifex_trading_dates.py",
        "research/combinatorial/expression_eval.py",
        "research/combinatorial/ledger.py",
        "research/combinatorial/partitioning.py",
    }.issubset(covered)


@pytest.mark.parametrize("family", ["smma", "bidask", "kbar", "tick"])
def test_run_config_accepts_every_registered_family(family: str, tmp_path) -> None:
    config = RunConfig(run_dir=tmp_path, family=family, seeds=(1, 2, 3))
    config.validate()
    assert config.family in FAMILY_REGISTRY


def test_run_config_rejects_an_unregistered_family(tmp_path) -> None:
    with pytest.raises(ValueError, match="unsupported family: 'nonsense'"):
        RunConfig(run_dir=tmp_path, family="nonsense", seeds=(1, 2, 3)).validate()


def test_run_config_skips_smma_length_normalisation_for_non_smma_families(tmp_path) -> None:
    with pytest.raises(ValueError):
        RunConfig(run_dir=tmp_path, family="smma", seeds=(1, 2, 3), smma_lengths=(0,)).validate()
    RunConfig(run_dir=tmp_path, family="bidask", seeds=(1, 2, 3), smma_lengths=(0,)).validate()
    RunConfig(run_dir=tmp_path, family="kbar", seeds=(1, 2, 3), smma_lengths=(0,)).validate()
    RunConfig(run_dir=tmp_path, family="tick", seeds=(1, 2, 3), smma_lengths=(0,)).validate()


def test_bidask_bounded_run_writes_a_screen_only_report(monkeypatch, tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    save_governed_dataset(
        run_dir / "dataset.npz",
        _bars(),
        query_evidence=[{"query_sha256": "test", "guard_overall": "pass"}],
        code_fingerprint=code_fingerprint(),
    )
    monkeypatch.setattr(runner, "apply_resource_policy", lambda: {"test": True})
    monkeypatch.setattr(MiningRun, "_validate_frozen_dataset_scope", lambda _self, _dataset: None)

    report = MiningRun(
        RunConfig(
            run_dir=run_dir,
            family="bidask",
            max_candidates=1,
            workers=1,
            seeds=(1, 2, 3),
            posthoc_diagnostic=True,
            cost_mode="root_proxy",
        )
    ).run()

    assert report["screen_only"] is True
    assert report["unique_hypotheses"] == 1
    status = mining_status(run_dir)
    assert status["run_manifest"]["family"] == "bidask"
    assert status["report"]["report_hash"] == report["report_hash"]


def test_tick_bounded_run_writes_a_screen_only_report(monkeypatch, tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    save_governed_tick_dataset(
        run_dir / "dataset.npz",
        _tick_bars(),
        query_evidence=[{"query_sha256": "test", "guard_overall": "pass"}],
        code_fingerprint=code_fingerprint(),
    )
    monkeypatch.setattr(runner, "apply_resource_policy", lambda: {"test": True})
    monkeypatch.setattr(MiningRun, "_validate_frozen_dataset_scope", lambda _self, _dataset: None)

    report = MiningRun(RunConfig(run_dir=run_dir, family="tick", max_candidates=1, workers=1, seeds=(1, 2, 3))).run()

    assert report["screen_only"] is True
    assert report["unique_hypotheses"] == 1
    status = mining_status(run_dir)
    assert status["run_manifest"]["family"] == "tick"
    assert status["run_manifest"]["roots"] == list(TICK_ROOTS)
    assert status["report"]["report_hash"] == report["report_hash"]


@pytest.mark.parametrize("family", ["smma", "bidask", "kbar", "tick"])
def test_parser_accepts_every_registered_family(family: str) -> None:
    args = cli.build_parser().parse_args(
        [
            "alpha",
            "mine",
            "run",
            "--family",
            family,
            "--run-dir",
            f"research/experiments/runs/{family}_test",
            "--wall-time-hours",
            "1",
            "--seeds",
            "20260726",
            "20260727",
            "20260728",
        ]
    )
    assert args.func is cli.cmd_alpha_mine_run
    assert args.family == family


def test_parser_rejects_an_unregistered_family() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["alpha", "mine", "run", "--family", "nonsense", "--run-dir", "research/experiments/runs/x"]
        )


def test_campaign_parser_and_driver_supervise_all_six_locked_diagnostic_legs(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    captured: list[RunConfig] = []

    def fake_run(self: MiningRun) -> dict[str, object]:
        captured.append(self.config)
        return {"verdict": "KILL", "report_hash": f"hash-{len(captured)}"}

    _stub_campaign_preflight(monkeypatch)
    monkeypatch.setattr(MiningRun, "run", fake_run)
    args = cli.build_parser().parse_args(
        [
            "alpha",
            "mine",
            "campaign",
            "--run-root",
            str(tmp_path),
            "--campaign-id",
            "campaign-test",
            "--max-candidates",
            "10",
            "--workers",
            "1",
        ]
    )

    assert args.func is cli.cmd_alpha_mine_campaign
    cli.cmd_alpha_mine_campaign(args)
    payload = json.loads(capsys.readouterr().out)

    assert len(captured) == 6
    assert {(config.family, config.timeframes_minutes) for config in captured} == {
        ("bidask", (2,)),
        ("bidask", (60, 120, 240)),
        ("kbar", (2,)),
        ("kbar", (60, 120, 240)),
        ("tick", (2,)),
        ("tick", (60, 120, 240)),
    }
    assert all(config.posthoc_diagnostic for config in captured)
    assert all(not config.unlock_final_holdout for config in captured)
    assert all(config.feedback_expressions_per_group == 0 for config in captured)
    assert len(payload["legs"]) == 6
    assert payload["harness_controls"]["passed"] is True
    assert (tmp_path / "campaign-test" / "campaign_report.json").exists()


def test_campaign_records_dataset_governance_failures_and_exits_nonzero(
    monkeypatch,
    tmp_path,
) -> None:
    _stub_campaign_preflight(monkeypatch)
    monkeypatch.setattr(
        MiningRun,
        "run",
        lambda _self: (_ for _ in ()).throw(TickDatasetGovernanceError("bad aggressor labels")),
    )
    args = cli.build_parser().parse_args(
        [
            "alpha",
            "mine",
            "campaign",
            "--run-root",
            str(tmp_path),
            "--campaign-id",
            "failed-campaign",
            "--max-candidates",
            "10",
            "--workers",
            "1",
        ]
    )

    with pytest.raises(SystemExit, match="2"):
        cli.cmd_alpha_mine_campaign(args)

    payload = json.loads((tmp_path / "failed-campaign" / "campaign_report.json").read_text())
    assert len(payload["legs"]) == 6
    assert all(leg["status"] == "failed" for leg in payload["legs"])
    assert all(leg["error_type"] == "TickDatasetGovernanceError" for leg in payload["legs"])


@pytest.mark.parametrize("family", ["bidask", "kbar", "tick"])
def test_cmd_run_passes_the_new_families_through_to_the_runner(family, monkeypatch, capsys, tmp_path) -> None:
    captured: dict[str, RunConfig] = {}

    def fake_run(config: RunConfig) -> dict[str, object]:
        captured["config"] = config
        return {"verdict": "KILL", "screen_only": True}

    monkeypatch.setattr(runner, "run_mining", fake_run)
    cli.cmd_alpha_mine_run(
        Namespace(
            family=family,
            run_dir=str(tmp_path / "run"),
            wall_time_hours=1,
            max_candidates=100,
            workers=2,
            seeds=[20260726, 20260727, 20260728],
            timeframes_minutes=[60],
            smma_lengths=[3, 5, 7, 10, 14, 21, 34, 55],
            posthoc_diagnostic=False,
            resume=False,
        )
    )

    assert captured["config"].family == family
    assert captured["config"].run_dir == tmp_path / "run"
    assert json.loads(capsys.readouterr().out)["screen_only"] is True


def test_mining_run_iterates_the_active_family_roots_not_the_smma_constant(monkeypatch, tmp_path) -> None:
    adapter = FAMILY_REGISTRY["tick"]
    monkeypatch.setitem(
        FAMILY_REGISTRY,
        "tick",
        runner.FamilyAdapter(
            build_features=adapter.build_features,
            build_features_for_expression=adapter.build_features_for_expression,
            evaluate_expression=adapter.evaluate_expression,
            dataset=runner.FamilyDatasetConfig(
                date_from=adapter.dataset.date_from,
                date_to=adapter.dataset.date_to,
                roots=("TXF",),
                export=adapter.dataset.export,
                load=adapter.dataset.load,
            ),
        ),
    )

    run = MiningRun(RunConfig(run_dir=tmp_path, family="tick", seeds=(1, 2, 3)))

    assert run._family_roots == ("TXF",)
    assert MiningRun(RunConfig(run_dir=tmp_path, family="smma", seeds=(1, 2, 3)))._family_roots == runner.ROOTS
