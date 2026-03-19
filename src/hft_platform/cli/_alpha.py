"""CLI commands: alpha scaffold, search, list, validate, promote, rl-promote, pool, canary, ab-compare, experiments."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from typing import Any


def cmd_alpha_scaffold(args: argparse.Namespace) -> None:
    cmd = [sys.executable, "-m", "research.tools.alpha_scaffold", args.alpha_id, "--complexity", str(args.complexity)]
    for ref in args.paper or []:
        cmd.extend(["--paper", str(ref)])
    if args.force:
        cmd.append("--force")

    proc = subprocess.run(
        cmd,
        cwd=".",
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        if proc.stderr.strip():
            print(proc.stderr.strip())
        sys.exit(proc.returncode or 1)


def cmd_alpha_search(args: argparse.Namespace) -> None:
    mode = str(args.mode)
    if mode == "template" and not args.template:
        print("--template is required for template mode")
        sys.exit(2)

    try:
        np = import_module("numpy")
        AlphaSearchEngine = import_module("research.combinatorial.search_engine").AlphaSearchEngine
    except Exception as exc:
        print(f"Failed to import alpha search engine: {exc}")
        sys.exit(1)

    source = np.load(args.data, allow_pickle=False)
    try:
        if isinstance(source, np.lib.npyio.NpzFile):
            if "data" in source:
                arr = np.asarray(source["data"])
            else:
                first_key = source.files[0] if source.files else None
                if first_key is None:
                    raise ValueError("Empty NPZ file")
                arr = np.asarray(source[first_key])
        else:
            arr = np.asarray(source)
    finally:
        if isinstance(source, np.lib.npyio.NpzFile):
            source.close()

    field_names = [f.strip() for f in str(args.feature_fields).split(",") if f.strip()]
    if not field_names:
        print("--feature-fields is required (comma separated)")
        sys.exit(2)

    features: dict[str, Any] = {}
    if arr.dtype.names:
        for field in field_names:
            if field not in arr.dtype.names:
                print(f"Feature field not found in data: {field}")
                sys.exit(2)
            features[field] = np.asarray(arr[field], dtype=np.float64)
        returns = np.asarray(arr[args.returns_field], dtype=np.float64) if args.returns_field else None
    else:
        for i, field in enumerate(field_names):
            if arr.ndim == 1:
                if i > 0:
                    print("Non-structured 1D data supports only one feature field")
                    sys.exit(2)
                features[field] = np.asarray(arr, dtype=np.float64)
            else:
                if i >= arr.shape[1]:
                    print(f"Feature index out of range for field '{field}'")
                    sys.exit(2)
                features[field] = np.asarray(arr[:, i], dtype=np.float64)
        if args.returns_field:
            print("--returns-field is only supported for structured arrays")
            sys.exit(2)
        returns = None

    engine = AlphaSearchEngine(
        features=features,
        returns=returns,
        random_seed=int(args.seed),
    )

    if mode == "random":
        results = engine.random_search(n_trials=int(args.trials))
    elif mode == "template":
        grid = _parse_param_grid(args.grid)
        results = engine.template_sweep(args.template, grid)
    else:
        results = engine.genetic_search(population=int(args.population), generations=int(args.generations))

    top_n = max(1, int(args.top))
    top_results = results[:top_n]
    payload: dict[str, Any] = {
        "mode": mode,
        "count": len(top_results),
        "results": [item.to_dict() for item in top_results],
    }

    if args.save_results:
        payload["results_path"] = engine.save_results(top_results, path=str(args.save_results))

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))


def _parse_param_grid(raw: str | None) -> dict[str, list[Any]]:
    if not raw:
        return {}
    grid: dict[str, list[Any]] = {}
    pairs = [part.strip() for part in str(raw).split(";") if part.strip()]
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Invalid grid token: {pair}")
        key, val = pair.split("=", 1)
        items = [item.strip() for item in val.split(",") if item.strip()]
        casted: list[Any] = []
        for item in items:
            try:
                casted.append(int(item))
                continue
            except ValueError:
                pass
            try:
                casted.append(float(item))
                continue
            except ValueError:
                pass
            casted.append(item)
        grid[key.strip()] = casted
    return grid


def cmd_alpha_list(args: argparse.Namespace) -> None:
    try:
        from research.registry.alpha_registry import AlphaRegistry
    except Exception as exc:
        print(f"Failed to import research registry: {exc}")
        sys.exit(1)

    registry = AlphaRegistry()
    loaded = registry.discover("research/alphas")
    if not loaded:
        print("No alpha artifacts discovered.")
        return

    for alpha_id in sorted(loaded):
        manifest = loaded[alpha_id].manifest
        print(f"{alpha_id}\tstatus={manifest.status.value}\ttier={manifest.tier.value if manifest.tier else '-'}")

    if registry.errors:
        print("\nDiscovery warnings:")
        for msg in registry.errors:
            print(f"- {msg}")


def cmd_alpha_validate(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.validation import ValidationConfig, run_alpha_validation
    except Exception as exc:
        print(f"Failed to import alpha validation pipeline: {exc}")
        sys.exit(1)

    config = ValidationConfig(
        alpha_id=args.alpha_id,
        data_paths=[str(p) for p in args.data],
        is_oos_split=float(args.is_oos_split),
        signal_threshold=float(args.signal_threshold),
        max_position=int(args.max_position),
        min_sharpe_oos=float(args.min_sharpe_oos),
        max_abs_drawdown=float(args.max_abs_drawdown),
        min_turnover=float(getattr(args, "min_turnover", 1e-6)),
        skip_gate_b_tests=bool(args.skip_gate_b_tests),
        pytest_timeout_s=int(args.pytest_timeout),
        project_root=".",
        experiments_dir=str(args.experiments_dir),
        latency_profile_id=str(getattr(args, "latency_profile_id", "sim_p95_v2026-02-26")),
        local_decision_pipeline_latency_us=int(getattr(args, "local_decision_pipeline_latency_us", 250)),
        submit_ack_latency_ms=float(getattr(args, "submit_ack_latency_ms", 36.0)),
        modify_ack_latency_ms=float(getattr(args, "modify_ack_latency_ms", 43.0)),
        cancel_ack_latency_ms=float(getattr(args, "cancel_ack_latency_ms", 47.0)),
        live_uplift_factor=float(getattr(args, "live_uplift_factor", 1.5)),
        maker_fee_bps=float(getattr(args, "maker_fee_bps", -0.2)),
        taker_fee_bps=float(getattr(args, "taker_fee_bps", 0.2)),
        stat_pvalue_threshold=float(getattr(args, "stat_pvalue_threshold", 0.1)),
        min_stat_tests_pass=int(getattr(args, "min_stat_tests_pass", 2)),
        stat_correction_method=str(getattr(args, "stat_correction_method", "bh")),
        min_stat_tests_bh_pass=int(getattr(args, "min_stat_tests_bh_pass", 1)),
        enable_walk_forward=bool(getattr(args, "enable_walk_forward", True)),
        wf_n_splits=int(getattr(args, "wf_n_splits", 5)),
        wf_min_fold_consistency=float(getattr(args, "wf_min_fold_consistency", 0.6)),
        wf_min_fold_sharpe_min=float(getattr(args, "wf_min_fold_sharpe_min", -0.5)),
        enable_param_optimization=bool(getattr(args, "enable_param_optimization", True)),
        opt_signal_threshold_min=float(getattr(args, "opt_signal_threshold_min", 0.05)),
        opt_signal_threshold_max=float(getattr(args, "opt_signal_threshold_max", 0.6)),
        opt_signal_threshold_steps=int(getattr(args, "opt_signal_threshold_steps", 8)),
        opt_objective=str(getattr(args, "opt_objective", "risk_adjusted")),
        opt_max_is_oos_gap=float(getattr(args, "opt_max_is_oos_gap", 1.0)),
        opt_min_neighbor_objective_ratio=float(getattr(args, "opt_min_neighbor_objective_ratio", 0.6)),
        opt_min_deflated_sharpe=float(getattr(args, "opt_min_deflated_sharpe", -0.1)),
        require_paper_refs=bool(getattr(args, "require_paper_refs", False)),
        require_paper_index_link=bool(getattr(args, "require_paper_index_link", False)),
        enforce_data_governance=bool(getattr(args, "enforce_data_governance", False)),
        require_data_meta=bool(getattr(args, "require_data_meta", False)),
        allowed_data_roots=tuple(getattr(args, "allowed_data_roots", ()) or ()),
        bootstrap_samples=int(getattr(args, "bootstrap_samples", 1000)),
        stress_latency_multiplier=float(getattr(args, "stress_latency_multiplier", 1.5)),
        stress_fee_multiplier=float(getattr(args, "stress_fee_multiplier", 1.5)),
        min_stress_sharpe_ratio=float(getattr(args, "min_stress_sharpe_ratio", 0.5)),
        stress_drawdown_limit_multiplier=float(getattr(args, "stress_drawdown_limit_multiplier", 1.25)),
    )
    result = run_alpha_validation(config)
    summary = result.to_dict()
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not result.passed:
        sys.exit(2)


def cmd_alpha_promote(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.promotion import PromotionConfig, promote_alpha
    except Exception as exc:
        print(f"Failed to import alpha promotion pipeline: {exc}")
        sys.exit(1)

    config = PromotionConfig(
        alpha_id=args.alpha_id,
        owner=args.owner,
        project_root=".",
        experiments_dir=str(getattr(args, "experiments_dir", "research/experiments")),
        scorecard_path=args.scorecard,
        shadow_sessions=int(args.shadow_sessions),
        min_shadow_sessions=int(args.min_shadow_sessions),
        drift_alerts=int(args.drift_alerts),
        execution_reject_rate=float(args.execution_reject_rate),
        max_execution_reject_rate=float(args.max_execution_reject_rate),
        require_paper_trade_governance=bool(getattr(args, "require_paper_trade_governance", False)),
        paper_trade_summary_path=(getattr(args, "paper_trade_summary", None) or None),
        min_paper_trade_calendar_days=int(getattr(args, "min_paper_trade_calendar_days", 7)),
        min_paper_trade_trading_days=int(getattr(args, "min_paper_trade_trading_days", 5)),
        min_paper_trade_session_minutes=int(getattr(args, "min_paper_trade_session_minutes", 30)),
        min_sharpe_oos=float(args.min_sharpe_oos),
        max_abs_drawdown=float(args.max_abs_drawdown),
        max_turnover=float(args.max_turnover),
        max_correlation=float(args.max_correlation),
        enable_rust_readiness_gate=bool(getattr(args, "enable_rust_readiness_gate", False)),
        rust_module_name=(getattr(args, "rust_module_name", None) or None),
        rust_parity_test_path=str(getattr(args, "rust_parity_test_path", "tests/unit/test_rust_hotpath_parity.py")),
        rust_parity_timeout_s=int(getattr(args, "rust_parity_timeout_s", 180)),
        enforce_rust_benchmark_gate=bool(getattr(args, "enforce_rust_benchmark_gate", False)),
        rust_benchmark_cmd=str(
            getattr(
                args,
                "rust_benchmark_cmd",
                (
                    "uv run python tests/benchmark/perf_regression_gate.py "
                    "--baseline tests/benchmark/.benchmark_baseline.json "
                    "--current benchmark.json "
                    "--threshold 0.10"
                ),
            )
        ),
        canary_weight=(None if args.canary_weight is None else float(args.canary_weight)),
        expiry_days=int(args.expiry_days),
        max_live_slippage_bps=float(args.max_live_slippage_bps),
        max_live_drawdown_contribution=float(args.max_live_drawdown_contribution),
        max_execution_error_rate=float(args.max_execution_error_rate),
        force=bool(args.force),
        config_version=str(getattr(args, "config_version", "v1") or "v1"),
        parent_config_version=getattr(args, "parent_config_version", None) or None,
    )
    result = promote_alpha(config)
    if result.checklist is not None:
        print("Promotion Checklist:")
        for item in result.checklist.items:
            status = "PASS" if item.passed else "FAIL"
            print(f"  [{status}] {item.label} [{item.detail}]")
        print()
    summary = result.to_dict()
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not result.approved:
        sys.exit(2)


def cmd_alpha_rl_promote(args: argparse.Namespace) -> None:
    try:
        promote_latest_rl_run = import_module("research.rl.lifecycle").promote_latest_rl_run
    except Exception as exc:
        print(f"Failed to import RL lifecycle promotion utility: {exc}")
        sys.exit(1)

    result = promote_latest_rl_run(
        alpha_id=str(args.alpha_id),
        owner=str(args.owner),
        base_dir=str(args.base_dir),
        project_root=str(args.project_root),
        shadow_sessions=int(args.shadow_sessions),
        min_shadow_sessions=int(args.min_shadow_sessions),
        drift_alerts=int(args.drift_alerts),
        execution_reject_rate=float(args.execution_reject_rate),
        force=bool(args.force),
    )
    payload = result.to_dict()
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))
    if not result.approved:
        sys.exit(2)


def cmd_alpha_pool(args: argparse.Namespace) -> None:
    try:
        import importlib

        pool_mod = importlib.import_module("hft_platform.alpha.pool")
    except Exception as exc:
        print(f"Failed to import alpha pool utilities: {exc}")
        sys.exit(1)

    pool_cmd = getattr(args, "pool_cmd", "matrix")
    _threshold_raw = getattr(args, "threshold", None)
    threshold = float(_threshold_raw) if _threshold_raw is not None else 0.7
    method = str(getattr(args, "method", "equal_weight"))
    ridge_alpha = float(getattr(args, "ridge_alpha", 0.1))
    min_uplift = float(getattr(args, "min_uplift", 0.05))
    alpha_id = getattr(args, "alpha_id", None)
    payload: dict[str, Any]

    if pool_cmd == "optimize":
        result = pool_mod.optimize_pool_weights(
            base_dir=args.base_dir,
            method=method,
            ridge_alpha=ridge_alpha,
        )
        payload = {"optimization": result.to_dict()}
    elif pool_cmd == "marginal":
        if not alpha_id:
            print("alpha pool marginal requires --alpha-id")
            sys.exit(2)
        payload = {
            "marginal": pool_mod.evaluate_marginal_alpha(
                alpha_id=str(alpha_id),
                base_dir=args.base_dir,
                method=method,
                min_uplift=min_uplift,
                ridge_alpha=ridge_alpha,
            )
        }
    else:
        matrix = pool_mod.compute_pool_matrix(base_dir=args.base_dir)
        payload = {"matrix": matrix}
        include_redundant = bool(getattr(args, "redundant", False)) or pool_cmd == "redundant"
        if include_redundant:
            metric = str(getattr(args, "corr_metric", "pearson"))
            try:
                payload["redundant"] = pool_mod.flag_redundant_pairs(matrix, threshold=threshold, metric=metric)
            except TypeError:
                # Backward compatibility for legacy helper signature without metric arg.
                payload["redundant"] = pool_mod.flag_redundant_pairs(matrix, threshold=threshold)
            payload["threshold"] = threshold
            payload["metric"] = metric

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_alpha_canary_status(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.canary import CanaryMonitor
    except Exception as exc:
        print(f"Failed to import canary monitor: {exc}")
        sys.exit(1)

    monitor = CanaryMonitor(promotions_dir=args.promotions_dir)
    canaries = monitor.load_active_canaries()
    if not canaries:
        print("No active canaries found.")
        return

    payload = []
    for c in canaries:
        payload.append(
            {
                "alpha_id": c.get("alpha_id", "?"),
                "weight": c.get("weight", 0),
                "enabled": c.get("enabled", False),
                "path": c.get("_path", ""),
            }
        )
    print(json.dumps({"canaries": payload, "count": len(payload)}, indent=2, sort_keys=True))


def cmd_alpha_canary_evaluate(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.canary import CanaryMonitor
    except Exception as exc:
        print(f"Failed to import canary monitor: {exc}")
        sys.exit(1)

    monitor = CanaryMonitor(promotions_dir=args.promotions_dir)
    live_metrics = {
        "slippage_bps": float(args.slippage_bps),
        "drawdown_contribution": float(args.dd_contrib),
        "execution_error_rate": float(args.error_rate),
        "sessions_live": int(args.sessions),
    }
    if args.sharpe_live is not None:
        live_metrics["sharpe_live"] = float(args.sharpe_live)

    status = monitor.evaluate(args.alpha_id, live_metrics)
    payload = status.to_dict()

    if args.apply:
        monitor.apply_decision(status)
        payload["applied"] = True

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_alpha_canary_auto_evaluate(args: argparse.Namespace) -> None:
    """Run one-shot auto-evaluation of all active canaries."""
    try:
        from hft_platform.alpha.canary import CanaryMonitor
        from hft_platform.alpha.canary_scheduler import CanaryAutoScheduler
    except Exception as exc:
        print(f"Failed to import canary auto-scheduler: {exc}")
        sys.exit(1)

    monitor = CanaryMonitor(promotions_dir=args.promotions_dir)
    dry_run = args.dry_run
    scheduler = CanaryAutoScheduler(monitor=monitor, dry_run=dry_run)

    import asyncio

    results = asyncio.run(scheduler.evaluate_all())

    payload: list[dict[str, Any]] = [s.to_dict() for s in results]
    summary = {
        "count": len(payload),
        "dry_run": dry_run,
        "results": payload,
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))


def cmd_alpha_ab_compare(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.experiments import ExperimentTracker
    except Exception as exc:
        print(f"Failed to import experiment tracker: {exc}")
        sys.exit(1)

    base_dir = getattr(args, "base_dir", "research/experiments")
    tracker = ExperimentTracker(base_dir=base_dir)
    rows = tracker.compare(run_ids=[args.run_id_a, args.run_id_b])

    if len(rows) < 2:
        found_ids = [r.get("run_id", "?") for r in rows]
        print(f"Error: fewer than 2 runs found (got: {found_ids}). Check run IDs.")
        sys.exit(1)

    run_a = rows[0]
    run_b = rows[1]

    # Collect numeric metrics from both runs
    all_metric_keys: list[str] = []
    seen: set[str] = set()
    for key in list(run_a.keys()) + list(run_b.keys()):
        if key in seen or key in {"run_id", "alpha_id", "config_hash", "timestamp"}:
            continue
        val_a = run_a.get(key)
        val_b = run_b.get(key)
        if isinstance(val_a, (int, float)) or isinstance(val_b, (int, float)):
            all_metric_keys.append(key)
            seen.add(key)

    col_w = 16
    sep = "\u2500" * (20 + col_w * 3)
    id_a = str(run_a.get("run_id", "?"))
    id_b = str(run_b.get("run_id", "?"))
    print(f"A/B Comparison: {id_a} vs {id_b}")
    print(sep)
    print(f"{'Metric':<20}{'Run A'.ljust(col_w)}{'Run B'.ljust(col_w)}{'Delta'.ljust(col_w)}")
    print(sep)
    for key in all_metric_keys:
        val_a = run_a.get(key)
        val_b = run_b.get(key)
        try:
            fa = float(val_a) if val_a is not None else None
            fb = float(val_b) if val_b is not None else None
        except (TypeError, ValueError):
            fa = fb = None
        if fa is None and fb is None:
            continue
        str_a = f"{fa:.3f}" if fa is not None else "-"
        str_b = f"{fb:.3f}" if fb is not None else "-"
        if fa is not None and fb is not None:
            delta = fb - fa
            str_delta = f"{delta:+.3f}"
        else:
            str_delta = "-"
        print(f"{key:<20}{str_a.ljust(col_w)}{str_b.ljust(col_w)}{str_delta.ljust(col_w)}")
    print(sep)

    if getattr(args, "out", None):
        payload = {"run_a": run_a, "run_b": run_b}
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def cmd_alpha_experiments_compare(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.experiments import ExperimentTracker
    except Exception as exc:
        print(f"Failed to import experiment tracker: {exc}")
        sys.exit(1)

    tracker = ExperimentTracker(base_dir=args.base_dir)
    rows = tracker.compare(run_ids=list(args.run_ids))
    payload = {"runs": rows, "count": len(rows)}
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_alpha_experiments_list(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.experiments import ExperimentTracker
    except Exception as exc:
        print(f"Failed to import experiment tracker: {exc}")
        sys.exit(1)

    tracker = ExperimentTracker(base_dir=args.base_dir)
    rows = [run.to_dict() for run in tracker.list_runs(alpha_id=args.alpha_id)]
    payload = {"runs": rows, "count": len(rows)}
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_alpha_experiments_best(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.experiments import ExperimentTracker
    except Exception as exc:
        print(f"Failed to import experiment tracker: {exc}")
        sys.exit(1)

    tracker = ExperimentTracker(base_dir=args.base_dir)
    rows = tracker.best_by_metric(
        metric=args.metric,
        n=int(args.top),
        alpha_id=args.alpha_id,
    )
    payload = {"metric": args.metric, "runs": rows, "count": len(rows)}
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))
