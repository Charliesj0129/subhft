from __future__ import annotations

import json
import os
from argparse import Namespace
from dataclasses import asdict, fields, replace
from types import SimpleNamespace

import numpy as np
import pytest

import hft_platform.cli as cli
from research.combinatorial.smma_dataset import BarDataset, save_governed_dataset
from research.combinatorial.smma_runner import (
    DISCOVERY_NUMERIC_THREAD_ENV,
    FAMILY_REGISTRY,
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
    _build_candidate_evaluation_context,
    _discovery_process_pool,
    _discovery_trial_sharpe_std,
    _evaluate_candidate,
    _evaluate_discovery_worker,
    _exact_horizon_inputs,
    _feedback_seed,
    _initialize_discovery_worker,
    _project_horizon_signal,
    _timestamp_signal_correlation,
    _with_integrity_hash,
    code_fingerprint,
    enumerate_candidates,
    evaluate_robustness_slices,
    feature_history_bars_for_expression,
    mining_status,
    resource_decision,
)
from research.combinatorial.smma_validation import (
    HarnessControlSummary,
    KillMetrics,
    LockedMetrics,
    build_split_plan,
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


def _install_single_group_adaptive_fixture(monkeypatch) -> None:
    adapter = FAMILY_REGISTRY["smma"]

    def build_features(bars, _config):
        count = len(bars)
        return {
            "x": np.linspace(-1.0, 1.0, count),
            "y": np.cos(np.arange(count, dtype=np.float64) / 4.0),
        }

    monkeypatch.setitem(
        FAMILY_REGISTRY,
        "smma",
        replace(
            adapter,
            build_features=build_features,
            dataset=replace(adapter.dataset, roots=("TXF",)),
        ),
    )
    monkeypatch.setattr(
        "research.combinatorial.smma_runner.generated_gp_expressions",
        lambda *_args, **_kwargs: ["x", "sign(y)"],
    )


def _seed_generation_zero(run: MiningRun, bars: BarDataset):
    built = run._build_group_context(
        bars,
        "TXF",
        60,
        expression_limit=2,
        seed=1,
        record_effective_trials=False,
        semantic_dedupe=True,
    )
    group_bars, labels, signals, expressions, candidates = built
    proposal_ids = run._record_generation_zero(
        root="TXF",
        timeframe=60,
        seed=1,
        expressions=expressions,
        candidates=candidates,
    )
    passing_kill = KillMetrics(0.02, 0.02, 0.02, 1.0, 2.0, 1.0, 0.1, 8.0, True, ())
    killed = KillMetrics(0.0, 0.0, 0.0, 1.0, -1.0, 0.0, 0.0, 11.0, False, ("net_edge",))
    passed_expressions: set[str] = set()
    for candidate in candidates:
        row = {
            "candidate_id": candidate.candidate_id,
            "stage": "discovery",
            "search_generation": 0,
            "generation_proposal_id": proposal_ids[candidate.expression],
            "candidate": asdict(candidate),
        }
        if candidate.duplicate_of is not None:
            row.update(
                {
                    "status": "deduplicated",
                    "failure_reason": "exact_resolved_cut_duplicate",
                    "reference_candidate_id": candidate.duplicate_of,
                }
            )
        elif candidate.expression not in passed_expressions:
            passed_expressions.add(candidate.expression)
            row.update({"status": "passed", "metrics": asdict(passing_kill), "failure_reason": ""})
        else:
            row.update({"status": "killed", "metrics": asdict(killed), "failure_reason": "net_edge"})
        assert run._ledger.append(row)
    return group_bars, labels, signals, expressions, candidates


def _new_seeded_adaptive_run(monkeypatch, run_dir, *, workers: int, max_candidates: int = 72):
    _install_single_group_adaptive_fixture(monkeypatch)
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(
        RunConfig(
            run_dir=run_dir,
            timeframes_minutes=(60,),
            max_candidates=max_candidates,
            feedback_expressions_per_group=1,
            workers=workers,
            seeds=(1, 2, 3),
        )
    )
    bars = _dataset()
    context = _seed_generation_zero(run, bars)
    return run, bars, context


def _normalized_adaptive_rows(run: MiningRun) -> list[dict[str, object]]:
    ignored = {"previous_hash", "recorded_at_ns"}
    normalized = [{key: value for key, value in row.items() if key not in ignored} for row in run._ledger.rows()]
    return json.loads(json.dumps(normalized, sort_keys=True))


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


def _evaluation_dataset(timeframe_min: int, *, days: int = 24) -> tuple[BarDataset, BarDataset]:
    base = _dataset(days=days)
    if timeframe_min in (2, 60):
        feature_bars = replace(
            base,
            timeframe_min=np.full(len(base), timeframe_min, dtype=np.int16),
        )
        return feature_bars, feature_bars

    indices = np.arange(0, len(base), 2, dtype=np.int64)
    values = {field: np.asarray(getattr(base, field))[indices].copy() for field in _field_names(base)}
    values["timeframe_min"][:] = timeframe_min
    values["reset"][:] = True
    values["session_close"][:] = True
    feature_bars = BarDataset(**values)
    combined = BarDataset(
        **{
            field: np.concatenate((np.asarray(getattr(base, field)), np.asarray(getattr(feature_bars, field))))
            for field in _field_names(base)
        }
    )
    return combined, feature_bars


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

    execution_bars, signal, labels, target_timeframe, _history_starts = _exact_horizon_inputs(
        dataset=combined,
        candidate=candidate,
        feature_bars=coarse,
        feature_signal=np.asarray([1.0, 2.0]),
        split_labels=np.asarray(["discovery", "discovery"]),
        feature_history_bars=2,
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
    assert _history_starts[[1, 3]].tolist() == [0, 0]
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

    execution_bars, signal, labels, target_timeframe, _history_starts = _exact_horizon_inputs(
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


@pytest.mark.parametrize(
    ("timeframe_min", "horizon"),
    [
        (2, "1h"),
        (60, "1h"),
        (60, "4h"),
        (60, "session"),
        (120, "1h"),
        (240, "1h"),
    ],
)
def test_cached_candidate_context_is_exactly_equal_to_uncached_evaluation(
    timeframe_min: int,
    horizon: str,
) -> None:
    dataset, feature_bars = _evaluation_dataset(timeframe_min)
    labels = np.full(len(feature_bars), "discovery", dtype="<U18")
    labels[-6:] = "selection"
    signal = np.sin(np.arange(len(feature_bars), dtype=np.float64) / 2.0)
    signal[3] = np.nan
    candidate = Candidate(
        candidate_id=f"cached-{timeframe_min}-{horizon}",
        root="TXF",
        timeframe_min=timeframe_min,
        expression="signal",
        horizon=horizon,
        direction=-1,
        threshold=-0.25,
        seed=1,
        complexity=1,
    )

    expected = _evaluate_candidate(
        candidate,
        dataset=dataset,
        bars=feature_bars,
        signal=signal,
        split_name="discovery",
        split_labels=labels,
        evaluation_fraction=0.25,
        cost_mode="per_contract",
    )
    context = _build_candidate_evaluation_context(
        dataset=dataset,
        candidate=candidate,
        feature_bars=feature_bars,
        split_name="discovery",
        split_labels=labels,
        evaluation_fraction=0.25,
    )
    projected_signal, _history_starts = _project_horizon_signal(
        grid=context.grid,
        feature_bars=feature_bars,
        feature_signal=signal,
    )
    actual = _evaluate_candidate(
        candidate,
        dataset=dataset,
        bars=feature_bars,
        signal=signal,
        split_name="discovery",
        split_labels=labels,
        evaluation_fraction=0.25,
        cost_mode="per_contract",
        evaluation_context=context,
        projected_signal=projected_signal,
    )
    reference_bars, reference_signal, reference_labels, target_timeframe, _history = _exact_horizon_inputs(
        dataset=dataset,
        candidate=candidate,
        feature_bars=feature_bars,
        feature_signal=signal,
        split_labels=labels,
    )

    assert actual.to_dict() == expected.to_dict()
    assert context.grid.target_timeframe_min == target_timeframe
    assert not np.shares_memory(context.grid.feature_labels, labels)
    assert _field_names(context.grid.execution_bars) == _field_names(reference_bars)
    for field in _field_names(reference_bars):
        np.testing.assert_array_equal(
            getattr(context.grid.execution_bars, field),
            getattr(reference_bars, field),
        )
    np.testing.assert_allclose(projected_signal, reference_signal, rtol=0, atol=0, equal_nan=True)
    np.testing.assert_array_equal(context.grid.execution_labels, reference_labels)


def test_coarse_horizon_rejects_a_misaligned_signal_before_execution_grid_access() -> None:
    _dataset_with_execution, feature_bars = _evaluation_dataset(120)
    labels = np.full(len(feature_bars), "discovery", dtype="<U18")
    candidate = Candidate(
        candidate_id="misaligned-coarse",
        root="TXF",
        timeframe_min=120,
        expression="signal",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )

    with pytest.raises(ValueError, match="feature bars, signal, and split labels"):
        _exact_horizon_inputs(
            dataset=feature_bars,
            candidate=candidate,
            feature_bars=feature_bars,
            feature_signal=np.zeros(len(feature_bars) - 1),
            split_labels=labels,
        )
    with pytest.raises(ValueError, match="feature bars, signal, and split labels"):
        _evaluate_candidate(
            candidate,
            dataset=feature_bars,
            bars=feature_bars,
            signal=np.zeros(len(feature_bars) - 1),
            split_name="discovery",
            split_labels=labels,
        )


def test_cached_candidate_still_validates_the_raw_expression_signal() -> None:
    dataset, feature_bars = _evaluation_dataset(120)
    labels = np.full(len(feature_bars), "discovery", dtype="<U18")
    candidate = Candidate(
        candidate_id="cached-raw-validation",
        root="TXF",
        timeframe_min=120,
        expression="signal",
        horizon="1h",
        direction=1,
        threshold=0.0,
        seed=1,
        complexity=1,
    )
    context = _build_candidate_evaluation_context(
        dataset=dataset,
        candidate=candidate,
        feature_bars=feature_bars,
        split_name="discovery",
        split_labels=labels,
        evaluation_fraction=0.25,
    )
    projected, _history_starts = _project_horizon_signal(
        grid=context.grid,
        feature_bars=feature_bars,
        feature_signal=np.ones(len(feature_bars)),
    )

    with pytest.raises(ValueError, match="feature bars, signal, and split labels"):
        _evaluate_candidate(
            candidate,
            dataset=dataset,
            bars=feature_bars,
            signal=np.ones(len(feature_bars) - 1),
            split_name="discovery",
            split_labels=labels,
            evaluation_fraction=0.25,
            evaluation_context=context,
            projected_signal=projected,
        )

    labels[0] = "selection"
    assert context.grid.feature_labels[0] == "discovery"
    with pytest.raises(RunIntegrityError, match="does not match the feature grid"):
        _evaluate_candidate(
            candidate,
            dataset=dataset,
            bars=feature_bars,
            signal=np.ones(len(feature_bars)),
            split_name="discovery",
            split_labels=labels,
            evaluation_fraction=0.25,
            evaluation_context=context,
            projected_signal=projected,
        )


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


def test_feature_history_composes_gp_and_family_base_lookbacks() -> None:
    assert (
        feature_history_bars_for_expression(
            "bidask",
            "ts_delta(bidask_mid_shift_ratio_slope6, 14)",
        )
        == 21
    )
    assert (
        feature_history_bars_for_expression(
            "kbar",
            "ts_delta(kbar_gap_return_delta6, 14)",
        )
        == 22
    )
    assert feature_history_bars_for_expression("smma", "close_l3_atr14_distance") is None


def test_unstreamable_expression_is_rejected_even_for_recursive_smma() -> None:
    with pytest.raises(ValueError, match="Cannot stream expression"):
        feature_history_bars_for_expression("smma", "rank(close_l3_atr14_distance)")


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

    execution_bars, signal, labels, target_timeframe, _history_starts = _exact_horizon_inputs(
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


def test_expression_eligibility_is_unchanged_when_non_discovery_values_change(
    monkeypatch,
    tmp_path,
) -> None:
    bars = _dataset()
    discovery_rows = len(bars) // 2
    feature = np.arange(len(bars), dtype=np.float64)
    feature[discovery_rows:] = np.inf
    captured: dict[str, tuple[str, ...]] = {}

    monkeypatch.setattr(
        "research.combinatorial.smma_runner.build_smma_family_features",
        lambda **_kwargs: {"usable": feature},
    )

    def fake_generate(feature_names, *, seed, limit):
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


def test_generation_zero_semantically_deduplicates_commutative_expressions(
    monkeypatch,
    tmp_path,
) -> None:
    bars = _dataset()
    a = np.linspace(-1.0, 1.0, len(bars))
    b = np.cos(np.arange(len(bars), dtype=np.float64) / 4.0)
    monkeypatch.setattr(
        "research.combinatorial.smma_runner.build_smma_family_features",
        lambda **_kwargs: {"a": a, "b": b},
    )
    monkeypatch.setattr(
        "research.combinatorial.smma_runner.generated_gp_expressions",
        lambda *_args, **_kwargs: ["add(a, b)", "add(b, a)"],
    )
    run = MiningRun(RunConfig(run_dir=tmp_path / "run", seeds=(1, 2, 3)))

    _bars, _labels, _signals, expressions, candidates = run._build_group_context(
        bars,
        "TXF",
        60,
        expression_limit=2,
        seed=1,
        semantic_dedupe=True,
    )

    assert expressions == ["add(a, b)"]
    assert len(candidates) == 24
    assert run._expression_supply["TXF/60"]["semantic_duplicate_rejected"] == 1


def test_feedback_zero_preserves_blind_v1_semantic_duplicates(
    monkeypatch,
    tmp_path,
) -> None:
    bars = _dataset()
    a = np.linspace(-1.0, 1.0, len(bars))
    b = np.cos(np.arange(len(bars), dtype=np.float64) / 4.0)
    monkeypatch.setattr(
        "research.combinatorial.smma_runner.build_smma_family_features",
        lambda **_kwargs: {"a": a, "b": b},
    )
    monkeypatch.setattr(
        "research.combinatorial.smma_runner.generated_gp_expressions",
        lambda *_args, **_kwargs: ["add(a, b)", "add(b, a)"],
    )
    run = MiningRun(RunConfig(run_dir=tmp_path / "run", seeds=(1, 2, 3)))

    _bars, _labels, _signals, expressions, candidates = run._build_group_context(
        bars,
        "TXF",
        60,
        expression_limit=2,
        seed=1,
    )

    assert expressions == ["add(a, b)", "add(b, a)"]
    assert len(candidates) == 48
    assert run._expression_supply["TXF/60"]["semantic_duplicate_rejected"] == 0


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
    other_generation_seed = enumerate_candidates(
        family="bidask",
        root="TXF",
        timeframe_min=60,
        expressions=["usable"],
        signals={"usable": np.asarray([0.0, 1.0, 2.0, 3.0])},
        discovery_mask=np.asarray([True, True, True, False]),
        seed=999,
    )

    assert [candidate.candidate_id for candidate in first] == [candidate.candidate_id for candidate in second]
    assert [candidate.threshold for candidate in first] != [candidate.threshold for candidate in second]
    assert [candidate.candidate_id for candidate in first] == [
        candidate.candidate_id for candidate in other_generation_seed
    ]
    assert first[0].candidate_id != other_family[0].candidate_id


def test_candidate_quantiles_are_resolved_once_per_expression_direction(monkeypatch) -> None:
    import research.combinatorial.smma_runner as runner

    original = runner.resolve_quantile_threshold
    calls: list[tuple[int, float]] = []

    def counted_resolution(signal, *, direction, quantile):
        calls.append((int(direction), float(quantile)))
        return original(signal, direction=direction, quantile=quantile)

    monkeypatch.setattr(runner, "resolve_quantile_threshold", counted_resolution)
    candidates = enumerate_candidates(
        family="kbar",
        root="TXF",
        timeframe_min=60,
        expressions=["usable"],
        signals={"usable": np.linspace(-2.0, 2.0, 101)},
        discovery_mask=np.ones(101, dtype=bool),
        seed=1,
    )

    assert calls == [(direction, quantile) for direction in (1, -1) for quantile in (0.50, 0.70, 0.85, 0.95)]
    assert [(candidate.horizon, candidate.direction, candidate.threshold_quantile) for candidate in candidates] == [
        (horizon, direction, quantile)
        for horizon in ("1h", "4h", "session")
        for direction in (1, -1)
        for quantile in (0.50, 0.70, 0.85, 0.95)
    ]
    for direction in (1, -1):
        by_horizon = [
            [
                candidate.threshold_resolution
                for candidate in candidates
                if candidate.horizon == horizon and candidate.direction == direction
            ]
            for horizon in ("1h", "4h", "session")
        ]
        assert by_horizon[0] == by_horizon[1] == by_horizon[2]


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


def test_process_discovery_matches_single_process_candidate_results() -> None:
    bars = _dataset(days=20)
    labels = np.full(len(bars), "discovery")
    signals = {"trend": np.linspace(-1.0, 1.0, len(bars))}
    candidates = [
        Candidate(
            candidate_id=f"candidate-{direction}",
            root="TXF",
            timeframe_min=60,
            expression="trend",
            horizon="1h",
            direction=direction,
            threshold=0.25,
            seed=1,
            complexity=1,
        )
        for direction in (1, -1)
    ]
    expected = [
        _evaluate_candidate(
            candidate,
            dataset=bars,
            bars=bars,
            signal=signals[candidate.expression],
            split_name="discovery",
            split_labels=labels,
            evaluation_fraction=0.25,
            cost_mode="per_contract",
        ).to_dict()
        for candidate in candidates
    ]

    prior_thread_env = {name: os.environ.get(name) for name in DISCOVERY_NUMERIC_THREAD_ENV}
    with _discovery_process_pool(
        workers=2,
        dataset=bars,
        bars=bars,
        signals=signals,
        labels=labels,
        cost_mode="per_contract",
        has_work=True,
    ) as executor:
        assert executor is not None
        assert {os.environ[name] for name in DISCOVERY_NUMERIC_THREAD_ENV} == {"1"}
        actual = [result.to_dict() for result in executor.map(_evaluate_discovery_worker, candidates)]

    assert actual == expected
    assert {name: os.environ.get(name) for name in DISCOVERY_NUMERIC_THREAD_ENV} == prior_thread_env


def test_discovery_worker_reuses_group_context_and_initializer_clears_it(monkeypatch) -> None:
    import research.combinatorial.smma_runner as runner

    bars = _dataset(days=24)
    labels = np.full(len(bars), "discovery", dtype="<U18")
    signals = {
        "x": np.linspace(-1.0, 1.0, len(bars)),
        "y": np.cos(np.arange(len(bars), dtype=np.float64) / 3.0),
    }
    candidates = [
        Candidate(
            candidate_id=f"worker-{expression}-{direction}-{threshold}",
            root="TXF",
            timeframe_min=60,
            expression=expression,
            horizon="1h",
            direction=direction,
            threshold=threshold,
            seed=1,
            complexity=1,
        )
        for expression in ("x", "y")
        for direction in (1, -1)
        for threshold in (0.0, 0.5)
    ]
    second_bars = replace(bars, close=np.asarray(bars.close)[::-1].copy())
    second_expected = _evaluate_candidate(
        candidates[0],
        dataset=second_bars,
        bars=second_bars,
        signal=signals["x"],
        split_name="discovery",
        split_labels=labels,
        evaluation_fraction=0.25,
        cost_mode="per_contract",
    ).to_dict()
    counts = {"targets": 0, "returns": 0, "detrend": 0, "projection": 0}
    real_targets = runner.forward_target_indices
    real_returns = runner.forward_returns
    real_detrend = runner.rolling_detrend
    real_projection = runner._project_horizon_signal

    def counted_targets(**kwargs):
        counts["targets"] += 1
        return real_targets(**kwargs)

    def counted_returns(*args, **kwargs):
        counts["returns"] += 1
        return real_returns(*args, **kwargs)

    def counted_detrend(*args, **kwargs):
        counts["detrend"] += 1
        return real_detrend(*args, **kwargs)

    def counted_projection(**kwargs):
        counts["projection"] += 1
        return real_projection(**kwargs)

    monkeypatch.setattr(runner, "forward_target_indices", counted_targets)
    monkeypatch.setattr(runner, "forward_returns", counted_returns)
    monkeypatch.setattr(runner, "rolling_detrend", counted_detrend)
    monkeypatch.setattr(runner, "_project_horizon_signal", counted_projection)

    _initialize_discovery_worker(bars, bars, signals, labels, "per_contract")
    [_evaluate_discovery_worker(candidate) for candidate in candidates]
    assert counts == {"targets": 1, "returns": 1, "detrend": 1, "projection": 2}

    _initialize_discovery_worker(second_bars, second_bars, signals, labels, "per_contract")
    second_actual = [_evaluate_discovery_worker(candidate) for candidate in candidates]
    assert counts == {"targets": 2, "returns": 2, "detrend": 2, "projection": 4}
    assert second_actual[0].to_dict() == second_expected


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


def test_hash_chain_ledger_counts_only_unique_discovery_candidates_after_resume(tmp_path) -> None:
    path = tmp_path / "trials.jsonl"
    ledger = HashChainLedger(path)
    assert ledger.append({"candidate_id": "a", "stage": "generation", "status": "accepted"})
    assert ledger.append({"candidate_id": "a", "stage": "discovery", "status": "passed"})
    assert ledger.append({"candidate_id": "b", "stage": "selection", "status": "killed"})
    assert ledger.unique_candidates == 1

    restored = HashChainLedger(path)

    assert restored.unique_candidates == 1


def test_dsr_trial_sharpe_dispersion_excludes_non_evaluated_duplicates() -> None:
    rows = (
        {"status": "passed", "metrics": {"clustered_sharpe": 1.0}},
        {"status": "deduplicated"},
        {"status": "killed", "metrics": {"clustered_sharpe": 3.0}},
        {"status": "generation", "metrics": {"clustered_sharpe": 100.0}},
    )

    assert _discovery_trial_sharpe_std(rows) == pytest.approx(np.sqrt(2.0))


def test_blind_v1_dsr_dispersion_retains_legacy_duplicate_rows() -> None:
    rows = (
        {"status": "passed", "metrics": {"clustered_sharpe": 1.0}},
        {"status": "deduplicated"},
        {"status": "killed", "metrics": {"clustered_sharpe": 3.0}},
    )

    assert _discovery_trial_sharpe_std(rows, evaluated_only=False) == pytest.approx(
        np.std(np.asarray([1.0, 0.0, 3.0]), ddof=1)
    )


def test_resume_rejects_changed_generation_evidence_for_existing_proposal_id(tmp_path) -> None:
    run = MiningRun(RunConfig(run_dir=tmp_path / "run", seeds=(1, 2, 3)))
    original = {
        "candidate_id": "proposal-1",
        "stage": "generation",
        "status": "rejected",
        "event": "expression_proposal",
        "rejection_reason": "semantic_duplicate",
    }
    run._append_generation_evidence(original)

    with pytest.raises(RunIntegrityError, match="evidence changed during resume"):
        run._append_generation_evidence({**original, "rejection_reason": "discovery_constant"})


def test_adaptive_lineage_rejects_discovery_row_linked_to_wrong_proposal(tmp_path) -> None:
    run = MiningRun(
        RunConfig(
            run_dir=tmp_path / "run",
            timeframes_minutes=(60,),
            max_candidates=144,
            feedback_expressions_per_group=1,
            seeds=(1, 2, 3),
        )
    )
    expressions = ("x", "y")
    candidates = (
        Candidate("candidate-x", "TXF", 60, "x", "1h", 1, 0.0, 1, 1),
        Candidate("candidate-y", "TXF", 60, "y", "1h", 1, 0.0, 1, 1),
    )
    for proposal_id, expression, candidate in zip(("proposal-x", "proposal-y"), expressions, candidates, strict=True):
        run._append_generation_evidence(
            {
                "candidate_id": proposal_id,
                "stage": "generation",
                "status": "accepted",
                "event": "expression_proposal",
                "search_generation": 0,
                "family": "smma",
                "root": "TXF",
                "timeframe_min": 60,
                "expression": expression,
                "parent_candidate_ids": [],
                "candidate_ids": [candidate.candidate_id],
            }
        )
    for candidate, wrong_proposal in zip(candidates, ("proposal-y", "proposal-x"), strict=True):
        run._ledger.append(
            {
                "candidate_id": candidate.candidate_id,
                "stage": "discovery",
                "status": "killed",
                "search_generation": 0,
                "generation_proposal_id": wrong_proposal,
                "candidate": asdict(candidate),
            }
        )

    with pytest.raises(RunIntegrityError, match="wrong generation proposal"):
        run._validate_adaptive_lineage()


def test_adaptive_budget_reserves_equal_per_group_quota_and_leaves_tail_unused(tmp_path) -> None:
    run = MiningRun(
        RunConfig(
            run_dir=tmp_path / "run",
            timeframes_minutes=(60,),
            max_candidates=145,
            feedback_expressions_per_group=1,
            seeds=(1, 2, 3),
        )
    )

    assert run._adaptive_search_budget() == {
        "strategy": "discovery_feedback_v1",
        "groups": 2,
        "candidate_variants_per_expression": 24,
        "generation_zero_expressions_per_group": 2,
        "feedback_expressions_per_group": 1,
        "allocated_candidate_ceiling": 144,
        "unallocated_candidate_tail": 1,
        "feedback_generator_version": "typed_crossover_v1",
    }


def test_adaptive_config_fails_closed_on_final_holdout_or_missing_generation_zero_budget(tmp_path) -> None:
    with pytest.raises(ValueError, match="cannot unlock the final holdout"):
        RunConfig(
            run_dir=tmp_path / "holdout",
            timeframes_minutes=(60,),
            max_candidates=144,
            feedback_expressions_per_group=1,
            unlock_final_holdout=True,
        ).validate()

    with pytest.raises(ValueError, match="leave at least one generation-0 expression"):
        RunConfig(
            run_dir=tmp_path / "budget",
            timeframes_minutes=(60,),
            max_candidates=48,
            feedback_expressions_per_group=1,
        ).validate()


def test_feedback_parents_are_distinct_generation_zero_discovery_passes_only(tmp_path) -> None:
    run = MiningRun(RunConfig(run_dir=tmp_path / "run", seeds=(1, 2, 3)))
    passing_kill = KillMetrics(0.02, 0.02, 0.02, 1.0, 2.0, 1.0, 0.1, 8.0, True, ())

    def append(candidate_id: str, expression: str, generation: int, status: str = "passed") -> None:
        candidate = Candidate(
            candidate_id=candidate_id,
            root="TXF",
            timeframe_min=60,
            expression=expression,
            horizon="1h",
            direction=1,
            threshold=0.0,
            seed=1,
            complexity=1,
        )
        run._ledger.append(
            {
                "candidate_id": candidate_id,
                "stage": "discovery",
                "status": status,
                "search_generation": generation,
                "candidate": asdict(candidate),
                "metrics": asdict(passing_kill),
            }
        )

    append("g0-x-best", "x", 0)
    append("g0-x-second", "x", 0)
    append("g0-y", "sign(y)", 0)
    append("g1-z", "z", 1)
    append("g0-killed", "ts_delta(x, 3)", 0, status="killed")

    parents = run._generation_zero_parents(root="TXF", timeframe=60)

    assert {item.candidate.candidate_id for item in parents} == {"g0-x-best", "g0-y"}


def test_feedback_resume_reuses_identical_lineage_without_generation_one_parents(
    monkeypatch,
    tmp_path,
) -> None:
    bars = _dataset()
    labels = build_split_plan(bars.trading_day).labels
    x = np.linspace(-1.0, 1.0, len(bars))
    y = np.cos(np.arange(len(bars), dtype=np.float64) / 4.0)
    monkeypatch.setattr(
        "research.combinatorial.smma_runner.build_smma_family_features",
        lambda **_kwargs: {"x": x, "y": y},
    )
    run = MiningRun(RunConfig(run_dir=tmp_path / "run", seeds=(1, 2, 3)))
    passing_kill = KillMetrics(0.02, 0.02, 0.02, 1.0, 2.0, 1.0, 0.1, 8.0, True, ())
    parents = (
        Candidate("g0-x", "TXF", 60, "x", "1h", 1, 0.0, 1, 1),
        Candidate("g0-y", "TXF", 60, "sign(y)", "1h", 1, 0.0, 1, 1),
        Candidate("g1-poison", "TXF", 60, "y", "1h", 1, 0.0, 1, 1),
    )
    for generation, candidate in zip((0, 0, 1), parents, strict=True):
        run._ledger.append(
            {
                "candidate_id": candidate.candidate_id,
                "stage": "discovery",
                "status": "passed",
                "search_generation": generation,
                "candidate": asdict(candidate),
                "metrics": asdict(passing_kill),
            }
        )

    first = run._build_feedback_context(
        root="TXF",
        timeframe=60,
        seed=7,
        bars=bars,
        labels=labels,
        generation_zero_expressions=("x", "sign(y)"),
        feedback_limit=2,
    )
    row_count = run._ledger.row_count
    second = run._build_feedback_context(
        root="TXF",
        timeframe=60,
        seed=7,
        bars=bars,
        labels=labels,
        generation_zero_expressions=("x", "sign(y)"),
        feedback_limit=2,
    )
    generation_one_rows = [
        row
        for row in run._ledger.rows(stage="generation")
        if row.get("event") == "expression_proposal" and row.get("status") == "accepted"
    ]

    assert first[1] == second[1]
    assert [candidate.candidate_id for candidate in first[2]] == [candidate.candidate_id for candidate in second[2]]
    assert run._ledger.row_count == row_count
    assert generation_one_rows
    assert all("g1-poison" not in row["parent_candidate_ids"] for row in generation_one_rows)


def test_feedback_generation_is_unchanged_when_later_split_values_change(
    monkeypatch,
    tmp_path,
) -> None:
    source = _dataset()
    labels = build_split_plan(source.trading_day).labels
    later = labels != "discovery"
    baseline_close = source.close.copy()
    baseline_close[later] = 0.0
    baseline = replace(
        source,
        open=baseline_close,
        high=baseline_close,
        low=baseline_close,
        close=baseline_close,
        bid_open=baseline_close,
        ask_open=baseline_close,
        bid_close=baseline_close,
        ask_close=baseline_close,
    )
    mutated_close = baseline_close.copy()
    mutated_close[later] = np.arange(int(np.count_nonzero(later)), dtype=np.float64) + 1.0
    mutated = replace(
        baseline,
        open=mutated_close,
        high=mutated_close,
        low=mutated_close,
        close=mutated_close,
        bid_open=mutated_close,
        ask_open=mutated_close,
        bid_close=mutated_close,
        ask_close=mutated_close,
    )

    def fake_features(**kwargs):
        close = np.asarray(kwargs["close"], dtype=np.float64)
        x = np.zeros(close.size, dtype=np.float64)
        x[later] = close[later]
        return {
            "x": x,
            "y": np.zeros(close.size, dtype=np.float64),
        }

    monkeypatch.setattr("research.combinatorial.smma_runner.build_smma_family_features", fake_features)
    monkeypatch.setattr(
        "research.combinatorial.smma_runner.generated_feedback_proposals",
        lambda *_args, **_kwargs: (
            SimpleNamespace(
                attempt=0,
                attempt_seed=123,
                parent_expressions=("x", "y"),
                expression="add(x, y)",
                semantic_hash="semantic-add-x-y",
                generator_status="candidate",
                rejection_reason="",
            ),
        ),
    )
    passing_kill = KillMetrics(0.02, 0.02, 0.02, 1.0, 2.0, 1.0, 0.1, 8.0, True, ())

    def build(run_dir, bars):
        run = MiningRun(
            RunConfig(
                run_dir=run_dir,
                timeframes_minutes=(60,),
                max_candidates=144,
                feedback_expressions_per_group=1,
                seeds=(1, 2, 3),
            )
        )
        for candidate_id, expression in (("parent-x", "x"), ("parent-y", "y")):
            candidate = Candidate(candidate_id, "TXF", 60, expression, "1h", 1, 0.0, 1, 1)
            run._ledger.append(
                {
                    "candidate_id": candidate_id,
                    "stage": "discovery",
                    "status": "passed",
                    "search_generation": 0,
                    "candidate": asdict(candidate),
                    "metrics": asdict(passing_kill),
                }
            )
        result = run._build_feedback_context(
            root="TXF",
            timeframe=60,
            seed=7,
            bars=bars,
            labels=labels,
            generation_zero_expressions=("x", "y"),
            feedback_limit=1,
        )
        evidence = [
            {key: value for key, value in row.items() if key != "previous_hash"}
            for row in run._ledger.rows(stage="generation")
        ]
        return result, evidence

    first, first_evidence = build(tmp_path / "baseline", baseline)
    second, second_evidence = build(tmp_path / "mutated", mutated)

    assert first[0] == second[0] == {}
    assert first[1] == second[1]
    assert [asdict(candidate) for candidate in first[2]] == [asdict(candidate) for candidate in second[2]]
    assert first_evidence == second_evidence


def test_insufficient_parent_diversity_leaves_feedback_budget_unused(
    monkeypatch,
    tmp_path,
) -> None:
    bars = _dataset()
    labels = build_split_plan(bars.trading_day).labels
    x = np.linspace(-1.0, 1.0, len(bars))
    monkeypatch.setattr(
        "research.combinatorial.smma_runner.build_smma_family_features",
        lambda **_kwargs: {"x": x},
    )
    run = MiningRun(RunConfig(run_dir=tmp_path / "run", seeds=(1, 2, 3)))
    candidate = Candidate("g0-x", "TXF", 60, "x", "1h", 1, 0.0, 1, 1)
    passing_kill = KillMetrics(0.02, 0.02, 0.02, 1.0, 2.0, 1.0, 0.1, 8.0, True, ())
    run._ledger.append(
        {
            "candidate_id": candidate.candidate_id,
            "stage": "discovery",
            "status": "passed",
            "search_generation": 0,
            "candidate": asdict(candidate),
            "metrics": asdict(passing_kill),
        }
    )

    signals, expressions, candidates, proposal_ids = run._build_feedback_context(
        root="TXF",
        timeframe=60,
        seed=7,
        bars=bars,
        labels=labels,
        generation_zero_expressions=("x",),
        feedback_limit=2,
    )
    closure = [row for row in run._ledger.rows(stage="generation") if row.get("event") == "generation_closure"]

    assert signals == {}
    assert expressions == []
    assert candidates == []
    assert proposal_ids == {}
    assert closure[0]["status"] == "closed_with_unused_budget"
    assert closure[0]["closure_reason"] == "insufficient_generation0_parent_diversity"
    assert closure[0]["unused_expressions"] == 2


def test_adaptive_discovery_closes_generation_zero_before_union_effective_count(
    monkeypatch,
    tmp_path,
) -> None:
    bars = _dataset()
    labels = build_split_plan(bars.trading_day).labels
    signal_x = np.linspace(-1.0, 1.0, len(bars))
    signal_y = np.cos(np.arange(len(bars), dtype=np.float64) / 4.0)
    run = MiningRun(
        RunConfig(
            run_dir=tmp_path / "run",
            timeframes_minutes=(60,),
            max_candidates=144,
            feedback_expressions_per_group=1,
            workers=1,
            seeds=(1, 2, 3),
        )
    )
    evaluation_generations: list[int] = []
    union_candidate_counts: list[int] = []

    def fake_group_context(_dataset_value, root, timeframe, expression_limit, seed, **_kwargs):
        assert expression_limit == 2
        expressions = ["x", "y"]
        signals = {"x": signal_x, "y": signal_y}
        candidates = enumerate_candidates(
            family="smma",
            root=root,
            timeframe_min=timeframe,
            expressions=expressions,
            signals=signals,
            discovery_mask=labels == "discovery",
            seed=seed,
        )
        run._expression_supply[f"{root}/{timeframe}"] = {"requested": 2, "valid": 2}
        return bars, labels, signals, expressions, candidates

    def fake_feedback_context(*, root, timeframe, seed, **_kwargs):
        expression = "sign(x)"
        signals = {expression: np.sign(signal_x)}
        child_candidates = enumerate_candidates(
            family="smma",
            root=root,
            timeframe_min=timeframe,
            expressions=[expression],
            signals=signals,
            discovery_mask=labels == "discovery",
            seed=seed,
        )
        proposal_id = f"proposal-{root}"
        parents = run._generation_zero_parents(root=root, timeframe=timeframe)[:2]
        run._append_generation_evidence(
            {
                "candidate_id": proposal_id,
                "stage": "generation",
                "status": "accepted",
                "event": "expression_proposal",
                "search_generation": 1,
                "family": "smma",
                "root": root,
                "timeframe_min": timeframe,
                "expression": expression,
                "parent_candidate_ids": [item.candidate.candidate_id for item in parents],
                "parent_expressions": [item.candidate.expression for item in parents],
                "candidate_ids": [candidate.candidate_id for candidate in child_candidates],
            }
        )
        run._generation_evidence[f"{root}/{timeframe}"] = {"feedback_accepted": 1}
        return signals, [expression], child_candidates, {expression: proposal_id}

    def fake_evaluate(*, candidates, passing, search_generation, proposal_ids, **_kwargs):
        evaluation_generations.append(search_generation)
        passed_expressions: set[str] = set()
        for candidate in candidates:
            if candidate.duplicate_of is not None:
                status = "deduplicated"
                kill = None
            elif candidate.expression not in passed_expressions:
                status = "passed"
                passed_expressions.add(candidate.expression)
                kill = KillMetrics(0.02, 0.02, 0.02, 1.0, 2.0, 1.0, 0.1, 8.0, True, ())
            else:
                status = "killed"
                kill = KillMetrics(0.0, 0.0, 0.0, 1.0, -1.0, 0.0, 0.0, 11.0, False, ("net_edge",))
            row = {
                "candidate_id": candidate.candidate_id,
                "stage": "discovery",
                "status": status,
                "search_generation": search_generation,
                "generation_proposal_id": proposal_ids[candidate.expression],
                "candidate": asdict(candidate),
            }
            if kill is not None:
                row["metrics"] = asdict(kill)
            run._ledger.append(row)
            if status == "passed" and kill is not None:
                passing.append(CandidateResult(candidate, kill, "discovery", "passed"))
        return None

    def fake_effective_count(*, candidates, **_kwargs):
        union_candidate_counts.append(len(candidates))
        return len(candidates)

    monkeypatch.setattr(run, "_build_group_context", fake_group_context)
    monkeypatch.setattr(run, "_build_feedback_context", fake_feedback_context)
    monkeypatch.setattr(run, "_evaluate_discovery_candidates", fake_evaluate)
    monkeypatch.setattr(run, "_finalize_discovery", lambda passing: list(passing))
    monkeypatch.setattr("research.combinatorial.smma_runner._effective_trigger_test_count", fake_effective_count)

    results = run._adaptive_discover(bars, [])

    assert results
    assert evaluation_generations == [0, 0, 1, 1]
    assert union_candidate_counts == [72, 72]
    assert run._effective_trial_counts == {"TMF/60": 72, "TXF/60": 72}


def test_adaptive_search_is_deterministic_across_worker_counts(monkeypatch, tmp_path) -> None:
    serial, serial_bars, _serial_context = _new_seeded_adaptive_run(
        monkeypatch,
        tmp_path / "serial",
        workers=1,
    )
    parallel, parallel_bars, _parallel_context = _new_seeded_adaptive_run(
        monkeypatch,
        tmp_path / "parallel",
        workers=2,
    )

    serial._adaptive_discover(serial_bars, [])
    parallel._adaptive_discover(parallel_bars, [])

    assert _normalized_adaptive_rows(serial) == _normalized_adaptive_rows(parallel)
    assert json.loads((serial.run_dir / "search_space.json").read_text()) == json.loads(
        (parallel.run_dir / "search_space.json").read_text()
    )


def test_adaptive_resume_after_partial_generation_one_is_bit_identical(monkeypatch, tmp_path) -> None:
    clean, clean_bars, _clean_context = _new_seeded_adaptive_run(
        monkeypatch,
        tmp_path / "clean",
        workers=1,
    )
    partial, partial_bars, partial_context = _new_seeded_adaptive_run(
        monkeypatch,
        tmp_path / "partial",
        workers=1,
    )
    clean._adaptive_discover(clean_bars, [])

    group_bars, labels, _signals, expressions, _candidates = partial_context
    feedback_seed = _feedback_seed(family="smma", root="TXF", timeframe_min=60, base_seed=1)
    feedback_signals, _feedback_expressions, feedback_candidates, proposal_ids = partial._build_feedback_context(
        root="TXF",
        timeframe=60,
        seed=feedback_seed,
        bars=group_bars,
        labels=labels,
        generation_zero_expressions=expressions,
        feedback_limit=1,
    )
    partial._evaluate_discovery_candidates(
        dataset=partial_bars,
        bars=group_bars,
        labels=labels,
        signals=feedback_signals,
        candidates=feedback_candidates[:5],
        passing=[],
        search_generation=1,
        proposal_ids=proposal_ids,
    )
    resumed = MiningRun(replace(partial.config, resume=True))
    resumed._adaptive_discover(partial_bars, [])

    assert _normalized_adaptive_rows(clean) == _normalized_adaptive_rows(resumed)
    assert json.loads((clean.run_dir / "search_space.json").read_text()) == json.loads(
        (resumed.run_dir / "search_space.json").read_text()
    )


def test_adaptive_candidate_budget_and_union_evidence_restore_end_to_end(monkeypatch, tmp_path) -> None:
    run, bars, _context = _new_seeded_adaptive_run(
        monkeypatch,
        tmp_path / "run",
        workers=1,
        max_candidates=73,
    )
    run._adaptive_discover(bars, [])
    search_space = json.loads((run.run_dir / "search_space.json").read_text())

    assert run._ledger.unique_candidates == 72
    assert search_space["adaptive_search_budget"]["allocated_candidate_ceiling"] == 72
    assert search_space["adaptive_search_budget"]["unallocated_candidate_tail"] == 1
    assert search_space["effective_trials_total"] == sum(search_space["effective_trial_counts_by_group"].values())
    assert not (run.run_dir / "split_access.jsonl").exists()

    restored = MiningRun(replace(run.config, resume=True))
    restored._restore_search_space_evidence(required=True)

    assert restored._effective_trial_counts == run._effective_trial_counts
    assert restored._generation_evidence == run._generation_evidence


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


def test_locked_checkpoint_round_trip_preserves_validation_v3_tuple_evidence() -> None:
    candidate = Candidate(
        candidate_id="candidate-v3",
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
    locked = LockedMetrics(
        -1.0,
        0.5,
        0.5,
        0.0,
        0.0,
        (0.1, 0.2),
        False,
        walk_forward_fold_trade_counts=(3, 4),
        walk_forward_fold_purged_counts=(1, 2),
        feature_history_exact=True,
        feature_history_bars=7,
    )
    original = CandidateResult(
        candidate=candidate,
        kill=kill,
        locked=locked,
        stage="locked_validation",
        status="killed",
    )

    restored = CandidateResult.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored == original


def test_smma_manifest_warns_that_validation_v3_feature_history_is_not_finite(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(RunConfig(run_dir=run_dir, seeds=(1, 2, 3)))

    identity = run._manifest_identity(_dataset())

    assert identity["validation_v3_feature_history_eligible"] is False
    assert identity["achievable_verdict_ceiling"] == "DISCOVERY_SELECTION_ONLY"
    assert any("no finite exact lookback" in warning for warning in identity["startup_warnings"])


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


def test_resume_rejects_trial_ledger_shorter_than_durable_checkpoint(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(RunConfig(run_dir=run_dir, seeds=(1, 2, 3), resume=True))
    run._ledger.append({"candidate_id": "candidate-1", "stage": "discovery", "status": "killed"})
    checkpoint = _with_integrity_hash(
        {
            "schema": "alpha_mining_checkpoint.v2",
            "stage": "discovery_complete",
            "dataset_fingerprint": run._dataset_fingerprint(),
            "code_fingerprint": run._code_fingerprint,
            "trials": 1,
            "ledger_rows": 2,
            "state": {},
            "recorded_at": "2026-08-02T00:00:00+00:00",
        },
        "checkpoint_hash",
    )
    run.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    with pytest.raises(RunIntegrityError, match="shorter than the durable checkpoint"):
        run._load_checkpoint()


def test_resume_rejects_search_space_lineage_hash_mismatch_with_ledger(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(
        RunConfig(
            run_dir=run_dir,
            timeframes_minutes=(60,),
            max_candidates=144,
            feedback_expressions_per_group=1,
            seeds=(1, 2, 3),
            resume=True,
        )
    )
    payload = _with_integrity_hash(
        {
            "schema": "alpha_mining_search_space.v3",
            "family": "smma",
            "code_fingerprint": run._code_fingerprint,
            "dataset_fingerprint": run._dataset_fingerprint(),
            "search_strategy": "discovery_feedback_v1",
            "lineage_hash": "not-the-ledger-lineage",
            "union_candidate_hash": "not-the-ledger-union",
            "effective_trial_counts_by_group": {"TXF/60": 1},
            "expression_supply_by_group": {},
        },
        "search_space_hash",
    )
    (run_dir / "search_space.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RunIntegrityError, match="lineage hash does not match"):
        run._restore_search_space_evidence(required=True)


def test_resume_rejects_search_space_candidate_union_hash_mismatch_with_ledger(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(
        RunConfig(
            run_dir=run_dir,
            timeframes_minutes=(60,),
            max_candidates=144,
            feedback_expressions_per_group=1,
            seeds=(1, 2, 3),
            resume=True,
        )
    )
    correct_lineage_hash = _with_integrity_hash({"generation_rows": []}, "value")["value"]
    payload = _with_integrity_hash(
        {
            "schema": "alpha_mining_search_space.v3",
            "family": "smma",
            "code_fingerprint": run._code_fingerprint,
            "dataset_fingerprint": run._dataset_fingerprint(),
            "search_strategy": "discovery_feedback_v1",
            "lineage_hash": correct_lineage_hash,
            "union_candidate_hash": "not-the-ledger-union",
            "effective_trial_counts_by_group": {"TXF/60": 1},
            "expression_supply_by_group": {},
        },
        "search_space_hash",
    )
    (run_dir / "search_space.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RunIntegrityError, match="candidate-union hash does not match"):
        run._restore_search_space_evidence(required=True)


def test_adaptive_resume_cannot_enter_selection_without_search_space_evidence(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(
        RunConfig(
            run_dir=run_dir,
            timeframes_minutes=(60,),
            max_candidates=144,
            feedback_expressions_per_group=1,
            seeds=(1, 2, 3),
            resume=True,
        )
    )
    run._checkpoint(stage="discovery_complete", state={"discovery_passed": []}, force=True)

    with pytest.raises(RunIntegrityError, match="no search-space evidence"):
        run._run_stages(_dataset(), {"manifest_hash": "manifest-test"})


def test_adaptive_search_budget_is_frozen_in_manifest_identity(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    first = MiningRun(
        RunConfig(
            run_dir=run_dir,
            timeframes_minutes=(60,),
            max_candidates=144,
            seeds=(1, 2, 3),
        )
    )
    dataset = first._load_or_export_dataset()
    first._ensure_manifest(dataset, {})
    resumed = MiningRun(
        RunConfig(
            run_dir=run_dir,
            timeframes_minutes=(60,),
            max_candidates=144,
            feedback_expressions_per_group=1,
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


def test_selection_builds_each_group_and_expression_once(monkeypatch, tmp_path) -> None:
    import research.combinatorial.smma_runner as runner

    base = _dataset()
    coarse_indices = np.arange(0, len(base), 2, dtype=np.int64)
    coarse = BarDataset(
        **{
            field.name: (
                np.full(coarse_indices.size, 120, dtype=np.int16)
                if field.name == "timeframe_min"
                else np.asarray(getattr(base, field.name))[coarse_indices].copy()
            )
            for field in fields(base)
        }
    )
    bars = BarDataset(
        **{
            field.name: np.concatenate((np.asarray(getattr(base, field.name)), np.asarray(getattr(coarse, field.name))))
            for field in fields(base)
        }
    )
    calls: dict[str, list[object]] = {"features": [], "expressions": []}
    adapter = runner.FAMILY_REGISTRY["smma"]

    def build_features(group_bars, _config):
        timeframe = int(group_bars.timeframe_min[0])
        calls["features"].append((str(group_bars.root[0]), timeframe))
        count = len(group_bars)
        return {
            "x": (
                np.arange(count, dtype=np.float64)
                if timeframe == 60
                else np.where(np.arange(count) % 2 == 0, -1.0, 1.0)
            ),
            "y": np.sin(np.arange(count, dtype=np.float64) * 1.7),
            "timeframe": np.full(count, timeframe, dtype=np.float64),
        }

    def evaluate_expression(expression, features, _reset):
        calls["expressions"].append((int(features["timeframe"][0]), expression))
        return features[expression]

    monkeypatch.setitem(
        runner.FAMILY_REGISTRY,
        "smma",
        replace(
            adapter,
            build_features=build_features,
            evaluate_expression=evaluate_expression,
            dataset=replace(adapter.dataset, roots=("TXF",)),
        ),
    )
    kill = KillMetrics(0.03, 0.03, 0.03, 1.0, 2.0, 1.0, 0.1, 8.0, True, ())
    killed = KillMetrics(0.0, 0.0, 0.0, 1.0, -1.0, 0.0, 0.1, 11.0, False, ("net_edge",))
    discovery = [
        CandidateResult(
            candidate=Candidate(
                candidate_id=candidate_id,
                root="TXF",
                timeframe_min=timeframe,
                expression=expression,
                horizon=horizon,
                direction=1,
                threshold=0.0,
                seed=1,
                complexity=1,
            ),
            kill=kill,
            stage="discovery",
            status="passed",
        )
        for candidate_id, timeframe, expression, horizon in (
            ("a", 60, "x", "1h"),
            ("b", 60, "x", "4h"),
            ("c", 60, "y", "1h"),
            ("d", 120, "x", "1h"),
            ("e", 120, "x", "4h"),
        )
    ]

    def pass_selection(candidate, **_kwargs):
        metrics = killed if candidate.candidate_id == "c" else kill
        return CandidateResult(
            candidate=candidate,
            kill=metrics,
            stage="selection",
            status="passed" if metrics.passed else "killed",
            failure_reason="" if metrics.passed else "net_edge",
        )

    monkeypatch.setattr(runner, "_evaluate_candidate", pass_selection)
    monkeypatch.setattr(runner.timebase, "now_ns", lambda: 123456789)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(RunConfig(run_dir=run_dir, seeds=(1, 2, 3)))

    selected = run._selection(bars, discovery)

    assert calls == {
        "features": [("TXF", 60), ("TXF", 120)],
        "expressions": [(60, "x"), (60, "y"), (120, "x")],
    }
    assert [item.candidate.candidate_id for item in selected] == ["a", "d"]
    assert [row["status"] for row in run._ledger.rows(stage="selection")] == [
        "passed",
        "correlation_deduplicated",
        "killed",
        "passed",
        "correlation_deduplicated",
    ]
    assert {row["stage"] for row in run._unlocks._ledger.rows()} == {"freeze_locked"}

    resumed_dir = tmp_path / "resumed"
    resumed_dir.mkdir()
    _write_dataset(resumed_dir)
    partial_run = MiningRun(RunConfig(run_dir=resumed_dir, seeds=(1, 2, 3)))
    partial_selected = partial_run._selection(bars, discovery[:3])
    resumed_run = MiningRun(RunConfig(run_dir=resumed_dir, seeds=(1, 2, 3), resume=True))
    resumed_selected = resumed_run._selection(bars, discovery, partial_selected)

    assert [item.to_dict() for item in resumed_selected] == [item.to_dict() for item in selected]
    assert (resumed_dir / "trials.jsonl").read_bytes() == (run_dir / "trials.jsonl").read_bytes()
    assert (resumed_dir / "split_access.jsonl").read_bytes() == (run_dir / "split_access.jsonl").read_bytes()
    assert {row["stage"] for row in resumed_run._unlocks._ledger.rows()} == {"freeze_locked"}


def test_adaptive_terminal_stop_writes_kill_report_with_explicit_incomplete_conservation(tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    run = MiningRun(
        RunConfig(
            run_dir=run_dir,
            timeframes_minutes=(60,),
            max_candidates=144,
            feedback_expressions_per_group=1,
            seeds=(1, 2, 3),
        )
    )
    run._ledger.append(
        {
            "candidate_id": "partial-pass",
            "stage": "discovery",
            "status": "passed",
        }
    )
    run._terminal_stop_reason = "wall_time"

    report = run._finish(
        {"manifest_hash": "manifest-test"},
        _dataset(),
        [],
        [],
        "KILL",
        "mining stopped before validation: wall_time",
    )

    assert report["verdict"] == "KILL"
    assert report["stage_conservation"]["post_discovery_conserved"] is False
    assert run.report_path.exists()


def test_adaptive_terminal_stop_report_is_idempotent_on_resume(monkeypatch, tmp_path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    config = RunConfig(
        run_dir=run_dir,
        timeframes_minutes=(60,),
        max_candidates=144,
        feedback_expressions_per_group=1,
        seeds=(1, 2, 3),
        resume=True,
    )
    run = MiningRun(config)
    candidate = Candidate("partial-pass", "TXF", 60, "x", "1h", 1, 0.0, 1, 1)
    kill = KillMetrics(0.02, 0.02, 0.02, 1.0, 2.0, 1.0, 0.1, 8.0, True, ())
    result = CandidateResult(candidate, kill, "discovery", "passed")
    run._ledger.append(
        {
            "candidate_id": candidate.candidate_id,
            "stage": "discovery",
            "status": "passed",
            "candidate": asdict(candidate),
            "metrics": asdict(kill),
        }
    )
    run._checkpoint(stage="discovery", state={"discovery_passed": [result.to_dict()]}, force=True)
    monkeypatch.setattr(MiningRun, "_wall_time_reached", lambda _self: True)

    first = run._run_stages(_dataset(), {"manifest_hash": "manifest-test"})
    resumed = MiningRun(config)
    second = resumed._run_stages(_dataset(), {"manifest_hash": "manifest-test"})

    assert first == second
    assert first["verdict"] == "KILL"
    assert first["terminal_stop_reason"] == "wall_time"
    assert not (run_dir / "search_space.json").exists()


def test_adaptive_terminal_stop_with_zero_discovery_passes_is_idempotent_on_resume(
    monkeypatch,
    tmp_path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_dataset(run_dir)
    config = RunConfig(
        run_dir=run_dir,
        timeframes_minutes=(60,),
        max_candidates=144,
        feedback_expressions_per_group=1,
        seeds=(1, 2, 3),
        resume=True,
    )
    run = MiningRun(config)
    run._checkpoint(stage="discovery", state={"discovery_passed": []}, force=True)
    monkeypatch.setattr(MiningRun, "_wall_time_reached", lambda _self: True)

    first = run._run_stages(_dataset(), {"manifest_hash": "manifest-test"})
    resumed = MiningRun(config)
    second = resumed._run_stages(_dataset(), {"manifest_hash": "manifest-test"})

    assert first == second
    assert first["terminal_stop_reason"] == "wall_time"
    assert first["search_space_complete"] is False


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
            "--feedback-expressions-per-group",
            "8",
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
    assert run_args.feedback_expressions_per_group == 8
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
    assert captured["config"].feedback_expressions_per_group == 0
    assert json.loads(capsys.readouterr().out)["screen_only"] is True


@pytest.mark.parametrize(
    (
        "eligible_days",
        "coverage_complete",
        "expected_mode",
        "expected_cap",
        "expected_cost_mode",
        "expected_posthoc",
    ),
    [
        (80, False, "bounded_diagnostic", 200, "root_proxy", True),
        (80, True, "full_needs_more_days", 20_000, "per_contract", False),
        (120, False, "bounded_diagnostic", 200, "root_proxy", True),
        (120, True, "full", 20_000, "per_contract", False),
    ],
)
def test_campaign_separates_full_search_from_promotion_day_eligibility(
    monkeypatch,
    capsys,
    tmp_path,
    eligible_days,
    coverage_complete,
    expected_mode,
    expected_cap,
    expected_cost_mode,
    expected_posthoc,
) -> None:
    captured: list[RunConfig] = []

    def fake_load(self):
        sidecar = self.dataset_path.with_suffix(".npz.meta.json")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"trading_day_count": eligible_days}))
        return _dataset()

    def fake_run(self):
        captured.append(self.config)
        return {
            "verdict": "KILL",
            "report_hash": f"hash-{len(captured)}",
            "cost_claim_eligible": self.config.cost_mode == "per_contract",
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
    monkeypatch.setattr(MiningRun, "run", fake_run)
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
        harness_control_samples=9,
    )

    cli.cmd_alpha_mine_campaign(args)

    report = json.loads(capsys.readouterr().out)
    assert len(captured) == 6
    assert report["schema"] == "alpha_mining_campaign.v3"
    assert report["execution_policy"] == (
        "full_per_contract_when_cost_profiles_complete_else_bounded_root_proxy_diagnostic"
    )
    assert report["promotion_policy"] == "minimum_100_eligible_trading_days; independent_of_search_breadth"
    assert report["harness_controls"]["passed"] is True
    assert len(report["harness_controls"]["code_fingerprint"]) == 64
    assert report["harness_controls"]["positive_passes"] >= 18
    assert report["harness_controls"]["null_survivors"] <= 10
    assert {leg["mode"] for leg in report["legs"]} == {expected_mode}
    assert {leg["full_search_eligible"] for leg in report["legs"]} == {coverage_complete}
    assert {leg["promotion_day_count_eligible"] for leg in report["legs"]} == {eligible_days >= 100}
    assert {leg["minimum_days_for_promotion"] for leg in report["legs"]} == {100}
    assert {config.max_candidates for config in captured} == {expected_cap}
    assert {config.cost_mode for config in captured} == {expected_cost_mode}
    assert {config.posthoc_diagnostic for config in captured} == {expected_posthoc}


def test_campaign_preserves_preflight_dataset_cache_evidence(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    captured: list[tuple[str, dict[str, object]]] = []

    def fake_load(self):
        evidence = {
            "enabled": True,
            "hit": self.config.family == "kbar",
            "cache_key": f"cache-{self.config.family}-{self.config.timeframes_minutes}",
        }
        self._dataset_cache_evidence = evidence
        sidecar = self.dataset_path.with_suffix(".npz.meta.json")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps({"trading_day_count": 120}))
        return _dataset()

    def fake_run(self):
        captured.append((self.config.family, dict(self._dataset_cache_evidence)))
        return {
            "verdict": "KILL",
            "report_hash": f"hash-{len(captured)}",
            "cost_claim_eligible": True,
        }

    coverage = {
        "observed_contracts": ["TXFG6"],
        "profiled_contracts": ["TXFG6"],
        "missing_contracts": [],
        "complete": True,
    }
    monkeypatch.setattr(MiningRun, "_load_or_export_dataset", fake_load)
    monkeypatch.setattr(MiningRun, "_validate_frozen_dataset_scope", lambda _self, _dataset: None)
    monkeypatch.setattr(MiningRun, "_cost_profile_coverage", staticmethod(lambda _dataset: coverage))
    monkeypatch.setattr(MiningRun, "run", fake_run)
    args = Namespace(
        run_root=str(tmp_path / "campaigns"),
        campaign_id="cache-evidence",
        wall_time_hours=12.0,
        max_candidates=20_000,
        diagnostic_max_candidates=200,
        diagnostic_wall_time_hours=1.0,
        workers=2,
        seeds=[1, 2, 3],
        resume=False,
        harness_control_samples=9,
    )

    cli.cmd_alpha_mine_campaign(args)

    assert len(captured) == 6
    for family, evidence in captured:
        assert evidence["enabled"] is True
        assert evidence["hit"] is (family == "kbar")
        assert str(evidence["cache_key"]).startswith(f"cache-{family}-")
    assert len(json.loads(capsys.readouterr().out)["legs"]) == 6


def test_campaign_controls_fail_closed_and_artifact_resume_is_idempotent(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    import research.combinatorial.smma_validation as validation

    failed = HarnessControlSummary(
        schema="alpha_mining_harness_controls.v1",
        seed=1,
        resample_samples=9,
        effective_trials=20,
        positive_cases=20,
        positive_passes=17,
        positive_locked_passes=17,
        positive_minimum_passes=18,
        null_cases=100,
        null_survivors=0,
        null_locked_passes=0,
        null_maximum_survivors=10,
        positive_gate_pass_counts={},
        null_gate_pass_counts={},
        passed=False,
        interpretation="conditional_harness_calibration_only_not_alpha_evidence",
        cases=(),
    )
    monkeypatch.setattr(validation, "run_locked_harness_controls", lambda **_kwargs: failed)
    monkeypatch.setattr(MiningRun, "_load_or_export_dataset", lambda _self: pytest.fail("leg started"))
    args = Namespace(
        run_root=str(tmp_path / "campaigns"),
        campaign_id="controls-fail",
        wall_time_hours=12.0,
        max_candidates=20_000,
        diagnostic_max_candidates=200,
        diagnostic_wall_time_hours=1.0,
        workers=2,
        seeds=[1, 2, 3],
        resume=False,
        harness_control_samples=9,
    )

    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_mine_campaign(args)

    assert exc.value.code == 2
    assert "campaign legs were not started" in capsys.readouterr().err
    control_path = tmp_path / "campaigns" / "controls-fail" / "harness_controls.json"
    persisted = json.loads(control_path.read_text())
    assert persisted["passed"] is False
    from hft_platform.cli._alpha import _write_alpha_mining_harness_controls

    same = _write_alpha_mining_harness_controls(
        control_path.parent,
        failed,
        code_fingerprint_value=persisted["code_fingerprint"],
    )
    assert same["control_hash"] == persisted["control_hash"]

    with pytest.raises(ValueError, match="differs from the frozen"):
        _write_alpha_mining_harness_controls(
            control_path.parent,
            failed,
            code_fingerprint_value="different-code-fingerprint",
        )


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
    assert status["run_manifest"]["discovery_executor"] == "single_process"
    assert status["run_manifest"]["discovery_worker_numeric_threads"] is None
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
