from __future__ import annotations

import os
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
from hft_platform.alpha.gate_c_runtime import GateCRuntime
from hft_platform.alpha.outcomes import Outcome, Verdict, VerdictResult, check_entry, derive_verdict

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
    core_checks: list[dict] | None = None,
) -> tuple[list[dict], dict | None]:
    """Invoke all applicable sub-gates and compute blocking aggregate.

    Returns:
        advisory: list of dicts, one per registered applicable gate, each with
            an ``outcome`` (see ``alpha.outcomes``).
        blocking: None if `profile is None`; otherwise
            ``{"passed", "failing", "names", "profile", "triage_status",
            "triage_reasons", "verdict"}``. ``passed`` is true only for a PASS
            verdict over the profile's blocking sub-gates plus ``core_checks``.
    """
    advisory, blocking, _ = _invoke_sub_gates_with_verdict(
        strategy_type=strategy_type,
        result_payload=result_payload,
        thresholds=thresholds,
        calibration_profile=calibration_profile,
        profile=profile,
        core_checks=core_checks,
    )
    return advisory, blocking


def _invoke_sub_gates_with_verdict(  # noqa: C901 - branchy dispatch; refactor tracked as follow-up
    *,
    strategy_type: str,
    result_payload: dict,
    thresholds: dict,
    calibration_profile: Any | None = None,
    profile: Any | None = None,
    core_checks: list[dict] | None = None,
) -> tuple[list[dict], dict | None, VerdictResult]:
    """``_invoke_sub_gates`` plus the run's verdict.

    This is the one place Gate C calls ``derive_verdict``. ``core_checks`` are
    Gate C's own checks (``check_entry`` dicts); each is blocking unless it
    carries ``"blocking": False`` (a check the run's config switched off).
    Sub-gates block only when the profile lists them. Without a profile the
    verdict covers ``core_checks`` alone, as Gate C's ``passed`` always has.
    """
    import numpy as np

    from hft_platform.alpha._sub_gates import (
        UnavailableDailyPnl,
        ensure_builtin_sub_gates_registered,
        get_registered_sub_gates,
    )
    from hft_platform.alpha._sub_gates.maker import FillRateValidationGate
    from hft_platform.backtest.result import BacktestResult

    ensure_builtin_sub_gates_registered()

    daily_unavailable = result_payload.get("daily_pnl_unavailable")
    daily_pnl: Any = (
        UnavailableDailyPnl(str(daily_unavailable))
        if daily_unavailable
        else list(result_payload.get("daily_pnl") or [])
    )
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
        daily_pnl=daily_pnl,
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

    # Punch-list (2026-05-29): expose strict-profile state to sub-gates via the
    # thresholds dict so they can fail-closed on missing evidence / legacy
    # scorecard shape / insufficient sample.  Loose-profile semantics
    # (advisory PASS) are preserved when the flag is False.
    is_strict = bool(getattr(profile, "is_strict", False))
    eval_thresholds: dict = {**thresholds, "_is_strict_profile": is_strict}

    advisory: list[dict] = []
    blocking_seen: list[str] = []

    for gate in get_registered_sub_gates():
        if strategy_type not in gate.applies_to:
            continue
        try:
            if isinstance(gate, FillRateValidationGate):
                sub = gate.evaluate(
                    result,
                    config=None,
                    thresholds=eval_thresholds,
                    profile=calibration_profile,
                )
            else:
                sub = gate.evaluate(result, config=None, thresholds=eval_thresholds)
            entry: dict[str, Any] = {
                "name": sub.name,
                "passed": sub.passed,
                "outcome": str(sub.outcome),
                "metrics": sub.metrics,
                "details": sub.details,
            }
        except Exception as exc:  # noqa: BLE001
            entry = {
                "name": str(getattr(gate, "name", "unknown")),
                "passed": None,
                "outcome": Outcome.ERROR.value,
                "metrics": {},
                "details": f"sub-gate error: {exc!r}",
                "error": True,
            }
        advisory.append(entry)
        if profile is not None and entry["name"] in blocking_names:
            blocking_seen.append(str(entry["name"]))

    core = list(core_checks or [])
    core_blocking = {str(c["name"]) for c in core if c.get("blocking", True)}
    verdict = derive_verdict(
        [*advisory, *core],
        blocking=(blocking_names if profile is not None else set()) | core_blocking,
        deferrable=dict(getattr(profile, "deferrable", {}) or {}),
    )

    blocking: dict | None
    if profile is None:
        blocking = None
    else:
        failing_names = set(verdict.failing)
        blocking_failing = [e for e in [*advisory, *core] if e["name"] in failing_names]
        triage_status, triage_reasons = _derive_triage_status(verdict, advisory)
        blocking = {
            "passed": verdict.passed,
            "failing": blocking_failing,
            "names": blocking_seen,
            "profile": profile.name,
            # Goal §4: sub-threshold runs must NOT be collapsed into a
            # generic KILL; triage_status is read off the verdict.
            "triage_status": triage_status,
            "triage_reasons": triage_reasons,
            "verdict": verdict.to_dict(),
        }

    # Goal §4 / §9 traceability: when the audit flag is on, emit one JSONL
    # row per invocation so every Gate-C decision is replayable later.
    # Failure of the writer must never break the validation path —
    # swallow all exceptions and log.
    if os.getenv("HFT_SUB_GATE_AUDIT_ENABLED", "0") == "1":
        try:
            from hft_platform.alpha import sub_gate_audit

            run_id = str(result_payload.get("run_id") or "")
            if run_id:
                prov = result_payload.get("spec_provenance")
                if not isinstance(prov, dict):
                    prov = None
                sub_gate_audit.record_sub_gate_run(
                    run_id=run_id,
                    strategy_name=str(result_payload.get("strategy_name") or ""),
                    instrument=str(result_payload.get("instrument") or ""),
                    strategy_type=strategy_type,
                    profile_name=(profile.name if profile is not None else ""),
                    advisory=advisory,
                    blocking=blocking,
                    spec_provenance=prov,
                )
        except Exception:  # noqa: BLE001
            logger.warning("sub_gate_audit emit failed", exc_info=True)

    return advisory, blocking, verdict


# Sub-gate results Gate D re-reads from the persisted scorecard.
_GATE_D_AUDIT_SUB_GATES = ("inventory_mtm", "cost_uncertainty")


def _scorecard_with_audits(scorecard_payload: dict, *, strategy_type: str, sub_gates: list[dict]) -> dict:
    """The persisted scorecard plus what Gate D audits: strategy type and audit sub-gates.

    ``compute_scorecard`` does not carry these, so without them Gate D could
    only report its audits as missing (ERROR) for every generated scorecard.
    """
    payload = {**scorecard_payload, "strategy_type": strategy_type}
    for entry in sub_gates:
        name = str(entry.get("name", ""))
        if name in _GATE_D_AUDIT_SUB_GATES:
            payload[name] = {
                "passed": entry.get("passed"),
                "outcome": entry.get("outcome"),
                "metrics": dict(entry.get("metrics") or {}),
                "details": entry.get("details", ""),
            }
    return payload


_SAMPLE_LABELS = {"promising", "needs_more_sample", "inconclusive"}


def _derive_triage_status(verdict: VerdictResult, advisory: list[dict]) -> tuple[str, list[str]]:
    """Name the verdict in the legacy triage vocabulary.

    PASS -> "passed"; KILL -> "killed"; INVALID -> "invalid"; NEEDS-MORE-DAYS
    and INCONCLUSIVE -> "sample_<label>", using ``min_sample_size``'s label
    when it has one that fits. Errors are never reported as killed.
    """
    reasons = list(verdict.failing)
    if verdict.verdict is Verdict.PASS:
        return "passed", []
    if verdict.verdict is Verdict.KILL:
        return "killed", reasons
    if verdict.verdict is Verdict.INVALID:
        return "invalid", reasons
    label = next(
        ((e.get("metrics") or {}).get("sample_adequacy_label") for e in advisory if e.get("name") == "min_sample_size"),
        None,
    )
    if verdict.verdict is Verdict.NEEDS_MORE_DAYS:
        if label not in ("promising", "needs_more_sample"):
            label = "needs_more_sample"
    else:
        label = "inconclusive"
    return f"sample_{label}", reasons


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


# Threshold keys whose direction is known: for these the stricter of the run's
# value and the profile's wins, so a profile can only tighten a gate.
_HIGHER_IS_STRICTER = frozenset(
    {
        "sharpe_is_min",
        "sharpe_oos_min",
        "winning_day_pct_min",
        "pnl_per_fill_min_pts",
        "min_fills",
        "min_days",
        "min_oos_trades",
        "min_oos_days",
        "ic_is_min",
        "ic_oos_min",
        "edge_to_cost_ratio_min",
        "deflated_sharpe_min",
        "mean_net_edge_pts_per_trade_min",
        "replay_parity_match_pct_min",
        "cost_uncertainty_p95_lower_bound_min_pts",
        "monthly_distribution_min_months",
        "bootstrap_ci_lower_bound_min",
        "block_bootstrap_ci_lower_bound_min",
    }
)
_LOWER_IS_STRICTER = frozenset(
    {
        "max_drawdown_pct",
        "adverse_fill_pct_max",
        "fill_rate_deviation_max",
        "outlier_day_contribution_max_pct",
        "top_month_contribution_max_pct",
        "drawdown_to_avg_monthly_max_ratio",
        "top_trade_share_max_pct",
        "worst_loss_share_max_pct",
        "force_flat_trip_share_max_pct",
    }
)


def _merge_profile_thresholds(base: dict, profile: Any | None, *, strategy_type: str) -> dict:
    """Run thresholds with the profile's applied on top; the profile can only tighten.

    The profile is the authority for every key it sets. For keys whose
    direction is known the stricter of the two values is kept, so neither a
    loose run default nor a loose profile value can relax a gate.
    """
    if profile is None:
        return dict(base)
    merged = dict(base)
    for key, value in profile.thresholds_for(strategy_type=strategy_type).items():
        current = merged.get(key)
        numeric = isinstance(value, (int, float)) and isinstance(current, (int, float))
        if numeric and not isinstance(value, bool) and not isinstance(current, bool):
            if key in _HIGHER_IS_STRICTER:
                value = max(current, value)
            elif key in _LOWER_IS_STRICTER:
                value = min(current, value)
        merged[key] = value
    return merged


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


def _load_gate_c_runtime() -> GateCRuntime:
    """Load the research adapter during the compatibility migration window."""
    from research.backtest.gate_c_runtime import build_gate_c_runtime

    return build_gate_c_runtime()


def _resolve_gate_c_runtime(runtime: GateCRuntime | None) -> GateCRuntime:
    if runtime is not None:
        return runtime
    return _load_gate_c_runtime()


def _threshold_check(name: str, value: Any, threshold: float, *, at_least: bool) -> dict:
    """PASS/FAIL against a threshold; a missing or non-finite value is an ERROR."""
    try:
        val = float(value)
    except (TypeError, ValueError):
        val = float("nan")
    metrics = {"value": val if np.isfinite(val) else None, "threshold": float(threshold)}
    if not np.isfinite(val):
        return check_entry(name, Outcome.ERROR, metrics=metrics, details=f"{name}: value missing or not finite")
    ok = val >= threshold if at_least else val <= threshold
    op = ">=" if at_least else "<="
    return check_entry(
        name, Outcome.PASS if ok else Outcome.FAIL, metrics=metrics, details=f"{val:.4g} {op} {threshold:.4g}"
    )


def _maker_core_checks(
    *,
    sharpe_is: Any,
    winning_day_pct: Any,
    pnl_per_fill: Any,
    max_drawdown: Any,
    total_fills: Any,
    thresholds: dict,
) -> list[dict]:
    """Gate C's own maker checks, one outcome each (thresholds unchanged)."""
    n_fills = int(total_fills or 0)
    if n_fills > 0:
        has_fills = check_entry("gate_c.has_fills", Outcome.PASS, metrics={"total_fills": float(n_fills)})
    else:
        has_fills = check_entry(
            "gate_c.has_fills",
            Outcome.INSUFFICIENT_SAMPLE,
            metrics={"total_fills": 0.0, "required_n": None, "reachable": False},
            details="no fills: nothing to measure",
        )
    return [
        _threshold_check("gate_c.sharpe_is", sharpe_is, thresholds.get("sharpe_is_min", 0.5), at_least=True),
        _threshold_check(
            "gate_c.winning_day_pct", winning_day_pct, thresholds.get("winning_day_pct_min", 55), at_least=True
        ),
        _threshold_check("gate_c.pnl_per_fill", pnl_per_fill, thresholds.get("pnl_per_fill_min_pts", 0), at_least=True),
        _threshold_check(
            "gate_c.max_drawdown", max_drawdown, thresholds.get("max_drawdown_pct", 30) / 100, at_least=False
        ),
        has_fills,
    ]


def _dict_check(name: str, payload: Any) -> dict:
    """Outcome for an evaluator dict carrying ``passed`` (and maybe ``outcome``).

    A dict without ``passed`` is an ERROR, never a default PASS.
    """
    if not isinstance(payload, dict) or "passed" not in payload:
        return check_entry(name, Outcome.ERROR, details=f"{name}: evaluator returned no result")
    raw = payload.get("outcome")
    outcome = Outcome(raw) if raw else (Outcome.PASS if payload.get("passed") else Outcome.FAIL)
    detail = str(payload.get("detail") or payload.get("reason") or "")
    metrics: dict[str, Any] = {}
    if outcome is Outcome.INSUFFICIENT_SAMPLE:
        metrics["required_n"] = None
    return check_entry(name, outcome, metrics=metrics, details=detail)


def _optimization_check(optimization_eval: Any) -> dict:
    if isinstance(optimization_eval, dict) and optimization_eval.get("enabled") is False:
        return check_entry(
            "gate_c.parameter_optimization", Outcome.NOT_APPLICABLE, details="enable_param_optimization=false"
        ) | {"blocking": False}
    return _dict_check("gate_c.parameter_optimization", optimization_eval)


def _walk_forward_check(wf_result: Any, config: ValidationConfig) -> dict:
    consistency = float(wf_result.fold_consistency_pct)
    sharpe_min = float(wf_result.fold_sharpe_min)
    metrics = {
        "fold_consistency_pct": consistency if np.isfinite(consistency) else None,
        "fold_sharpe_min": sharpe_min if np.isfinite(sharpe_min) else None,
    }
    if not (np.isfinite(consistency) and np.isfinite(sharpe_min)):
        return check_entry(
            "gate_c.walk_forward", Outcome.ERROR, metrics=metrics, details="walk-forward metrics not finite"
        )
    ok = consistency >= float(config.wf_min_fold_consistency) and sharpe_min >= float(config.wf_min_fold_sharpe_min)
    return check_entry("gate_c.walk_forward", Outcome.PASS if ok else Outcome.FAIL, metrics=metrics)


def _dated_daily_rows(daily_pnl: Any) -> list[dict]:
    """Rows of ``daily_pnl`` that name their trading date; anything else is dropped."""
    if not isinstance(daily_pnl, list | tuple):
        return []
    rows = [r for r in daily_pnl if isinstance(r, dict) and isinstance(r.get("date"), str) and r["date"]]
    return rows if len(rows) == len(daily_pnl) else []


def _maker_protocol_error_report(
    alpha_id: str, strategy: Any, missing: list[str]
) -> tuple[GateReport, str, str, str, str]:
    """Gate C report for a maker alpha that does not implement MakerStrategy."""
    detail = f"{type(strategy).__name__} lacks MakerStrategy method(s): {', '.join(missing)}"
    entry = check_entry("gate_c.maker_protocol", Outcome.ERROR, details=detail)
    verdict = derive_verdict([entry], blocking={entry["name"]})
    logger.warning("maker_gate_c: strategy protocol mismatch", alpha_id=alpha_id, missing=missing)
    report = GateReport(
        gate="Gate C",
        passed=False,
        details={
            "engine_type": "maker",
            "error": detail,
            "gate_c_checks": [entry],
            "verdict": verdict.to_dict(),
        },
    )
    return report, "", "", "", ""


def run_gate_c(  # noqa: C901 - existing complexity 17; refactor tracked as follow-up
    alpha: Any,
    config: ValidationConfig,
    root: Path,
    resolved_data_paths: list[str],
    experiments_base: Path,
    runtime: GateCRuntime | None = None,
) -> tuple[GateReport, str, str, str, str]:
    _ensure_project_root_on_path(root)
    from hft_platform.alpha.experiments import ExperimentTracker

    runtime = _resolve_gate_c_runtime(runtime)
    HftNativeRunner = runtime.hft_native_runner
    ensure_hftbt_npz = runtime.ensure_hftbt_npz
    BacktestConfig = runtime.backtest_config
    WalkForwardConfig = runtime.walk_forward_config
    compute_scorecard = runtime.compute_scorecard

    alpha_id = alpha.manifest.alpha_id
    strategy_type = getattr(alpha.manifest, "strategy_type", "taker")
    instrument = getattr(alpha.manifest, "instrument", "")

    if strategy_type == "maker":
        # --- Maker path: CK-direct backtest ---
        from hft_platform.alpha.latency_profiles import resolve_profile

        maker_runtime = runtime.load_maker_runtime()
        load_cost_profile = maker_runtime.load_cost_profile
        QueueDepletionFill = maker_runtime.queue_depletion_fill
        ClickHouseSource = maker_runtime.clickhouse_source
        LatencyProfile = maker_runtime.latency_profile
        MakerEngine = maker_runtime.maker_engine
        ResultStore = maker_runtime.result_store

        maker_strategy = alpha.create_maker_strategy() if hasattr(alpha, "create_maker_strategy") else alpha
        missing = [m for m in ("on_tick", "on_fill") if not callable(getattr(maker_strategy, m, None))]
        if missing:
            return _maker_protocol_error_report(alpha_id, maker_strategy, missing)

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
        latency_profile: Any | None = None
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

        result = engine.run(
            strategy=maker_strategy,
            instrument=instrument,
            pipeline_mode="strict",
        )
        ResultStore().save(result, alpha_id)

        # --- Maker Gate C: evaluate using maker_scorecard + gate_thresholds ---
        maker_thresholds = _merge_profile_thresholds(
            _load_maker_thresholds(root), config.profile, strategy_type="maker"
        )

        scorecard_data = result.maker_scorecard or {}
        n_days = scorecard_data.get("n_days", 0)
        winning_day_pct = scorecard_data.get("winning_day_pct", 0)
        pnl_per_fill = scorecard_data.get("pnl_per_fill", 0)
        total_fills = scorecard_data.get("total_fills", 0)

        # Gate C maker checks
        maker_check_entries = _maker_core_checks(
            sharpe_is=result.sharpe_is,
            winning_day_pct=winning_day_pct,
            pnl_per_fill=pnl_per_fill,
            max_drawdown=result.max_drawdown,
            total_fills=total_fills,
            thresholds=maker_thresholds,
        )
        maker_checks = {e["name"].removeprefix("gate_c."): bool(e["passed"]) for e in maker_check_entries}

        # --- Sub-gates (Plan C Task C10 + Slice A profile blocking) ---
        daily_pnl = getattr(result, "daily_pnl", None)
        maker_sub_gates, maker_blocking, maker_verdict = _invoke_sub_gates_with_verdict(
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
            thresholds=_merge_profile_thresholds(maker_thresholds, config.profile, strategy_type="maker"),
            calibration_profile=None,
            profile=config.profile,
            core_checks=maker_check_entries,
        )

        maker_passed = maker_verdict.passed

        # Compute scorecard (reuse existing function with maker data)
        tracker = ExperimentTracker(base_dir=experiments_base)
        latest_signals = getattr(tracker, "latest_signals_by_alpha", None)
        pool_signals = latest_signals() if callable(latest_signals) else {}
        pool_signals = {k: v for k, v in dict(pool_signals).items() if str(k) != str(alpha_id)}
        data_meta_path = _resolve_first_data_meta_path(resolved_data_paths)
        # MakerEngine has no OOS split and writes a constant sharpe_oos; record
        # it as not measured instead of as a zero Sharpe.
        scorecard = compute_scorecard(
            {
                "signals": result.signals,
                "sharpe_is": result.sharpe_is,
                "sharpe_oos": None,
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
                "sharpe_oos": None,
                "sharpe_oos_outcome": Outcome.NOT_APPLICABLE.value,
                "max_drawdown": result.max_drawdown,
                "maker_scorecard": scorecard_data,
                "per_spread_breakdown": result.per_spread_breakdown,
                "daily_pnl": result.daily_pnl,
                "maker_checks": maker_checks,
                "gate_c_checks": maker_check_entries,
                "verdict": maker_verdict.to_dict(),
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
                "sharpe_oos": float("nan"),
                "max_drawdown": float(result.max_drawdown),
                "maker_pnl_per_fill": float(pnl_per_fill),
                "maker_winning_day_pct": float(winning_day_pct),
                "maker_total_fills": float(total_fills),
            },
            gate_status={"gate_c": bool(maker_passed)},
            scorecard_payload=_scorecard_with_audits(
                scorecard.to_dict(), strategy_type="maker", sub_gates=maker_sub_gates
            ),
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

        taker_runtime = runtime.load_taker_runtime()
        ResultStore = taker_runtime.result_store
        TakerEngine = taker_runtime.taker_engine

        if _dc.is_dataclass(base_result) and not isinstance(base_result, type):
            data_period = ""
            if resolved_data_paths:
                from pathlib import Path as _Path

                data_period = ",".join(str(_Path(p).stem) for p in resolved_data_paths)
            # Round 40: thread the per-instrument cost profile so
            # ``BacktestResult.trade_pnl`` reflects NET per-trip PnL.
            # Missing profile (unknown instrument) falls back to gross
            # mid-to-mid trips rather than fabricating a cost — goal
            # 限制 §4.  We never default to a stand-in cost model.
            _cost_model = None
            try:
                _cost_model = taker_runtime.load_cost_profile(instrument)
            except (KeyError, FileNotFoundError):
                _cost_model = None
            base_result = TakerEngine().enrich_result(
                base_result,
                instrument=instrument,
                data_period=data_period,
                pipeline_mode="strict",
                cost_model=_cost_model,
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
    optimization_check = _optimization_check(optimization_eval)
    optimization_gate_passed = bool(optimization_check["passed"]) or not optimization_check.get("blocking", True)

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
    if stat_tests.get("reason") == "insufficient_oos_returns":
        stat_check = check_entry(
            "gate_c.stat_tests",
            Outcome.INSUFFICIENT_SAMPLE,
            metrics={"sample_count": stat_tests.get("sample_count"), "required_n": None},
            details="insufficient OOS returns for the statistical tests",
        )
    else:
        stat_check = check_entry(
            "gate_c.stat_tests",
            Outcome.PASS if stat_gate_passed else Outcome.FAIL,
            metrics={"n_survived": float(n_bh_survived), "required": float(required_bh_pass)},
            details=f"{n_bh_survived} of {n_tests} tests survive {correction_method} (need {required_bh_pass})",
        )

    wf_result: Any | None = None
    if bool(config.enable_walk_forward):
        wf_cfg = WalkForwardConfig(n_splits=int(config.wf_n_splits))
        wf_result = runner.run_walk_forward(alpha, wf_cfg)
        wf_check = _walk_forward_check(wf_result, config)
    else:
        wf_check = check_entry("gate_c.walk_forward", Outcome.NOT_APPLICABLE, details="enable_walk_forward=false") | {
            "blocking": False
        }
    # Legacy report flag: "did not block". A check the config switched off
    # reads True here; its outcome is in ``gate_c_checks``.
    wf_gate_passed = bool(wf_check["passed"]) or not wf_check.get("blocking", True)

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
        trend_check = {"passed": False, "outcome": "error", "detail": "mid_prices_unavailable"}
    trend_entry = _dict_check("gate_c.trend_contamination", trend_check)
    trend_gate_passed = bool(trend_entry["passed"])
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

    taker_thresholds = _merge_profile_thresholds(
        {
            "sharpe_is_min": float(config.min_sharpe_oos),
            "sharpe_oos_min": float(config.min_sharpe_oos),
            "max_drawdown_pct": float(abs(config.max_abs_drawdown)) * 100,
            "winning_day_pct_min": 55.0,
            "ic_is_min": 0.03,
            "ic_oos_min": 0.02,
        },
        config.profile,
        strategy_type="taker",
    )
    core_entries = [
        _threshold_check(
            "gate_c.sharpe_oos", result.sharpe_oos, float(taker_thresholds["sharpe_oos_min"]), at_least=True
        ),
        _threshold_check(
            "gate_c.max_drawdown",
            result.max_drawdown,
            -abs(float(taker_thresholds["max_drawdown_pct"])) / 100,
            at_least=True,
        ),
        _threshold_check("gate_c.turnover", result.turnover, float(config.min_turnover), at_least=True),
    ]
    core_passed = all(e["passed"] for e in core_entries)
    taker_check_entries = [
        *core_entries,
        stat_check,
        wf_check,
        optimization_check,
        _dict_check("gate_c.stress_backtest", stress_eval),
        _dict_check("gate_c.parameter_robustness", robustness_eval),
        trend_entry,
    ]

    # --- Sub-gates (Plan C Task C10 + Slice A profile blocking) ---
    # Day-axis sub-gates need one dated row per trading day. The taker runner's
    # equity curve is per tick, so differencing it is not a daily series; with
    # no dated rows those gates report ERROR instead of measuring ticks.
    _eq = getattr(result, "equity_curve", None)
    _daily_rows = _dated_daily_rows(getattr(result, "daily_pnl", None))
    _trade_pnl = getattr(result, "trade_pnl", None)
    _daily_payload: dict[str, Any] = (
        {"daily_pnl": _daily_rows}
        if _daily_rows
        else {"daily_pnl_unavailable": "taker result has no dated per-day rows (equity_curve is per tick)"}
    )
    if _trade_pnl is not None:
        _daily_payload["trade_pnl"] = list(_trade_pnl)

    taker_sub_gates, taker_blocking, taker_verdict = _invoke_sub_gates_with_verdict(
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
            "n_fills": int(len(_trade_pnl)) if _trade_pnl is not None else 0,
            "n_trading_days": len({row["date"] for row in _daily_rows}),
            "equity_curve": _eq,
            "ic_is": float(result.ic_mean) if result.ic_mean is not None else None,
            "ic_oos": None,
            **_daily_payload,
        },
        thresholds=taker_thresholds,
        calibration_profile=None,
        profile=config.profile,
        core_checks=taker_check_entries,
    )

    passed = taker_verdict.passed

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
            # Thresholds actually applied (run defaults tightened by the profile).
            "applied_thresholds": dict(taker_thresholds),
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
            "gate_c_checks": taker_check_entries,
            "verdict": taker_verdict.to_dict(),
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
        scorecard_payload=_scorecard_with_audits(scorecard.to_dict(), strategy_type="taker", sub_gates=taker_sub_gates),
        backtest_report_payload=asdict(report),
        signals=result.signals,
        equity=result.equity_curve,
    )
    report.details["experiment_meta_path"] = str(meta_path)
    return report, result.run_id, result.config_hash, str(scorecard_path), str(meta_path)
