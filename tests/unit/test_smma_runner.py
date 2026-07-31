from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import asdict, fields, replace
from types import SimpleNamespace

import numpy as np
import pytest

import hft_platform.cli as cli
from research.combinatorial.smma_dataset import BarDataset, save_governed_dataset
from research.combinatorial.smma_runner import (
    OUTPUT_STOP_BYTES,
    RSS_PAUSE_BYTES,
    RSS_STOP_BYTES,
    Candidate,
    CandidateResult,
    HashChainLedger,
    MiningRun,
    RunConfig,
    RunIntegrityError,
    SplitUnlockGuard,
    _evaluate_candidate,
    _exact_horizon_inputs,
    _timestamp_signal_correlation,
    _with_integrity_hash,
    code_fingerprint,
    enumerate_candidates,
    evaluate_robustness_slices,
    mining_status,
    resource_decision,
)
from research.combinatorial.smma_validation import (
    KillMetrics,
    LockedMetrics,
    forward_target_indices,
    simulate_next_bar_execution,
)


def _dataset(days: int = 30) -> BarDataset:
    count = days * 2
    close = 100.0 + np.sin(np.arange(count) / 3.0) + np.arange(count) * 0.05
    trading_days = np.repeat([f"2026-06-{index + 1:02d}" for index in range(days)], 2)
    reset: np.ndarray = np.zeros(count, dtype=bool)
    reset[::2] = True
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
        bid_qty_open=np.full(count, 10.0),
        ask_qty_open=np.full(count, 12.0),
        bid_close=close - 0.1,
        ask_close=close + 0.1,
        bid_qty_close=np.full(count, 8.0),
        ask_qty_close=np.full(count, 9.0),
        reset=reset,
        session_close=session_close,
    )


def _write_dataset(run_dir) -> None:
    save_governed_dataset(
        run_dir / "dataset.npz",
        _dataset(),
        query_evidence=[{"query_sha256": "test", "guard_overall": "pass"}],
        code_fingerprint=code_fingerprint(),
    )


def _field_names(dataset: BarDataset) -> tuple[str, ...]:
    return tuple(item.name for item in fields(dataset))


def _with_daily(dataset: BarDataset) -> BarDataset:
    return BarDataset(
        **{
            field: np.concatenate(
                (
                    np.asarray(getattr(dataset, field)),
                    (
                        np.full(len(dataset), 1440, dtype=np.int16)
                        if field == "timeframe_min"
                        else (
                            np.full(len(dataset), "full", dtype="<U5")
                            if field == "session"
                            else np.asarray(getattr(dataset, field))
                        )
                    ),
                )
            )
            for field in _field_names(dataset)
        }
    )


def test_coarse_feature_signal_uses_exact_60_minute_execution_horizon() -> None:
    base = _dataset(days=2)
    hour_ns = 3_600_000_000_000
    base_values = {field: np.asarray(getattr(base, field)).copy() for field in _field_names(base)}
    base_values["trading_day"][:] = "2026-06-01"
    base_values["reset"][:] = False
    base_values["reset"][0] = True
    base_values["session_close"][:] = False
    base_values["session_close"][-1] = True
    base = BarDataset(**base_values)

    coarse_values = {field: np.asarray(getattr(base, field))[[0, 2]].copy() for field in _field_names(base)}
    coarse_values["timeframe_min"][:] = 120
    coarse_values["ts_ns"] = np.asarray([0, 2 * hour_ns], dtype=np.int64)
    coarse_values["reset"] = np.asarray([True, False])
    coarse_values["session_close"] = np.asarray([False, True])
    coarse = BarDataset(**coarse_values)
    combined = BarDataset(
        **{
            field: np.concatenate((np.asarray(getattr(base, field)), np.asarray(getattr(coarse, field))))
            for field in _field_names(base)
        }
    )
    candidate = Candidate(
        candidate_id="coarse-1h",
        root="TXF",
        timeframe_min=120,
        expression="close_l3_atr14_distance",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )

    execution_bars, signal, labels, target_timeframe = _exact_horizon_inputs(
        dataset=combined,
        candidate=candidate,
        feature_bars=coarse,
        feature_signal=np.asarray([1.0, 2.0]),
        split_labels=np.asarray(["discovery", "discovery"]),
    )
    targets = forward_target_indices(
        horizon="1h",
        timeframe_min=target_timeframe,
        split_labels=labels,
        reset_mask=execution_bars.reset,
        session_close=execution_bars.session_close,
    )
    execution = simulate_next_bar_execution(
        signal=signal,
        direction=1,
        threshold=0.0,
        target_indices=targets,
        bid_open=execution_bars.bid_open,
        ask_open=execution_bars.ask_open,
        bid_close=execution_bars.bid_close,
        ask_close=execution_bars.ask_close,
        reset_mask=execution_bars.reset,
        instrument_profile="TXFD6",
    )

    assert target_timeframe == 60
    assert np.flatnonzero(np.isfinite(signal)).tolist() == [1, 3]
    assert targets[1] == 2
    assert execution.entry_indices.tolist() == [2]
    assert execution.exit_indices.tolist() == [2]
    assert (
        execution_bars.ts_ns[execution.exit_indices[0]] + hour_ns - execution_bars.ts_ns[execution.entry_indices[0]]
        == hour_ns
    )


def test_partial_coarse_bar_maps_to_last_observed_60_minute_close() -> None:
    base = _dataset(days=2)
    hour_ns = 3_600_000_000_000
    base_values = {field: np.asarray(getattr(base, field))[:3].copy() for field in _field_names(base)}
    base_values["trading_day"][:] = "2026-06-01"
    base_values["session"][:] = "day"
    base_values["contract"][:] = "TXFG6"
    base_values["ts_ns"] = np.asarray([0, hour_ns, 2 * hour_ns], dtype=np.int64)
    base_values["reset"] = np.asarray([True, False, False])
    base_values["session_close"] = np.asarray([False, False, True])
    base = BarDataset(**base_values)

    coarse_values = {field: np.asarray(getattr(base, field))[[0]].copy() for field in _field_names(base)}
    coarse_values["timeframe_min"][:] = 240
    coarse_values["reset"][:] = True
    coarse_values["session_close"][:] = True
    coarse = BarDataset(**coarse_values)
    combined = BarDataset(
        **{
            field: np.concatenate((np.asarray(getattr(base, field)), np.asarray(getattr(coarse, field))))
            for field in _field_names(base)
        }
    )
    candidate = Candidate(
        candidate_id="partial-coarse-1h",
        root="TXF",
        timeframe_min=240,
        expression="close_l3_atr14_distance",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )

    execution_bars, signal, labels, target_timeframe = _exact_horizon_inputs(
        dataset=combined,
        candidate=candidate,
        feature_bars=coarse,
        feature_signal=np.asarray([1.0]),
        split_labels=np.asarray(["discovery"]),
    )

    assert target_timeframe == 60
    assert labels.tolist() == ["discovery", "discovery", "discovery"]
    assert np.flatnonzero(np.isfinite(signal)).tolist() == [2]
    assert execution_bars.session_close.tolist() == [False, False, True]


def test_run_config_enforces_frozen_resource_caps(tmp_path) -> None:
    RunConfig(run_dir=tmp_path, seeds=(1, 2, 3)).validate()
    RunConfig(
        run_dir=tmp_path,
        seeds=(1, 2, 3),
        timeframes_minutes=(2,),
        smma_lengths=(1, 2, 3, 5, 8, 13, 21, 34, 55),
    ).validate()
    with pytest.raises(ValueError, match="20000"):
        RunConfig(run_dir=tmp_path, max_candidates=20_001).validate()
    with pytest.raises(ValueError, match="three distinct"):
        RunConfig(run_dir=tmp_path, seeds=(1, 1, 2)).validate()
    with pytest.raises(ValueError, match="strictly increasing"):
        RunConfig(run_dir=tmp_path, smma_lengths=(1, 3, 2)).validate()
    with pytest.raises(ValueError, match="truthful screen adapter"):
        RunConfig(
            run_dir=tmp_path,
            family="tick",
            unlock_final_holdout=True,
        ).validate()
    with pytest.raises(ValueError, match="posthoc diagnostic"):
        RunConfig(run_dir=tmp_path, cost_mode="root_proxy").validate()
    RunConfig(
        run_dir=tmp_path,
        posthoc_diagnostic=True,
        cost_mode="root_proxy",
    ).validate()


def test_two_minute_run_also_requires_sixty_minute_completeness_evidence(tmp_path) -> None:
    run = MiningRun(
        RunConfig(
            run_dir=tmp_path / "run",
            seeds=(1, 2, 3),
            timeframes_minutes=(2,),
            smma_lengths=(1, 2, 3, 5, 8, 13, 21, 34, 55),
        )
    )

    assert run._required_dataset_timeframes() == (2, 60, 1440)


def test_dataset_cache_reuses_identical_bidask_and_kbar_exports(monkeypatch, tmp_path) -> None:
    import research.combinatorial.smma_runner as runner

    exports: list[dict[str, object]] = []
    original_bidask = runner.FAMILY_REGISTRY["bidask"]
    original_kbar = runner.FAMILY_REGISTRY["kbar"]

    def fake_export(path, **kwargs):
        exports.append(dict(kwargs))
        return save_governed_dataset(
            path,
            _dataset(),
            query_evidence=[{"query_sha256": "test", "guard_overall": "pass"}],
            code_fingerprint=str(kwargs["code_fingerprint"]),
            requested_date_from=str(kwargs["date_from"]),
            requested_date_to=str(kwargs["date_to"]),
        )

    monkeypatch.setitem(
        runner.FAMILY_REGISTRY,
        "bidask",
        runner.FamilyAdapter(
            build_features=original_bidask.build_features,
            build_features_for_expression=original_bidask.build_features_for_expression,
            evaluate_expression=original_bidask.evaluate_expression,
            dataset=runner.FamilyDatasetConfig(
                date_from=original_bidask.dataset.date_from,
                date_to=original_bidask.dataset.date_to,
                roots=original_bidask.dataset.roots,
                export=fake_export,
                load=original_bidask.dataset.load,
            ),
        ),
    )
    monkeypatch.setitem(
        runner.FAMILY_REGISTRY,
        "kbar",
        runner.FamilyAdapter(
            build_features=original_kbar.build_features,
            build_features_for_expression=original_kbar.build_features_for_expression,
            evaluate_expression=original_kbar.evaluate_expression,
            dataset=runner.FamilyDatasetConfig(
                date_from=original_kbar.dataset.date_from,
                date_to=original_kbar.dataset.date_to,
                roots=original_kbar.dataset.roots,
                export=fake_export,
                load=original_kbar.dataset.load,
            ),
        ),
    )
    cache = tmp_path / "cache"
    bidask = MiningRun(
        RunConfig(
            run_dir=tmp_path / "bidask",
            family="bidask",
            seeds=(1, 2, 3),
            dataset_cache_dir=cache,
        )
    )
    kbar = MiningRun(
        RunConfig(
            run_dir=tmp_path / "kbar",
            family="kbar",
            seeds=(1, 2, 3),
            dataset_cache_dir=cache,
        )
    )
    bidask.run_dir.mkdir()
    kbar.run_dir.mkdir()

    bidask._load_or_export_dataset()
    kbar._load_or_export_dataset()

    assert len(exports) == 1
    assert exports[0]["date_from"] == "2026-01-27"
    assert exports[0]["date_to"] == "2026-07-29"
    assert bidask._dataset_cache_evidence["hit"] is False
    assert kbar._dataset_cache_evidence["hit"] is True

    original_window = runner.build_trading_date_window("2026-01-27", "2026-07-29")
    monkeypatch.setattr(
        runner,
        "build_trading_date_window",
        lambda _date_from, _date_to: replace(
            original_window,
            calendar_mapping_hash="changed-official-calendar-mapping",
        ),
    )
    changed_calendar = MiningRun(
        RunConfig(
            run_dir=tmp_path / "changed-calendar",
            family="kbar",
            seeds=(1, 2, 3),
            dataset_cache_dir=cache,
        )
    )
    changed_calendar.run_dir.mkdir()

    changed_calendar._load_or_export_dataset()

    assert len(exports) == 2
    assert changed_calendar._dataset_cache_evidence["hit"] is False


def test_two_minute_horizon_uses_thirty_bar_target_without_projection() -> None:
    base = _dataset(days=20)
    values = {field: np.asarray(getattr(base, field))[:32].copy() for field in _field_names(base)}
    values["timeframe_min"][:] = 2
    values["trading_day"][:] = "2026-06-01"
    values["session"][:] = "day"
    values["contract"][:] = "TXFG6"
    values["ts_ns"] = np.arange(32, dtype=np.int64) * 120_000_000_000
    values["reset"][:] = False
    values["reset"][0] = True
    values["session_close"][:] = False
    values["session_close"][-1] = True
    bars = BarDataset(**values)
    candidate = Candidate(
        candidate_id="two-minute-1h",
        root="TXF",
        timeframe_min=2,
        expression="close_l1_2_atr14_spread",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )

    execution_bars, signal, labels, target_timeframe = _exact_horizon_inputs(
        dataset=bars,
        candidate=candidate,
        feature_bars=bars,
        feature_signal=np.arange(32, dtype=np.float64),
        split_labels=np.full(32, "discovery"),
    )
    targets = forward_target_indices(
        horizon="1h",
        timeframe_min=target_timeframe,
        split_labels=labels,
        reset_mask=execution_bars.reset,
        session_close=execution_bars.session_close,
    )

    assert target_timeframe == 2
    assert signal.tolist() == list(np.arange(32, dtype=np.float64))
    assert targets[0] == 30
    assert targets[1] == 31


def test_group_context_prefilters_unavailable_features_before_gp_generation(
    monkeypatch,
    tmp_path,
) -> None:
    bars = _dataset()
    captured: dict[str, tuple[str, ...]] = {}

    monkeypatch.setattr(
        "research.combinatorial.smma_runner.build_smma_family_features",
        lambda **_kwargs: {
            "usable": np.arange(len(bars), dtype=np.float64),
            "unavailable": np.full(len(bars), np.nan),
        },
    )

    def fake_generate(
        feature_names,
        *,
        seed,
        limit,
    ):
        del seed, limit
        captured["feature_names"] = tuple(feature_names)
        return ["usable"]

    monkeypatch.setattr(
        "research.combinatorial.smma_runner.generated_gp_expressions",
        fake_generate,
    )
    run = MiningRun(RunConfig(run_dir=tmp_path / "run", seeds=(1, 2, 3)))

    _bars, _labels, _signals, expressions, candidates = run._build_group_context(
        bars,
        "TXF",
        60,
        expression_limit=1,
        seed=1,
    )

    assert captured["feature_names"] == ("usable",)
    assert expressions == ["usable"]
    assert len(candidates) == 24
    assert {candidate.threshold_quantile for candidate in candidates} == {0.50, 0.70, 0.85, 0.95}
    assert all(candidate.family == "smma" for candidate in candidates)


def test_candidate_id_uses_family_and_quantile_not_resolved_cut() -> None:
    first = enumerate_candidates(
        family="bidask",
        root="TXF",
        timeframe_min=60,
        expressions=["usable"],
        signals={"usable": np.asarray([0.0, 1.0, 2.0, 3.0])},
        discovery_mask=np.asarray([True, True, True, False]),
        seed=1,
    )
    second = enumerate_candidates(
        family="bidask",
        root="TXF",
        timeframe_min=60,
        expressions=["usable"],
        signals={"usable": np.asarray([0.0, 10.0, 20.0, 30.0])},
        discovery_mask=np.asarray([True, True, True, False]),
        seed=1,
    )
    other_family = enumerate_candidates(
        family="kbar",
        root="TXF",
        timeframe_min=60,
        expressions=["usable"],
        signals={"usable": np.asarray([0.0, 1.0, 2.0, 3.0])},
        discovery_mask=np.asarray([True, True, True, False]),
        seed=1,
    )

    assert [candidate.candidate_id for candidate in first] == [candidate.candidate_id for candidate in second]
    assert [candidate.threshold for candidate in first] != [candidate.threshold for candidate in second]
    assert first[0].candidate_id != other_family[0].candidate_id


def test_exact_resolved_cut_duplicates_reference_the_lowest_quantile_candidate() -> None:
    candidates = enumerate_candidates(
        family="kbar",
        root="TXF",
        timeframe_min=60,
        expressions=["discrete"],
        signals={"discrete": np.ones(20)},
        discovery_mask=np.ones(20, dtype=bool),
        seed=1,
    )
    first_group = [candidate for candidate in candidates if candidate.horizon == "1h" and candidate.direction == 1]

    assert first_group[0].duplicate_of is None
    assert all(candidate.duplicate_of == first_group[0].candidate_id for candidate in first_group[1:])
    assert len({candidate.candidate_id for candidate in first_group}) == 4


def test_quantile_cut_is_resolved_on_discovery_split_only() -> None:
    discovery_mask = np.asarray([True, True, True, False])
    first = enumerate_candidates(
        family="bidask",
        root="TXF",
        timeframe_min=60,
        expressions=["usable"],
        signals={"usable": np.asarray([0.0, 1.0, 2.0, 1_000.0])},
        discovery_mask=discovery_mask,
        seed=1,
    )
    second = enumerate_candidates(
        family="bidask",
        root="TXF",
        timeframe_min=60,
        expressions=["usable"],
        signals={"usable": np.asarray([0.0, 1.0, 2.0, -1_000.0])},
        discovery_mask=discovery_mask,
        seed=1,
    )

    assert [candidate.threshold for candidate in first] == [candidate.threshold for candidate in second]


def test_signal_that_never_crosses_its_cut_skips_execution(monkeypatch) -> None:
    bars = _dataset(days=20)
    candidate = Candidate(
        candidate_id="never-crosses",
        root="TXF",
        timeframe_min=60,
        expression="constant",
        horizon="1h",
        direction=1,
        threshold=1.1,
        seed=1,
        complexity=1,
        threshold_quantile=0.95,
    )

    def execution_must_not_run(**_kwargs):
        raise AssertionError("full execution scan should have been skipped")

    monkeypatch.setattr(
        "research.combinatorial.smma_runner.simulate_next_bar_execution",
        execution_must_not_run,
    )
    result = _evaluate_candidate(
        candidate,
        dataset=bars,
        bars=bars,
        signal=np.ones(len(bars)),
        split_name="discovery",
        split_labels=np.full(len(bars), "discovery"),
    )

    assert result.failure_reason.endswith("insufficient_trigger_activity")


def test_selection_clustered_sharpe_uses_full_trading_day_axis() -> None:
    bars = _dataset(days=30)
    split_labels = np.full(len(bars), "discovery", dtype="<U18")
    split_labels[len(bars) // 2 :] = "selection"
    candidate = Candidate(
        candidate_id="selection-index-axis",
        root="TXF",
        timeframe_min=60,
        expression="trend",
        horizon="1h",
        direction=1,
        threshold=-10.0,
        seed=1,
        complexity=1,
    )

    result = _evaluate_candidate(
        candidate,
        dataset=bars,
        bars=bars,
        signal=np.linspace(-1.0, 1.0, len(bars)),
        split_name="selection",
        split_labels=split_labels,
    )

    assert np.isfinite(result.kill.clustered_sharpe)


def test_hash_chain_ledger_detects_tamper(tmp_path) -> None:
    path = tmp_path / "trials.jsonl"
    ledger = HashChainLedger(path)
    assert ledger.append({"candidate_id": "a", "stage": "discovery", "status": "killed"})
    assert not ledger.append({"candidate_id": "a", "stage": "discovery", "status": "passed"})
    row = json.loads(path.read_text())
    row["status"] = "passed"
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(RunIntegrityError, match="hash chain mismatch"):
        HashChainLedger(path)


def test_resume_restores_frozen_effective_trial_count_evidence(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(RunConfig(run_dir=run_dir, seeds=(1, 2, 3)))
    payload = _with_integrity_hash(
        {
            "schema": "alpha_mining_search_space.v2",
            "code_fingerprint": run._code_fingerprint,
            "dataset_fingerprint": run._dataset_fingerprint(),
            "effective_trial_counts_by_group": {"TXF/60": 7, "TMF/60": 5},
            "expression_supply_by_group": {
                "TXF/60": {"requested": 10, "valid": 9},
            },
        },
        "search_space_hash",
    )
    (run_dir / "search_space.json").write_text(json.dumps(payload))

    run._restore_search_space_evidence()

    assert run._effective_trial_counts == {"TXF/60": 7, "TMF/60": 5}
    assert run._expression_supply == {"TXF/60": {"requested": 10, "valid": 9}}


def test_restart_recovers_passing_trial_after_last_checkpoint(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    candidate = Candidate(
        candidate_id="candidate-123",
        root="TXF",
        timeframe_min=60,
        expression="close_l3_atr14_distance",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )
    kill = KillMetrics(
        raw_ic=0.02,
        detrended_ic=0.02,
        nonoverlap_ic=0.02,
        overlap_ratio=1.0,
        net_edge=2.0,
        net_sharpe=1.0,
        turnover=0.1,
        strict_edge_gap=8.0,
        passed=True,
        reasons=(),
    )
    run = MiningRun(RunConfig(run_dir=run_dir, max_candidates=1, seeds=(1, 2, 3)))
    run._ledger.append(
        {
            "candidate_id": candidate.candidate_id,
            "stage": "discovery",
            "status": "passed",
            "candidate": asdict(candidate),
            "metrics": asdict(kill),
        }
    )

    recovered = run._discover(_dataset(), [])

    assert [item.to_dict() for item in recovered] == [
        CandidateResult(
            candidate=candidate,
            kill=kill,
            stage="discovery",
            status="passed",
        ).to_dict()
    ]


def test_checkpoint_cadence_counts_post_discovery_ledger_rows(monkeypatch, tmp_path) -> None:
    import research.combinatorial.smma_runner as runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    monkeypatch.setattr(runner, "CHECKPOINT_TRIALS", 2)
    run = MiningRun(RunConfig(run_dir=run_dir, max_candidates=2, seeds=(1, 2, 3)))
    for candidate_id in ("discovery-1", "discovery-2"):
        run._ledger.append({"candidate_id": candidate_id, "stage": "discovery", "status": "killed"})
    run._checkpoint(stage="discovery", state={})
    discovery_checkpoint = json.loads(run.checkpoint_path.read_text())

    run._ledger.append({"candidate_id": "selection-1", "stage": "selection", "status": "killed"})
    run._checkpoint(stage="selection", state={"selection_passed": []})
    assert json.loads(run.checkpoint_path.read_text()) == discovery_checkpoint
    assert run._active_checkpoint_stage == "selection"

    run._ledger.append({"candidate_id": "selection-2", "stage": "selection", "status": "killed"})
    run._checkpoint(stage="selection", state={"selection_passed": []})
    selection_checkpoint = json.loads(run.checkpoint_path.read_text())
    assert selection_checkpoint["stage"] == "selection"
    assert selection_checkpoint["trials"] == 2
    assert selection_checkpoint["ledger_rows"] == 4

    run._heartbeat(stage=run._active_checkpoint_stage, force=True)
    heartbeat = json.loads(run.heartbeat_path.read_text())
    assert heartbeat["stage"] == "selection"
    assert heartbeat["trials"] == 2
    assert heartbeat["ledger_rows"] == 4


def test_resume_routes_selection_checkpoint_without_repeating_discovery(monkeypatch, tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    candidate = Candidate(
        candidate_id="candidate-123",
        root="TXF",
        timeframe_min=60,
        expression="close_l3_atr14_distance",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )
    kill = KillMetrics(0.02, 0.02, 0.02, 1.0, 2.0, 1.0, 0.1, 8.0, True, ())
    result = CandidateResult(candidate=candidate, kill=kill, stage="selection", status="passed")
    run = MiningRun(RunConfig(run_dir=run_dir, max_candidates=1, seeds=(1, 2, 3), resume=True))
    run._checkpoint(
        stage="selection",
        state={"discovery_passed": [result.to_dict()], "selection_passed": [result.to_dict()]},
        force=True,
    )
    captured: dict[str, list[CandidateResult]] = {}

    def fake_selection(_self, _dataset, discovery, restored=()):
        captured["discovery"] = list(discovery)
        captured["selection_restored"] = list(restored)
        return list(restored)

    def fake_locked(_self, _dataset, selection, restored=()):
        captured["locked_selection"] = list(selection)
        captured["locked_restored"] = list(restored)
        return []

    monkeypatch.setattr(MiningRun, "_selection", fake_selection)
    monkeypatch.setattr(MiningRun, "_locked", fake_locked)
    report = run._run_stages(_dataset(), {"manifest_hash": "manifest-test"})

    assert report["reason"] == "all candidates killed by locked validation"
    assert captured == {
        "discovery": [result],
        "selection_restored": [result],
        "locked_selection": [result],
        "locked_restored": [],
    }


def test_resume_routes_locked_checkpoint_without_repeating_selection(monkeypatch, tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    candidate = Candidate(
        candidate_id="candidate-123",
        root="TXF",
        timeframe_min=60,
        expression="close_l3_atr14_distance",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )
    kill = KillMetrics(0.02, 0.02, 0.02, 1.0, 2.0, 1.0, 0.1, 8.0, True, ())
    locked = LockedMetrics(-1.0, 0.5, 0.5, 0.0, 0.0, (0.0,) * 5, False)
    selection = CandidateResult(candidate=candidate, kill=kill, stage="selection", status="passed")
    evaluated = CandidateResult(
        candidate=candidate,
        kill=kill,
        locked=locked,
        stage="locked_validation",
        status="killed",
    )
    run = MiningRun(RunConfig(run_dir=run_dir, max_candidates=1, seeds=(1, 2, 3), resume=True))
    run._checkpoint(
        stage="locked_validation",
        state={"selection_passed": [selection.to_dict()], "locked_evaluated": [evaluated.to_dict()]},
        force=True,
    )
    captured: dict[str, list[CandidateResult]] = {}

    def fail_selection(*_args, **_kwargs):
        raise AssertionError("selection must not repeat from a locked-validation checkpoint")

    def fake_locked(_self, _dataset, selection_values, restored=()):
        captured["selection"] = list(selection_values)
        captured["restored"] = list(restored)
        return []

    monkeypatch.setattr(MiningRun, "_selection", fail_selection)
    monkeypatch.setattr(MiningRun, "_locked", fake_locked)
    report = run._run_stages(_dataset(), {"manifest_hash": "manifest-test"})

    assert report["reason"] == "all candidates killed by locked validation"
    assert captured == {"selection": [selection], "restored": [evaluated]}


def test_selection_rejects_ledger_candidate_outside_frozen_discovery(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(RunConfig(run_dir=run_dir, max_candidates=1, seeds=(1, 2, 3)))
    run._ledger.append(
        {
            "candidate_id": "unexpected",
            "stage": "selection",
            "status": "killed",
            "candidate": asdict(
                Candidate(
                    candidate_id="unexpected",
                    root="TXF",
                    timeframe_min=60,
                    expression="close_l3_atr14_distance",
                    horizon="1h",
                    direction=1,
                    threshold=0.0,
                    seed=1,
                    complexity=1,
                )
            ),
            "metrics": asdict(KillMetrics(0.0, 0.0, 0.0, 1.0, -1.0, 0.0, 0.0, 11.0, False, ("net_edge",))),
        }
    )

    with pytest.raises(RunIntegrityError, match="outside the frozen discovery set"):
        run._selection(_dataset(), [])


def test_locked_rejects_checkpoint_candidate_outside_frozen_selection(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(RunConfig(run_dir=run_dir, max_candidates=1, seeds=(1, 2, 3)))
    candidate = Candidate(
        candidate_id="unexpected",
        root="TXF",
        timeframe_min=60,
        expression="close_l3_atr14_distance",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )
    restored = CandidateResult(
        candidate=candidate,
        kill=KillMetrics(0.02, 0.02, 0.02, 1.0, 2.0, 1.0, 0.1, 8.0, True, ()),
        locked=LockedMetrics(-1.0, 0.5, 0.5, 0.0, 0.0, (0.0,) * 5, False),
        stage="locked_validation",
        status="killed",
    )

    with pytest.raises(RunIntegrityError, match="outside the frozen selection set"):
        run._locked(_dataset(), [], [restored])


def test_discovery_trend_pollution_uses_killed_horizon_metrics(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(RunConfig(run_dir=run_dir, max_candidates=3, seeds=(1, 2, 3)))
    for index, (horizon, ic, passed) in enumerate((("1h", 0.02, True), ("4h", 0.03, False), ("session", 0.04, False))):
        candidate = Candidate(
            candidate_id=f"candidate-{index}",
            root="TXF",
            timeframe_min=60,
            expression="close_l3_atr14_distance",
            horizon=horizon,
            direction=1,
            threshold=0.0,
            seed=1,
            complexity=1,
        )
        kill = KillMetrics(
            raw_ic=ic,
            detrended_ic=ic,
            nonoverlap_ic=ic,
            overlap_ratio=1.0,
            net_edge=2.0 if passed else 0.0,
            net_sharpe=1.0,
            turnover=0.1,
            strict_edge_gap=8.0,
            passed=passed,
            reasons=() if passed else ("net_edge",),
        )
        run._ledger.append(
            {
                "candidate_id": candidate.candidate_id,
                "stage": "discovery",
                "status": "passed" if passed else "killed",
                "candidate": asdict(candidate),
                "metrics": asdict(kill),
            }
        )

    assert run._discover(_dataset(), []) == []
    disposition = run._ledger.rows(stage="post_discovery")
    assert len(disposition) == 1
    assert disposition[0]["status"] == "filtered_monotonic_horizon"
    assert disposition[0]["disposition_reason"] == "monotonic_horizon_pollution"


def test_governed_contract_profile_is_visible_to_full_run_preflight(tmp_path) -> None:
    run = MiningRun(RunConfig(run_dir=tmp_path / "run", seeds=(1, 2, 3)))

    coverage = run._cost_profile_coverage(_dataset())

    assert coverage["complete"] is True
    assert coverage["profiled_contracts"] == ["TXFG6"]
    assert coverage["missing_contracts"] == []


def test_resume_manifest_mismatch_fails_closed(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    first = MiningRun(RunConfig(run_dir=run_dir, max_candidates=10, seeds=(1, 2, 3)))
    dataset = first._load_or_export_dataset()
    first._ensure_manifest(dataset, {})

    resumed = MiningRun(
        RunConfig(
            run_dir=run_dir,
            max_candidates=11,
            seeds=(1, 2, 3),
            resume=True,
        )
    )
    with pytest.raises(RunIntegrityError, match="fingerprint mismatch"):
        resumed._ensure_manifest(dataset, {})


def test_run_rejects_dataset_outside_frozen_instrument_scope(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    save_governed_dataset(
        run_dir / "dataset.npz",
        _dataset(),
        query_evidence=[
            {"timeframe_min": value, "query_sha256": "test", "guard_overall": "pass"} for value in (60, 120, 240, 1440)
        ],
        code_fingerprint=code_fingerprint(),
        requested_date_from="2026-03-19",
        requested_date_to="2026-07-24",
    )
    run = MiningRun(RunConfig(run_dir=run_dir, max_candidates=1, seeds=(1, 2, 3)))
    dataset = run._load_or_export_dataset()
    with pytest.raises(RunIntegrityError, match="complete governed dataset schema v2"):
        run._validate_frozen_dataset_scope(dataset)


def test_manifest_tamper_fails_closed_before_resume(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    first = MiningRun(RunConfig(run_dir=run_dir, max_candidates=10, seeds=(1, 2, 3)))
    dataset = first._load_or_export_dataset()
    first._ensure_manifest(dataset, {})
    manifest = json.loads(first.manifest_path.read_text())
    manifest["created_at"] = "2026-01-01T00:00:00+00:00"
    first.manifest_path.write_text(json.dumps(manifest))

    resumed = MiningRun(
        RunConfig(
            run_dir=run_dir,
            max_candidates=10,
            seeds=(1, 2, 3),
            resume=True,
        )
    )
    with pytest.raises(RunIntegrityError, match="run manifest hash mismatch"):
        resumed._ensure_manifest(dataset, {})


def test_locked_and_final_splits_require_distinct_freezes(tmp_path) -> None:
    guard = SplitUnlockGuard(tmp_path / "split_access.jsonl")
    guard.freeze_locked("candidate-123")
    guard.require("candidate-123", "locked_validation")
    with pytest.raises(RunIntegrityError, match="final_holdout is locked"):
        guard.require("candidate-123", "final_holdout")
    guard.freeze_final("candidate-123")
    guard.require("candidate-123", "final_holdout")


def test_resource_guard_pauses_then_stops_at_frozen_limits() -> None:
    assert resource_decision(rss_bytes=RSS_PAUSE_BYTES - 1, output_bytes=0) == "continue"
    assert resource_decision(rss_bytes=RSS_PAUSE_BYTES, output_bytes=0) == "pause_rss"
    assert resource_decision(rss_bytes=RSS_STOP_BYTES, output_bytes=0) == "stop_rss_limit"
    assert resource_decision(rss_bytes=0, output_bytes=OUTPUT_STOP_BYTES) == "stop_output_limit"


def test_signal_correlation_aligns_different_timeframes_by_timestamp() -> None:
    hourly_ts: np.ndarray = np.arange(10, dtype=np.int64)
    sparse_ts: np.ndarray = np.arange(0, 10, 2, dtype=np.int64)
    correlation = _timestamp_signal_correlation(
        hourly_ts,
        hourly_ts.astype(float),
        sparse_ts,
        sparse_ts.astype(float),
    )
    assert correlation == pytest.approx(1.0)


def test_selection_dispositions_are_idempotent_and_funnel_conserves(tmp_path) -> None:
    run = MiningRun(RunConfig(run_dir=tmp_path / "run", seeds=(1, 2, 3)))
    kept = Candidate(
        candidate_id="kept",
        root="TXF",
        timeframe_min=60,
        expression="kept",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )
    correlated = Candidate(
        candidate_id="correlated",
        root="TXF",
        timeframe_min=60,
        expression="correlated",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )
    capped = Candidate(
        candidate_id="capped",
        root="TXF",
        timeframe_min=60,
        expression="capped",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )
    for candidate in (correlated, capped):
        run._ledger.append(
            {
                "candidate_id": candidate.candidate_id,
                "stage": "discovery",
                "status": "passed",
            }
        )
        run._ledger.append(
            {
                "candidate_id": candidate.candidate_id,
                "stage": "post_discovery",
                "status": "advanced",
            }
        )
    timestamps = np.arange(10, dtype=np.int64)
    signal = np.arange(10, dtype=np.float64)
    selection_rows = {}

    assert run._record_selection_disposition(
        candidate=correlated,
        selection_ts=timestamps,
        selection_signal=signal,
        signals_kept=[(timestamps, signal, kept.candidate_id)],
        selected_for_root=1,
        selection_rows=selection_rows,
    )
    assert run._record_selection_disposition(
        candidate=correlated,
        selection_ts=timestamps,
        selection_signal=signal,
        signals_kept=[(timestamps, signal, kept.candidate_id)],
        selected_for_root=1,
        selection_rows=selection_rows,
    )
    assert run._record_selection_disposition(
        candidate=capped,
        selection_ts=timestamps,
        selection_signal=-signal,
        signals_kept=[],
        selected_for_root=10,
        selection_rows=selection_rows,
    )

    assert len(run._ledger.rows(stage="selection")) == 2
    _funnel, _failures, dispositions, conservation, _near = run._funnel_evidence()
    assert dispositions["selection"] == {
        "correlation_deduplicated": 1,
        "rank_capped": 1,
    }
    assert conservation["selection_conserved"] is True


def test_status_reads_artifacts_without_mutation(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    heartbeat = _with_integrity_hash({"stage": "discovery"}, "heartbeat_hash")
    (run_dir / "heartbeat.json").write_text(json.dumps(heartbeat))
    before = (run_dir / "heartbeat.json").read_bytes()
    status = mining_status(run_dir)
    assert status["heartbeat"]["stage"] == "discovery"
    assert status["unique_hypotheses"] == 0
    assert (run_dir / "heartbeat.json").read_bytes() == before


def test_parser_accepts_frozen_smma_run_and_status_contract() -> None:
    run_args = cli.build_parser().parse_args(
        [
            "alpha",
            "mine",
            "run",
            "--family",
            "smma",
            "--run-dir",
            "research/experiments/runs/smma_test",
            "--wall-time-hours",
            "72",
            "--max-candidates",
            "20000",
            "--workers",
            "12",
            "--timeframes-minutes",
            "2",
            "--smma-lengths",
            "1",
            "2",
            "3",
            "5",
            "8",
            "13",
            "21",
            "34",
            "55",
            "--seeds",
            "20260726",
            "20260727",
            "20260728",
            "--posthoc-diagnostic",
            "--resume",
        ]
    )
    assert run_args.func is cli.cmd_alpha_mine_run
    assert run_args.seeds == [20260726, 20260727, 20260728]
    assert run_args.timeframes_minutes == [2]
    assert run_args.smma_lengths == [1, 2, 3, 5, 8, 13, 21, 34, 55]
    assert run_args.posthoc_diagnostic is True
    assert run_args.unlock_final_holdout is False
    assert run_args.dataset_cache_dir is None
    assert run_args.cost_mode == "per_contract"
    status_args = cli.build_parser().parse_args(
        ["alpha", "mine", "status", "--run-dir", "research/experiments/runs/smma_test"]
    )
    assert status_args.func is cli.cmd_alpha_mine_status


def test_cmd_status_reports_integrity_error(monkeypatch, capsys) -> None:
    import research.combinatorial.smma_runner as runner

    def fail(_run_dir):
        raise RunIntegrityError("tampered")

    monkeypatch.setattr(runner, "mining_status", fail)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_mine_status(Namespace(run_dir="/tmp/not-used"))
    assert exc.value.code == 2
    assert "tampered" in capsys.readouterr().err


def test_cmd_run_passes_frozen_contract_to_runner(monkeypatch, capsys, tmp_path) -> None:
    import research.combinatorial.smma_runner as runner

    captured = {}

    def fake_run(config):
        captured["config"] = config
        return {"verdict": "KILL", "screen_only": True}

    monkeypatch.setattr(runner, "run_mining", fake_run)
    args = Namespace(
        family="smma",
        run_dir=str(tmp_path / "run"),
        wall_time_hours=72,
        max_candidates=20_000,
        workers=12,
        seeds=[20260726, 20260727, 20260728],
        timeframes_minutes=[2],
        smma_lengths=[1, 2, 3, 5, 8, 13, 21, 34, 55],
        posthoc_diagnostic=True,
        resume=False,
    )
    cli.cmd_alpha_mine_run(args)

    assert captured["config"].run_dir == tmp_path / "run"
    assert captured["config"].seeds == (20260726, 20260727, 20260728)
    assert captured["config"].timeframes_minutes == (2,)
    assert captured["config"].smma_lengths == (1, 2, 3, 5, 8, 13, 21, 34, 55)
    assert captured["config"].posthoc_diagnostic is True
    assert captured["config"].unlock_final_holdout is False
    assert captured["config"].dataset_cache_dir is None
    assert captured["config"].cost_mode == "per_contract"
    assert json.loads(capsys.readouterr().out)["screen_only"] is True


@pytest.mark.parametrize(
    ("eligible_days", "coverage_complete", "expected_mode", "expected_cap", "expected_cost_mode"),
    [
        (80, False, "bounded_diagnostic", 200, "root_proxy"),
        (120, True, "full", 20_000, "per_contract"),
    ],
)
def test_campaign_stages_each_leg_by_data_and_cost_eligibility(
    monkeypatch,
    capsys,
    tmp_path,
    eligible_days,
    coverage_complete,
    expected_mode,
    expected_cap,
    expected_cost_mode,
) -> None:
    import research.combinatorial.smma_runner as runner

    captured: list[RunConfig] = []

    def fake_load(self):
        sidecar = self.dataset_path.with_suffix(".npz.meta.json")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"trading_day_count": eligible_days}))
        return _dataset()

    def fake_run(config):
        captured.append(config)
        return {
            "verdict": "KILL",
            "report_hash": f"hash-{len(captured)}",
            "cost_claim_eligible": config.cost_mode == "per_contract",
        }

    coverage = {
        "observed_contracts": ["TXFG6"],
        "profiled_contracts": ["TXFG6"] if coverage_complete else [],
        "missing_contracts": [] if coverage_complete else ["TXFG6"],
        "complete": coverage_complete,
    }
    monkeypatch.setattr(MiningRun, "_load_or_export_dataset", fake_load)
    monkeypatch.setattr(MiningRun, "_validate_frozen_dataset_scope", lambda _self, _dataset: None)
    monkeypatch.setattr(MiningRun, "_cost_profile_coverage", staticmethod(lambda _dataset: coverage))
    monkeypatch.setattr(runner, "run_mining", fake_run)
    args = Namespace(
        run_root=str(tmp_path / "campaigns"),
        campaign_id=f"campaign-{eligible_days}",
        wall_time_hours=12.0,
        max_candidates=20_000,
        diagnostic_max_candidates=200,
        diagnostic_wall_time_hours=1.0,
        workers=2,
        seeds=[1, 2, 3],
        resume=False,
    )

    cli.cmd_alpha_mine_campaign(args)

    report = json.loads(capsys.readouterr().out)
    assert len(captured) == 6
    assert {leg["mode"] for leg in report["legs"]} == {expected_mode}
    assert {config.max_candidates for config in captured} == {expected_cap}
    assert {config.cost_mode for config in captured} == {expected_cost_mode}


def test_bounded_run_writes_kill_report_when_first_hypothesis_fails(monkeypatch, tmp_path) -> None:
    import research.combinatorial.smma_runner as runner

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    dataset = _dataset()
    dataset.contract[:] = "TXFD6"
    dataset.reset[:] = False
    dataset.reset[0] = True
    save_governed_dataset(
        run_dir / "dataset.npz",
        dataset,
        query_evidence=[{"query_sha256": "test", "guard_overall": "pass"}],
        code_fingerprint=code_fingerprint(),
    )
    monkeypatch.setattr(runner, "apply_resource_policy", lambda: {"test": True})
    monkeypatch.setattr(MiningRun, "_validate_frozen_dataset_scope", lambda _self, _dataset: None)

    report = MiningRun(
        RunConfig(
            run_dir=run_dir,
            max_candidates=1,
            workers=1,
            seeds=(1, 2, 3),
        )
    ).run()

    assert report["verdict"] == "KILL"
    assert report["unique_hypotheses"] == 1
    assert report["screen_only"] is True
    status = mining_status(run_dir)
    assert status["report"]["report_hash"] == report["report_hash"]
    assert status["run_manifest"]["timeframes_minutes"] == [60, 120, 240]
    assert status["run_manifest"]["dataset_timeframes_minutes"] == [60, 120, 240, 1440]
    assert status["run_manifest"]["smma_lengths"] == [3, 5, 7, 10, 14, 21, 34, 55]
    assert status["run_manifest"]["robustness_timeframes_minutes"] == [1440]
    assert status["run_manifest"]["posthoc_diagnostic"] is False
    assert status["run_manifest"]["final_holdout_claim_eligible"] is False
    assert status["run_manifest"]["final_holdout_unlocked"] is False
    assert status["run_manifest"]["edge_thresholds"]["TXF"]["minimum_edge_nwd"] == 200.0
    assert status["run_manifest"]["dataset_date_from"] == "2026-03-19"
    assert status["run_manifest"]["dataset_date_to"] == "2026-07-24"
    assert report["funnel"]["discovery"]["killed"] == 1
    assert report["gate_failure_histogram"]


def test_final_survivor_records_day_only_and_daily_robustness() -> None:
    candidate = Candidate(
        candidate_id="candidate-123",
        root="TXF",
        timeframe_min=60,
        expression="close_l3_atr14_distance",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )
    kill = KillMetrics(
        raw_ic=0.02,
        detrended_ic=0.02,
        nonoverlap_ic=0.02,
        overlap_ratio=1.0,
        net_edge=2.0,
        net_sharpe=1.0,
        turnover=0.1,
        strict_edge_gap=8.0,
        passed=True,
        reasons=(),
    )

    evidence = evaluate_robustness_slices(
        _with_daily(_dataset()),
        CandidateResult(candidate=candidate, kill=kill, stage="final_holdout", status="passed"),
    )

    assert set(evidence) == {"day_only", "daily_sensitivity"}
    assert evidence["day_only"]["timeframe_min"] == 60
    assert evidence["daily_sensitivity"]["timeframe_min"] == 1440
    assert evidence["daily_sensitivity"]["horizon"] == "session"


def test_screen_evidence_is_not_rerun_after_durable_completion(monkeypatch, tmp_path) -> None:
    run = MiningRun(RunConfig(run_dir=tmp_path / "run", max_candidates=1, seeds=(1, 2, 3)))
    candidate = Candidate(
        candidate_id="candidate-123",
        root="TXF",
        timeframe_min=60,
        expression="close_l3_atr14_distance",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )
    kill = KillMetrics(
        raw_ic=0.02,
        detrended_ic=0.02,
        nonoverlap_ic=0.02,
        overlap_ratio=1.0,
        net_edge=2.0,
        net_sharpe=1.0,
        turnover=0.1,
        strict_edge_gap=8.0,
        passed=True,
        reasons=(),
    )
    survivor = CandidateResult(
        candidate=candidate,
        kill=kill,
        stage="final_holdout",
        status="passed",
    )
    calls = []

    def fake_subprocess(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="screened\n", stderr="")

    monkeypatch.setattr("research.combinatorial.smma_runner.subprocess.run", fake_subprocess)
    monkeypatch.chdir(tmp_path)

    first = run._screen_survivors([survivor])
    second = run._screen_survivors([survivor])

    assert first == second
    assert len(calls) == 1
    assert first[0]["screen_only"] is True

    evidence_path = run.run_dir / "screen_evidence" / f"{candidate.candidate_id}.json"
    tampered = json.loads(evidence_path.read_text())
    tampered["outcome"]["screen_exit_code"] = 0
    evidence_path.write_text(json.dumps(tampered))
    with pytest.raises(RunIntegrityError, match="screen evidence hash mismatch"):
        run._screen_survivors([survivor])
