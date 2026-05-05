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


def _load_strict_validation_profile(profile_arg: str | None) -> Any:
    """Resolve ``--profile`` for ``hft alpha validate``.

    A strict profile is mandatory for Gate-D-eligible validation runs.
    Returns the loaded profile object on success; exits with code 2 on
    any failure (missing arg, file not found, profile not strict).
    """
    if not profile_arg:
        print(
            "[hft alpha validate] --profile is required and must reference a strict "
            "validation profile (see config/research/profiles/vm_ul6_strict.yaml). "
            "Use `hft alpha screen` for loose pre-Gate-C evaluation.",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        from hft_platform.alpha._validation_profile import load_profile
    except Exception as exc:
        print(f"Failed to import validation profile loader: {exc}", file=sys.stderr)
        sys.exit(1)
    profile_path = Path(profile_arg)
    if not profile_path.is_absolute() and not profile_path.exists():
        candidate = Path("config/research/profiles") / f"{profile_arg}.yaml"
        if candidate.exists():
            profile_path = candidate
    try:
        profile = load_profile(profile_path)
    except FileNotFoundError as exc:
        print(f"[hft alpha validate] profile not found: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001
        print(f"[hft alpha validate] profile load failed: {exc}", file=sys.stderr)
        sys.exit(2)
    if not getattr(profile, "is_strict", False):
        print(
            f"[hft alpha validate] profile {getattr(profile, 'name', profile_arg)!r} is not "
            "strict; refusing to run. `hft alpha validate` only accepts strict profiles.",
            file=sys.stderr,
        )
        sys.exit(2)
    return profile


def cmd_alpha_screen(args: argparse.Namespace) -> None:
    """Loose-mode alpha screening — stamps ``screen_only=true`` on the scorecard.

    A screen artifact is **not** promotion-eligible; ``hft alpha promote``
    rejects any scorecard with ``screen_only=true``. Use this for early
    triage during research; use ``hft alpha validate --profile strict``
    when an artifact must be eligible for Gate D.
    """
    try:
        from hft_platform.alpha.validation import ValidationConfig, run_alpha_validation
    except Exception as exc:
        print(f"Failed to import alpha validation pipeline: {exc}")
        sys.exit(1)

    import datetime as _dt

    config = ValidationConfig(
        alpha_id=args.alpha_id,
        data_paths=[str(p) for p in args.data],
        is_oos_split=float(getattr(args, "is_oos_split", 0.7)),
        signal_threshold=float(getattr(args, "signal_threshold", 0.3)),
        max_position=int(getattr(args, "max_position", 5)),
        min_sharpe_oos=float(getattr(args, "min_sharpe_oos", 0.0)),
        max_abs_drawdown=float(getattr(args, "max_abs_drawdown", 0.3)),
        skip_gate_b_tests=bool(getattr(args, "skip_gate_b_tests", False)),
        pytest_timeout_s=int(getattr(args, "pytest_timeout", 300)),
        project_root=".",
        experiments_dir=str(getattr(args, "experiments_dir", "research/experiments")),
        profile=None,
    )
    result = run_alpha_validation(config)
    summary = result.to_dict()

    try:
        scorecard_path = Path(result.scorecard_path)
        if scorecard_path.exists():
            payload = json.loads(scorecard_path.read_text())
            payload["screen_only"] = True
            payload["screen_profile"] = "loose_default"
            payload["screen_timestamp"] = (
                _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
            )
            scorecard_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
            summary["screen_only"] = True
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: failed to stamp screen_only on scorecard: {exc}", file=sys.stderr)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not result.passed:
        sys.exit(2)


def cmd_alpha_validate(args: argparse.Namespace) -> None:
    profile = _load_strict_validation_profile(getattr(args, "profile", None))
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
        profile=profile,
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


def _refuse_screen_or_synthetic_scorecard(scorecard_arg: str | None) -> None:
    """L6: Pre-flight scorecard guard for ``hft alpha promote``.

    Reads ``--scorecard`` (when provided) and refuses to invoke the
    promotion pipeline if the artifact is screen-only or has synthetic
    equity. This is a fast-fail UX layer; ``promote_alpha`` repeats the
    check internally for callers that bypass the CLI.
    """
    if not scorecard_arg:
        return
    path = Path(scorecard_arg)
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"[hft alpha promote] failed to read scorecard {path}: {exc}", file=sys.stderr)
        return
    if bool(payload.get("screen_only", False)):
        print(
            f"[hft alpha promote] cannot_promote_screen_artifact: {path} is stamped "
            "screen_only=true; produce a strict-validation artifact via "
            "`hft alpha validate --profile strict` first.",
            file=sys.stderr,
        )
        sys.exit(2)
    eq_src_raw = payload.get("equity_source")
    eq_src = str(eq_src_raw).strip() if isinstance(eq_src_raw, str) else None
    if eq_src == "synthetic":
        print(
            f"[hft alpha promote] cannot_promote_synthetic_equity: {path} reports "
            "equity_source='synthetic'; re-run the backtest under "
            "strict_equity=True with a real equity series.",
            file=sys.stderr,
        )
        sys.exit(2)


def cmd_alpha_promote(args: argparse.Namespace) -> None:
    _refuse_screen_or_synthetic_scorecard(getattr(args, "scorecard", None))
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


def cmd_alpha_batch_correlation(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.batch_correlation import batch_compute_correlations
    except Exception as exc:
        print(f"Failed to import batch correlation module: {exc}")
        sys.exit(1)

    results = batch_compute_correlations(
        experiments_dir=str(getattr(args, "experiments_dir", "research/experiments")),
        project_root=".",
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    payload = {"correlations": results, "count": len(results)}
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_alpha_paper_trade_batch(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.paper_trade_batch import (
            batch_record_sessions,
            discover_gate_d_candidates,
        )
    except Exception as exc:
        print(f"Failed to import paper trade batch module: {exc}")
        sys.exit(1)

    action = getattr(args, "paper_trade_action", None)
    experiments_dir = str(getattr(args, "experiments_dir", "research/experiments"))

    if action == "discover":
        candidates = discover_gate_d_candidates(
            experiments_dir=experiments_dir,
            top_n=int(getattr(args, "top_n", 20)),
            min_sharpe_oos=float(getattr(args, "min_sharpe_oos", 1.0)),
        )
        payload: dict[str, Any] = {"candidates": candidates, "count": len(candidates)}
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif action == "record":
        alpha_ids = list(args.alpha_ids) if args.alpha_ids else []
        if not alpha_ids:
            print("No alpha IDs specified. Use --alpha-ids or run 'discover' first.")
            sys.exit(2)
        results = batch_record_sessions(
            alpha_ids=alpha_ids,
            experiments_dir=experiments_dir,
            sessions_per_alpha=int(getattr(args, "sessions_per_alpha", 5)),
            base_date=getattr(args, "base_date", None),
            seed=int(getattr(args, "seed", 42)),
        )
        payload = {"sessions": results, "count": len(results)}
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("Usage: hft alpha paper-trade-batch {discover|record}")
        sys.exit(2)


def cmd_alpha_promote_batch(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.batch_promote import BatchPromoter
    except Exception as exc:
        print(f"Failed to import batch promote module: {exc}")
        sys.exit(1)

    promoter = BatchPromoter(
        experiments_dir=str(getattr(args, "experiments_dir", "research/experiments")),
        project_root=".",
        owner=str(getattr(args, "owner", "batch")),
        min_sharpe_oos=float(getattr(args, "min_sharpe_oos", 1.0)),
        max_abs_drawdown=float(getattr(args, "max_abs_drawdown", 0.2)),
        max_correlation=float(getattr(args, "max_correlation", 0.7)),
    )

    alpha_ids = list(getattr(args, "alpha_ids", None) or []) or None
    results = promoter.run_fleet(
        dry_run=bool(getattr(args, "dry_run", True)),
        top_n=int(getattr(args, "top_n", 50)),
        alpha_ids=alpha_ids,
    )

    approved = sum(1 for r in results if r.get("approved"))
    payload = {
        "results": results,
        "total": len(results),
        "approved": approved,
        "rejected": len(results) - approved,
    }
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    if approved == 0 and results:
        sys.exit(2)


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


def cmd_alpha_validate_batch(args: argparse.Namespace) -> None:
    """Run Gate A-C validation across multiple alphas sequentially."""
    import time

    try:
        from hft_platform.alpha.validation import ValidationConfig, run_alpha_validation
    except Exception as exc:
        print(f"Failed to import alpha validation pipeline: {exc}")
        sys.exit(1)

    try:
        from research.registry.alpha_registry import AlphaRegistry
    except Exception as exc:
        print(f"Failed to import alpha registry: {exc}")
        sys.exit(1)

    alphas_dir = getattr(args, "alphas_dir", "research/alphas")
    registry = AlphaRegistry()
    all_alphas = registry.discover(alphas_dir)

    alpha_ids_filter = getattr(args, "alpha_ids", None)
    if alpha_ids_filter:
        target_ids = sorted(set(alpha_ids_filter) & set(all_alphas.keys()))
        missing = set(alpha_ids_filter) - set(all_alphas.keys())
        if missing:
            print(f"Warning: alpha IDs not found: {sorted(missing)}")
    else:
        target_ids = sorted(all_alphas.keys())

    gates = getattr(args, "gates", "ABC").upper()
    skip_gate_c = "C" not in gates
    skip_gate_b = "B" not in gates
    data_paths = [str(p) for p in args.data]

    print(f"Batch validation: {len(target_ids)} alphas, gates={gates}, data={data_paths}")

    results: list[dict[str, Any]] = []
    passed_ids: list[str] = []
    failed_ids: list[str] = []
    errored_ids: list[str] = []
    t0 = time.monotonic()

    for i, alpha_id in enumerate(target_ids, 1):
        print(f"\n[{i}/{len(target_ids)}] Validating {alpha_id} ...", flush=True)
        try:
            config = ValidationConfig(
                alpha_id=alpha_id,
                data_paths=data_paths,
                skip_gate_b_tests=skip_gate_b,
                min_sharpe_oos=float(getattr(args, "min_sharpe_oos", 0.0)),
                max_abs_drawdown=float(getattr(args, "max_abs_drawdown", 0.3)),
                project_root=".",
                experiments_dir=str(getattr(args, "experiments_dir", "research/experiments")),
            )
            result = run_alpha_validation(config)
            entry = result.to_dict()

            if skip_gate_c and result.gate_a.passed and (skip_gate_b or result.gate_b.passed):
                entry["passed"] = True
                entry["note"] = "Gate C skipped per --gates flag"

            results.append(entry)
            if entry.get("passed"):
                passed_ids.append(alpha_id)
                print(f"  PASS {alpha_id}")
            else:
                failed_ids.append(alpha_id)
                print(f"  FAIL {alpha_id}")

            if getattr(args, "fail_fast", False) and not entry.get("passed"):
                print("Stopping early (--fail-fast)")
                break

        except Exception as exc:
            errored_ids.append(alpha_id)
            results.append({"alpha_id": alpha_id, "passed": False, "error": str(exc)})
            print(f"  ERROR {alpha_id}: {exc}")

    elapsed_s = time.monotonic() - t0
    report = {
        "summary": {
            "total": len(target_ids),
            "passed": len(passed_ids),
            "failed": len(failed_ids),
            "errored": len(errored_ids),
            "elapsed_s": round(elapsed_s, 1),
            "gates": gates,
        },
        "passed_alphas": passed_ids,
        "failed_alphas": failed_ids,
        "errored_alphas": errored_ids,
        "results": results,
    }

    out_path = getattr(args, "out", None)
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, indent=2, sort_keys=True))
        print(f"\nReport written to {out_path}")

    print(f"\n{'=' * 60}")
    print(
        f"Batch complete: {len(passed_ids)} passed, {len(failed_ids)} failed, "
        f"{len(errored_ids)} errors ({elapsed_s:.1f}s)"
    )

    if failed_ids or errored_ids:
        sys.exit(2)
