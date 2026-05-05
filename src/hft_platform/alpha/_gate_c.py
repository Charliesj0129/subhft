from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import numpy as np
import structlog

from hft_platform.alpha._param_opt import (
    _evaluate_parameter_robustness,
    _evaluate_stress_backtest,
    _optimize_parameters,
)
from hft_platform.alpha._stat_tests import (
    _bh_correction,
    _compute_oos_returns,
    _evaluate_oos_statistical_tests,
    _evaluate_trend_contamination,
    _extract_bds_pvalue,
    _extract_stat_test_pvalues,
)
from hft_platform.alpha._validation_helpers import (
    _ensure_project_root_on_path,
    _resolve_first_data_meta_path,
)
from hft_platform.alpha._validation_types import GateReport, ValidationConfig

logger = structlog.get_logger("alpha.gate_c")


def _daily_pnl_points(daily_pnl: list[Any] | None) -> float:
    if not daily_pnl:
        return 0.0
    total = 0.0
    for entry in daily_pnl:
        if isinstance(entry, dict):
            total += float(entry.get("pnl_pts", 0.0))
        else:
            total += float(entry)
    return total


def _daily_pnl_sequence(daily_pnl: list[Any] | None) -> list[Any]:
    return list(daily_pnl or [])


def _invoke_sub_gates(
    *,
    strategy_type: str,
    result_payload: dict,
    thresholds: dict,
    calibration_profile: Any | None = None,
    profile: Any | None = None,
) -> tuple[list[dict], dict | None]:
    """Invoke all applicable sub-gates and compute blocking aggregate.

    Returns:
        advisory: list of dicts, one per registered applicable gate.
        blocking: None if `profile is None`; otherwise
            ``{"passed": bool, "failing": [<gate_dict>], "names": [...], "profile": <name>}``
            with only gates listed in ``profile.blocking_sub_gates`` contributing
            to ``passed``. Errored gates are treated as ``passed=False`` for
            blocking purposes (fail-closed).
    """
    import numpy as np

    from hft_platform.alpha._sub_gates import (
        ensure_builtin_sub_gates_registered,
        get_registered_sub_gates,
    )
    from hft_platform.alpha._sub_gates.maker import FillRateValidationGate
    from hft_platform.backtest.result import BacktestResult

    ensure_builtin_sub_gates_registered()

    result = BacktestResult(
        run_id=result_payload.get("run_id", ""),
        config_hash=result_payload.get("config_hash", ""),
        instrument=result_payload.get("instrument", ""),
        strategy_name=result_payload.get("strategy_name", ""),
        strategy_type=strategy_type,  # type: ignore[arg-type]
        engine=result_payload.get("engine", "unknown"),
        queue_model=result_payload.get("queue_model", "unknown"),
        calibration_profile_id=result_payload.get("calibration_profile_id", "uncalibrated"),
        data_source=result_payload.get("data_source", "unknown"),
        latency_profile=str(result_payload.get("latency_profile", "")),
        pnl_pts=float(result_payload.get("pnl_pts", 0.0)),
        n_fills=int(result_payload.get("n_fills", 0)),
        n_trading_days=int(result_payload.get("n_trading_days", 0)),
        equity_curve=result_payload.get("equity_curve", np.zeros(1)),
        pnl_per_fill=result_payload.get("pnl_per_fill"),
        adverse_fill_pct=result_payload.get("adverse_fill_pct"),
        fill_rate_per_day=result_payload.get("fill_rate_per_day"),
        ic_is=result_payload.get("ic_is"),
        ic_oos=result_payload.get("ic_oos"),
        daily_pnl=list(result_payload.get("daily_pnl") or []),
    )
    if "trade_pnl" in result_payload:
        try:
            object.__setattr__(result, "trade_pnl", list(result_payload["trade_pnl"]))
        except Exception:  # noqa: BLE001
            pass
    if "replay_parity_report" in result_payload:
        # Slice C: passthrough so ReplayParityGate can read the precomputed
        # report. Task 11 will attach the report directly on BacktestResult
        # at construction; until then this lets test fixtures inject it.
        try:
            object.__setattr__(result, "replay_parity_report", result_payload["replay_parity_report"])
        except Exception:  # noqa: BLE001
            pass

    blocking_names: set[str] = set(getattr(profile, "blocking_sub_gates", ()) or ())

    advisory: list[dict] = []
    blocking_failing: list[dict] = []
    blocking_seen: list[str] = []

    for gate in get_registered_sub_gates():
        if strategy_type not in gate.applies_to:
            continue
        try:
            if isinstance(gate, FillRateValidationGate):
                sub = gate.evaluate(
                    result,
                    config=None,
                    thresholds=thresholds,
                    profile=calibration_profile,
                )
            else:
                sub = gate.evaluate(result, config=None, thresholds=thresholds)
            entry = {
                "name": sub.name,
                "passed": sub.passed,
                "metrics": sub.metrics,
                "details": sub.details,
            }
            advisory.append(entry)
            if profile is not None and sub.name in blocking_names:
                blocking_seen.append(sub.name)
                if not sub.passed:
                    blocking_failing.append(entry)
        except Exception as exc:  # noqa: BLE001
            gate_name: str = str(getattr(gate, "name", "unknown"))
            entry = {
                "name": gate_name,
                "passed": None,
                "metrics": {},
                "details": f"sub-gate error: {exc!r}",
                "error": True,
            }
            advisory.append(entry)
            if profile is not None and gate_name in blocking_names:
                blocking_seen.append(gate_name)
                blocking_failing.append(entry)

    if profile is None:
        return advisory, None
    return advisory, {
        "passed": len(blocking_failing) == 0,
        "failing": blocking_failing,
        "names": blocking_seen,
        "profile": profile.name,
    }


def _invoke_sub_gates_advisory(
    *,
    strategy_type: str,
    result_payload: dict,
    thresholds: dict,
    calibration_profile: Any | None = None,
) -> list[dict]:
    """Backward-compatible wrapper for callers that don't pass a profile."""
    advisory, _ = _invoke_sub_gates(
        strategy_type=strategy_type,
        result_payload=result_payload,
        thresholds=thresholds,
        calibration_profile=calibration_profile,
        profile=None,
    )
    return advisory


def _load_maker_thresholds(root: Path) -> dict:
    """Load Gate C maker thresholds from gate_thresholds.yaml, or return {}."""
    import yaml as _yaml

    thresholds_path = root / "config" / "research" / "gate_thresholds.yaml"
    if not thresholds_path.exists():
        return {}
    all_thresholds = _yaml.safe_load(thresholds_path.read_text())
    return dict(all_thresholds.get("maker", {}))


def _equity_to_daily_pnl(equity_curve: Any) -> list[float]:
    """Convert cumulative equity curve to list of daily PnL differences."""
    if equity_curve is not None and hasattr(equity_curve, "__len__") and len(equity_curve) > 1:
        return np.diff(np.asarray(equity_curve, dtype=float)).tolist()
    return []


def run_gate_c(  # noqa: C901 - existing complexity 17; refactor tracked as follow-up
    alpha: Any,
    config: ValidationConfig,
    root: Path,
    resolved_data_paths: list[str],
    experiments_base: Path,
) -> tuple[GateReport, str, str, str, str]:
    _ensure_project_root_on_path(root)
    from hft_platform.alpha.experiments import ExperimentTracker
    from research.backtest.hft_native_runner import HftNativeRunner, ensure_hftbt_npz
    from research.backtest.types import BacktestConfig, WalkForwardConfig
    from research.registry.scorecard import compute_scorecard

    alpha_id = alpha.manifest.alpha_id
    strategy_type = getattr(alpha.manifest, "strategy_type", "taker")
    instrument = getattr(alpha.manifest, "instrument", "")

    if strategy_type == "maker":
        # --- Maker path: CK-direct backtest ---
        from hft_platform.alpha.latency_profiles import resolve_profile
        from research.backtest.cost_models import load_cost_profile
        from research.backtest.fill_models import QueueDepletionFill
        from research.backtest.maker_engine import ClickHouseSource, LatencyProfile, MakerEngine
        from research.backtest.result_store import ResultStore

        ck_source = ClickHouseSource()
        ck_source.health_check()
        cost = load_cost_profile(instrument)
        qf = float(getattr(config, "queue_fraction", 0.5))
        fill_model = QueueDepletionFill(queue_fraction=qf)

        # Resolve latency_profile metadata string -> LatencyProfile injected into
        # MakerEngine. Missing / unresolvable names fall back to instant-RTT
        # (logged). This closes the wiring gap identified in
        # docs/incidents/2026-04-24-r47-backtest-credibility-audit.md.
        latency_profile_name = getattr(alpha.manifest, "latency_profile", "") or ""
        latency_profile: LatencyProfile | None = None
        if latency_profile_name:
            try:
                resolved = resolve_profile(latency_profile_name)
                latency_profile = LatencyProfile(
                    place_ns=int(float(resolved["submit_ack_latency_ms"]) * 1_000_000),
                    cancel_ns=int(float(resolved["cancel_ack_latency_ms"]) * 1_000_000),
                )
            except (KeyError, ValueError) as exc:
                logger.warning(
                    "maker_gate_c: latency_profile unresolved; defaulting to instant-RTT",
                    profile=latency_profile_name,
                    error=str(exc),
                )

        engine = MakerEngine(
            fill_model=fill_model,
            cost_model=cost,
            ck_source=ck_source,
            latency_profile=latency_profile,
        )

        maker_strategy = alpha.create_maker_strategy() if hasattr(alpha, "create_maker_strategy") else alpha
        result = engine.run(
            strategy=maker_strategy,
            instrument=instrument,
            pipeline_mode="strict",
        )
        ResultStore().save(result, alpha_id)

        # --- Maker Gate C: evaluate using maker_scorecard + gate_thresholds ---
        maker_thresholds = _load_maker_thresholds(root)

        scorecard_data = result.maker_scorecard or {}
        n_days = scorecard_data.get("n_days", 0)
        winning_day_pct = scorecard_data.get("winning_day_pct", 0)
        pnl_per_fill = scorecard_data.get("pnl_per_fill", 0)
        total_fills = scorecard_data.get("total_fills", 0)

        # Gate C maker checks
        maker_checks = {
            "sharpe_is": result.sharpe_is >= maker_thresholds.get("sharpe_is_min", 0.5),
            "winning_day_pct": winning_day_pct >= maker_thresholds.get("winning_day_pct_min", 55),
            "pnl_per_fill": pnl_per_fill >= maker_thresholds.get("pnl_per_fill_min_pts", 0),
            "max_drawdown": result.max_drawdown <= maker_thresholds.get("max_drawdown_pct", 30) / 100,
            "has_fills": total_fills > 0,
        }

        # --- Sub-gates (Plan C Task C10 + Slice A profile blocking) ---
        daily_pnl = getattr(result, "daily_pnl", None)
        maker_sub_gates, maker_blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload={
                "run_id": result.run_id,
                "config_hash": result.config_hash,
                "instrument": instrument,
                "strategy_name": alpha_id,
                "engine": "maker_engine",
                "queue_model": f"QueueDepletionFill(qf={qf})",
                "calibration_profile_id": "uncalibrated",
                "data_source": "clickhouse_direct",
                "latency_profile": getattr(result, "latency_profile", ""),
                "pnl_pts": _daily_pnl_points(daily_pnl),
                "n_fills": int(total_fills),
                "n_trading_days": int(n_days),
                "equity_curve": getattr(result, "equity_curve", None),
                "pnl_per_fill": float(pnl_per_fill) if pnl_per_fill is not None else None,
                "adverse_fill_pct": float(scorecard_data.get("adverse_fill_pct", 0)),
                "fill_rate_per_day": (float(total_fills) / max(float(n_days), 1.0)),
                "daily_pnl": _daily_pnl_sequence(daily_pnl),
            },
            thresholds=(
                config.profile.thresholds_for(strategy_type="maker") | maker_thresholds
                if config.profile is not None
                else maker_thresholds
            ),
            calibration_profile=None,
            profile=config.profile,
        )

        maker_passed = all(maker_checks.values()) and (maker_blocking is None or maker_blocking["passed"])

        # Compute scorecard (reuse existing function with maker data)
        from research.registry.scorecard import compute_scorecard

        tracker = ExperimentTracker(base_dir=experiments_base)
        latest_signals = getattr(tracker, "latest_signals_by_alpha", None)
        pool_signals = latest_signals() if callable(latest_signals) else {}
        pool_signals = {k: v for k, v in dict(pool_signals).items() if str(k) != str(alpha_id)}
        data_meta_path = _resolve_first_data_meta_path(resolved_data_paths)
        scorecard = compute_scorecard(
            {
                "signals": result.signals,
                "sharpe_is": result.sharpe_is,
                "sharpe_oos": result.sharpe_oos,
                "ic_mean": result.ic_mean,
                "ic_std": result.ic_std,
                "turnover": result.turnover,
                "max_drawdown": result.max_drawdown,
                "regime_metrics": result.regime_metrics,
                "capacity_estimate": result.capacity_estimate,
                "latency_profile": result.latency_profile,
            },
            pool_signals=pool_signals,
            data_meta_path=data_meta_path,
        )
        scorecard_path = experiments_base / "runs" / result.run_id / "scorecard.json"

        report = GateReport(
            gate="Gate C",
            passed=maker_passed,
            details={
                "run_id": result.run_id,
                "config_hash": result.config_hash,
                "engine_type": "maker",
                "fill_model": result.fill_model,
                "cost_model": result.cost_model,
                "instrument": instrument,
                "sharpe_is": result.sharpe_is,
                "sharpe_oos": result.sharpe_oos,
                "max_drawdown": result.max_drawdown,
                "maker_scorecard": scorecard_data,
                "per_spread_breakdown": result.per_spread_breakdown,
                "daily_pnl": result.daily_pnl,
                "maker_checks": maker_checks,
                "maker_thresholds": maker_thresholds,
                "scorecard_path": str(scorecard_path),
                "sub_gates_advisory": maker_sub_gates,
                "sub_gates_blocking": maker_blocking,
                "note": (
                    "Maker Gate C: IC/optimize/walk-forward/stress tests skipped (not applicable to maker strategies)"
                ),
            },
        )
        meta_path = tracker.log_run(
            run_id=result.run_id,
            alpha_id=alpha_id,
            config_hash=result.config_hash,
            data_paths=resolved_data_paths,
            metrics={
                "sharpe_is": float(result.sharpe_is),
                "sharpe_oos": float(result.sharpe_oos),
                "max_drawdown": float(result.max_drawdown),
                "maker_pnl_per_fill": float(pnl_per_fill),
                "maker_winning_day_pct": float(winning_day_pct),
                "maker_total_fills": float(total_fills),
            },
            gate_status={"gate_c": bool(maker_passed)},
            scorecard_payload=scorecard.to_dict(),
            backtest_report_payload=asdict(report),
            signals=result.signals,
            equity=result.equity_curve,
        )
        report.details["experiment_meta_path"] = str(meta_path)
        return report, result.run_id, result.config_hash, str(scorecard_path), str(meta_path)
    else:
        # --- Taker path: existing hft_native_runner (unchanged logic) ---
        backtest_cfg = BacktestConfig(
            data_paths=resolved_data_paths,
            is_oos_split=float(config.is_oos_split),
            signal_threshold=float(config.signal_threshold),
            max_position=int(config.max_position),
            maker_fee_bps=float(config.maker_fee_bps),
            taker_fee_bps=float(config.taker_fee_bps),
            sell_tax_bps=float(config.sell_tax_bps),
            latency_profile_id=str(config.latency_profile_id),
            local_decision_pipeline_latency_us=int(config.local_decision_pipeline_latency_us),
            submit_ack_latency_ms=float(config.submit_ack_latency_ms),
            modify_ack_latency_ms=float(config.modify_ack_latency_ms),
            cancel_ack_latency_ms=float(config.cancel_ack_latency_ms),
            live_uplift_factor=float(config.live_uplift_factor),
            backtest_engine=str(config.backtest_engine),
            queue_model=str(config.queue_model),
            latency_model=str(config.latency_model),
            exchange_model=str(config.exchange_model),
            min_queue_survival_rate=float(config.min_queue_survival_rate),
        )
        backtest_engine_key = str(config.backtest_engine).lower()
        if backtest_engine_key == "research":
            raise ValueError("backtest_engine='research' 已於 v1.1 移除。請使用 'hftbacktest_v2'。")
        for dp in resolved_data_paths:
            ensure_hftbt_npz(dp)  # auto-convert research.npy → hftbt.npz; idempotent
        runner: Any = HftNativeRunner(alpha, backtest_cfg)
        base_result = runner.run()

        # Enrich taker result with provenance (only if result is a dataclass instance)
        import dataclasses as _dc

        from research.backtest.result_store import ResultStore
        from research.backtest.taker_engine import TakerEngine

        if _dc.is_dataclass(base_result) and not isinstance(base_result, type):
            data_period = ""
            if resolved_data_paths:
                from pathlib import Path as _Path

                data_period = ",".join(str(_Path(p).stem) for p in resolved_data_paths)
            base_result = TakerEngine().enrich_result(
                base_result,
                instrument=instrument,
                data_period=data_period,
                pipeline_mode="strict",
            )
            ResultStore().save(base_result, alpha_id)
    _runner_cls = type(runner)
    optimization_eval = _optimize_parameters(
        alpha=alpha,
        base_cfg=backtest_cfg,
        base_result=base_result,
        config=config,
        runner_cls=_runner_cls,
    )
    optimization_gate_passed = bool(optimization_eval.get("passed", True))

    selected_cfg = backtest_cfg
    selected_threshold = optimization_eval.get("selected_signal_threshold")
    if selected_threshold is not None:
        try:
            threshold_val = float(selected_threshold)
            if np.isfinite(threshold_val):
                threshold_val = max(1e-6, threshold_val)
                selected_cfg = replace(backtest_cfg, signal_threshold=threshold_val)
        except (TypeError, ValueError):
            selected_cfg = backtest_cfg

    if selected_cfg.signal_threshold == backtest_cfg.signal_threshold:
        result = base_result
    else:
        runner = _runner_cls(alpha, selected_cfg)
        result = runner.run()

    oos_returns = _compute_oos_returns(result.equity_curve, config.is_oos_split)
    stat_tests = _evaluate_oos_statistical_tests(
        oos_returns,
        pvalue_threshold=float(config.stat_pvalue_threshold),
        min_tests_pass=int(config.min_stat_tests_pass),
        bootstrap_samples=int(config.bootstrap_samples),
    )
    raw_pvalues = _extract_stat_test_pvalues(stat_tests)
    correction_method = str(config.stat_correction_method).strip().lower()
    n_tests = len(raw_pvalues)
    if correction_method == "bh":
        bh_rejected, bh_adj_pvals = _bh_correction(raw_pvalues, float(config.stat_pvalue_threshold))
    elif correction_method == "bonferroni":
        bonf_alpha = float(config.stat_pvalue_threshold) / max(1, n_tests)
        bh_rejected = [float(p) <= bonf_alpha for p in raw_pvalues]
        bh_adj_pvals = [min(float(p) * max(1, n_tests), 1.0) for p in raw_pvalues]
    else:
        correction_method = "none"
        bh_rejected = [float(p) <= float(config.stat_pvalue_threshold) for p in raw_pvalues]
        bh_adj_pvals = [float(p) for p in raw_pvalues]
    n_bh_survived = int(sum(1 for flag in bh_rejected if flag))
    required_bh_pass = int(config.min_stat_tests_bh_pass)
    if correction_method == "none":
        required_bh_pass = max(required_bh_pass, int(config.min_stat_tests_pass))
    stat_gate_passed = n_bh_survived >= required_bh_pass

    wf_result: Any | None = None
    wf_gate_passed = True
    if bool(config.enable_walk_forward):
        wf_cfg = WalkForwardConfig(n_splits=int(config.wf_n_splits))
        wf_result = runner.run_walk_forward(alpha, wf_cfg)
        wf_gate_passed = bool(
            np.isfinite(float(wf_result.fold_consistency_pct))
            and np.isfinite(float(wf_result.fold_sharpe_min))
            and float(wf_result.fold_consistency_pct) >= float(config.wf_min_fold_consistency)
            and float(wf_result.fold_sharpe_min) >= float(config.wf_min_fold_sharpe_min)
        )

    stress_eval = _evaluate_stress_backtest(
        alpha=alpha,
        base_cfg=selected_cfg,
        base_result=result,
        config=config,
        runner_cls=_runner_cls,
    )
    robustness_eval = _evaluate_parameter_robustness(
        alpha=alpha,
        base_cfg=selected_cfg,
        base_result=result,
        runner_cls=_runner_cls,
    )
    # Trend contamination check (detrended IC)
    _mid = getattr(result, "mid_prices", None)
    if _mid is not None and hasattr(_mid, "size") and _mid.size > 0:
        trend_check = _evaluate_trend_contamination(
            signals=result.signals,
            mid_prices=_mid,
        )
    else:
        trend_check = {"passed": True, "detail": "mid_prices_unavailable (skipped)"}
    trend_gate_passed = bool(trend_check.get("passed", True))
    scorecard_extra = {
        "walk_forward_sharpe_mean": (float(wf_result.fold_sharpe_mean) if wf_result is not None else None),
        "walk_forward_sharpe_std": (float(wf_result.fold_sharpe_std) if wf_result is not None else None),
        "walk_forward_sharpe_min": (float(wf_result.fold_sharpe_min) if wf_result is not None else None),
        "walk_forward_consistency_pct": (float(wf_result.fold_consistency_pct) if wf_result is not None else None),
        "stat_bh_n_survived": int(n_bh_survived),
        "stat_bh_method": correction_method,
        "stat_bds_pvalue": _extract_bds_pvalue(stat_tests),
    }
    tracker = ExperimentTracker(base_dir=experiments_base)
    latest_signals = getattr(tracker, "latest_signals_by_alpha", None)
    pool_signals = latest_signals() if callable(latest_signals) else {}
    pool_signals = {k: v for k, v in dict(pool_signals).items() if str(k) != str(alpha_id)}
    data_meta_path = _resolve_first_data_meta_path(resolved_data_paths)
    scorecard = compute_scorecard(
        {
            "signals": result.signals,
            "sharpe_is": result.sharpe_is,
            "sharpe_oos": result.sharpe_oos,
            "ic_mean": result.ic_mean,
            "ic_std": result.ic_std,
            "turnover": result.turnover,
            "max_drawdown": result.max_drawdown,
            "regime_metrics": result.regime_metrics,
            "capacity_estimate": result.capacity_estimate,
            "latency_profile": result.latency_profile,
        },
        pool_signals=pool_signals,
        wf_extra=scorecard_extra,
        data_meta_path=data_meta_path,
    )
    scorecard_data_ul = int(scorecard.data_ul) if scorecard.data_ul is not None else None
    gate_c_data_ul_advisory = {
        "value": scorecard_data_ul,
        "recommended_min": 3,
        "warn": (scorecard_data_ul is None or scorecard_data_ul < 3),
        "blocking": False,
        "detail": (
            "OK"
            if scorecard_data_ul is not None and scorecard_data_ul >= 3
            else "VM-UL<3: Gate C recommends UL3+ metadata for stronger reproducibility."
        ),
    }
    scorecard_path = experiments_base / "runs" / result.run_id / "scorecard.json"

    core_passed = (
        (result.sharpe_oos >= config.min_sharpe_oos)
        and (result.max_drawdown >= -abs(config.max_abs_drawdown))
        and (result.turnover >= config.min_turnover)
    )

    # --- Sub-gates (Plan C Task C10 + Slice A profile blocking) ---
    # Compute daily_pnl from equity_curve (cumulative PnL -> diff)
    _eq = getattr(result, "equity_curve", None)
    _daily_pnl = _equity_to_daily_pnl(_eq)

    taker_sub_gates, taker_blocking = _invoke_sub_gates(
        strategy_type="taker",
        result_payload={
            "run_id": result.run_id,
            "config_hash": result.config_hash,
            "instrument": instrument,
            "strategy_name": alpha_id,
            "engine": "hftbacktest_v2",
            "queue_model": str(selected_cfg.queue_model),
            "calibration_profile_id": "uncalibrated",
            "data_source": "hftbt_npz",
            "latency_profile": getattr(result, "latency_profile", ""),
            "pnl_pts": (
                float(result.equity_curve[-1] - result.equity_curve[0]) if _eq is not None and len(_eq) > 1 else 0.0
            ),
            "n_fills": (int(len(result.signals)) if hasattr(result, "signals") and result.signals is not None else 0),
            "n_trading_days": int(len(_daily_pnl)),
            "equity_curve": _eq,
            "ic_is": float(result.ic_mean) if result.ic_mean is not None else None,
            "ic_oos": None,
            "daily_pnl": _daily_pnl,
        },
        thresholds=(
            (
                config.profile.thresholds_for(strategy_type="taker")
                | {
                    "sharpe_is_min": float(config.min_sharpe_oos),
                    "max_drawdown_pct": float(abs(config.max_abs_drawdown)) * 100,
                    "winning_day_pct_min": 55.0,
                    "ic_is_min": 0.03,
                    "ic_oos_min": 0.02,
                }
            )
            if config.profile is not None
            else {
                "sharpe_is_min": float(config.min_sharpe_oos),
                "max_drawdown_pct": float(abs(config.max_abs_drawdown)) * 100,
                "winning_day_pct_min": 55.0,
                "ic_is_min": 0.03,
                "ic_oos_min": 0.02,
            }
        ),
        calibration_profile=None,
        profile=config.profile,
    )

    passed = (
        core_passed
        and bool(stat_gate_passed)
        and bool(wf_gate_passed)
        and bool(optimization_gate_passed)
        and bool(stress_eval.get("passed"))
        and bool(robustness_eval.get("passed"))
        and bool(trend_gate_passed)
        and (taker_blocking is None or taker_blocking["passed"])
    )

    report = GateReport(
        gate="Gate C",
        passed=passed,
        details={
            "run_id": result.run_id,
            "config_hash": result.config_hash,
            "sharpe_is": result.sharpe_is,
            "sharpe_oos": result.sharpe_oos,
            "ic_mean": result.ic_mean,
            "ic_std": result.ic_std,
            "turnover": result.turnover,
            "max_drawdown": result.max_drawdown,
            "capacity_estimate": result.capacity_estimate,
            "regime_metrics": result.regime_metrics,
            "criteria": {
                "min_sharpe_oos": config.min_sharpe_oos,
                "max_abs_drawdown": config.max_abs_drawdown,
                "min_turnover": config.min_turnover,
                "stat_pvalue_threshold": config.stat_pvalue_threshold,
                "min_stat_tests_pass": config.min_stat_tests_pass,
                "stat_correction_method": correction_method,
                "min_stat_tests_bh_pass": required_bh_pass,
                "enable_walk_forward": bool(config.enable_walk_forward),
                "wf_n_splits": int(config.wf_n_splits),
                "wf_min_fold_consistency": float(config.wf_min_fold_consistency),
                "wf_min_fold_sharpe_min": float(config.wf_min_fold_sharpe_min),
                "enable_param_optimization": bool(config.enable_param_optimization),
                "opt_signal_threshold_min": float(config.opt_signal_threshold_min),
                "opt_signal_threshold_max": float(config.opt_signal_threshold_max),
                "opt_signal_threshold_steps": int(config.opt_signal_threshold_steps),
                "opt_objective": str(config.opt_objective),
                "min_stress_sharpe_ratio": config.min_stress_sharpe_ratio,
                "stress_drawdown_limit_multiplier": config.stress_drawdown_limit_multiplier,
            },
            "core_metrics_passed": core_passed,
            "stat_gate_passed": stat_gate_passed,
            "walk_forward_gate_passed": wf_gate_passed,
            "optimization_gate_passed": optimization_gate_passed,
            "trend_gate_passed": trend_gate_passed,
            "statistical_tests": stat_tests,
            "multiple_testing": {
                "method": correction_method,
                "raw_pvalues": raw_pvalues,
                "adjusted_pvalues": bh_adj_pvals,
                "rejected": bh_rejected,
                "n_survived": n_bh_survived,
                "required": required_bh_pass,
            },
            "walk_forward": (
                {
                    "n_splits": int(wf_result.config.n_splits),
                    "n_folds": len(wf_result.folds),
                    "fold_consistency_pct": float(wf_result.fold_consistency_pct),
                    "fold_sharpe_mean": float(wf_result.fold_sharpe_mean),
                    "fold_sharpe_std": float(wf_result.fold_sharpe_std),
                    "fold_sharpe_min": float(wf_result.fold_sharpe_min),
                    "fold_sharpe_max": float(wf_result.fold_sharpe_max),
                    "fold_ic_mean": float(wf_result.fold_ic_mean),
                }
                if wf_result is not None
                else {"skipped": True, "reason": "enable_walk_forward=false"}
            ),
            "parameter_optimization": optimization_eval,
            "stress_backtest": stress_eval,
            "parameter_robustness": robustness_eval,
            "trend_contamination": trend_check,
            "sub_gates_advisory": taker_sub_gates,
            "sub_gates_blocking": taker_blocking,
            "latency_profile": result.latency_profile,
            "scorecard_path": str(scorecard_path),
            "scorecard_data_meta_path": data_meta_path,
            "data_ul_advisory": gate_c_data_ul_advisory,
            "selected_signal_threshold": float(selected_cfg.signal_threshold),
            "base_signal_threshold": float(backtest_cfg.signal_threshold),
        },
    )
    meta_path = tracker.log_run(
        run_id=result.run_id,
        alpha_id=alpha_id,
        config_hash=result.config_hash,
        data_paths=resolved_data_paths,
        metrics={
            "sharpe_is": float(result.sharpe_is),
            "sharpe_oos": float(result.sharpe_oos),
            "ic_mean": float(result.ic_mean),
            "ic_std": float(result.ic_std),
            "turnover": float(result.turnover),
            "max_drawdown": float(result.max_drawdown),
            "capacity_estimate": float(result.capacity_estimate),
            "latency_model_applied": float(bool(result.latency_profile.get("model_applied", False))),
            "stat_tests_passed": float(bool(stat_tests.get("passed"))),
            "stat_bh_n_survived": float(n_bh_survived),
            "walk_forward_gate_passed": float(bool(wf_gate_passed)),
            "walk_forward_consistency_pct": (
                float(wf_result.fold_consistency_pct) if wf_result is not None else float("nan")
            ),
            "param_optimization_passed": float(bool(optimization_gate_passed)),
            "selected_signal_threshold": float(selected_cfg.signal_threshold),
            "stress_test_passed": float(bool(stress_eval.get("passed"))),
            "param_robustness_passed": float(bool(robustness_eval.get("passed"))),
            "trend_gate_passed": float(bool(trend_gate_passed)),
        },
        gate_status={"gate_c": bool(passed)},
        scorecard_payload=scorecard.to_dict(),
        backtest_report_payload=asdict(report),
        signals=result.signals,
        equity=result.equity_curve,
    )
    report.details["experiment_meta_path"] = str(meta_path)
    return report, result.run_id, result.config_hash, str(scorecard_path), str(meta_path)
