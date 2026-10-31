"""CLI commands: alpha list, promote, pool, canary, ab-compare, experiments, paper-trade-batch, kill, cluster.

The research subcommands (scaffold, search, mine, screen, validate, validate-batch,
pipeline, batch-correlation, promote-batch) run in research's process: see
cli/_research.py and research/registry/alpha_cli.py.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def cmd_alpha_list(args: argparse.Namespace) -> None:
    try:
        from hft_platform.alpha.discovery import AlphaDiscoveryRegistry
    except Exception as exc:
        print(f"Failed to import alpha discovery registry: {exc}")
        sys.exit(1)

    from hft_platform.alpha.evidence import EvidenceRootError
    from hft_platform.alpha.status import effective_status, ledger_events_by_alpha

    try:
        ledger = ledger_events_by_alpha()
    except (EvidenceRootError, ValueError, OSError) as exc:
        print(f"Cannot read the verdict ledger: {exc}", file=sys.stderr)
        sys.exit(2)

    registry = AlphaDiscoveryRegistry()
    loaded = registry.discover("research/alphas")
    if not loaded:
        print("No alpha artifacts discovered.")
        return

    for alpha_id in sorted(loaded):
        manifest = loaded[alpha_id].manifest
        status = effective_status(manifest.status, ledger.get(alpha_id, ()))
        tier = manifest.tier.value if manifest.tier else "-"
        print(f"{alpha_id}\tstatus={status.status.value}\tsource={status.source.value}\ttier={tier}")

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
            "validation profile (see config/research/profiles/vm_ul6_strict_v2.yaml). "
            "Use `hft alpha screen` for loose pre-Gate-C evaluation.",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        from hft_platform.alpha._validation_profile import load_profile
        from hft_platform.alpha.paths import repo_path_or_relative
    except Exception as exc:
        print(f"Failed to import validation profile loader: {exc}", file=sys.stderr)
        sys.exit(1)
    profile_path = Path(profile_arg)
    if not profile_path.is_absolute() and not profile_path.exists():
        candidate = repo_path_or_relative("config/research/profiles") / f"{profile_arg}.yaml"
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

    profile = _load_strict_validation_profile(getattr(args, "profile", None))
    dry_run = bool(getattr(args, "dry_run", False))

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
        replay_parity_report_paths=tuple(str(p) for p in (getattr(args, "replay_parity_report", None) or [])),
        replay_strategy_id=(getattr(args, "replay_strategy_id", None) or None),
        replay_strategy=(getattr(args, "replay_strategy", None) or None),
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
        validation_profile=profile,
        dry_run=dry_run,
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
    reports = [str(p) for p in (getattr(args, "replay_parity_report", None) or [])]
    if reports:
        live_metrics["replay_parity_reports"] = reports

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


# ---------------------------------------------------------------------------
# Slice-D Task 11: cmd_alpha_cheap_screen
# (Renamed during rebase onto #341 — `alpha screen` is the loose-mode validate
# verb from #341, so the cheap pre-screener registers as `alpha cheap-screen`.)
# ---------------------------------------------------------------------------


def cmd_alpha_cheap_screen(args: argparse.Namespace) -> None:
    """Run the cheap pre-screener (IC + turnover + cost-floor) for one alpha.

    On ``verdict='kill'`` and ``--write-kill``, append a ``gate='pre_screen'``
    row to the kill ledger. Exit codes mirror the promote shape:
      * 0 — verdict == 'pass' or 'unknown'
      * 2 — verdict == 'kill'
      * 1 — infra/import failure
    """
    try:
        from hft_platform.alpha.screener import cheap_screen
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to import alpha screener: {exc}")
        sys.exit(1)

    result = cheap_screen(
        args.alpha_id,
        project_root=Path(getattr(args, "project_root", None) or "."),
        ic_min_abs=(float(args.threshold_ic) if getattr(args, "threshold_ic", None) is not None else None),
        turnover_kill=(
            float(args.threshold_turnover) if getattr(args, "threshold_turnover", None) is not None else None
        ),
    )
    payload = {
        "alpha_id": result.alpha_id,
        "verdict": result.verdict,
        "ic_mean": result.ic_mean,
        "ic_std": result.ic_std,
        "turnover": result.turnover,
        "cost_floor_breach": result.cost_floor_breach,
        "reason": result.reason,
        "duration_s": result.duration_s,
        # A cheap screen is not a powered verdict: a screen "kill" means the
        # candidate is screened out, not that it is dead.
        "research_outcome": "SCREENED_OUT" if result.verdict == "kill" else "SCREENED_IN",
    }
    print(json.dumps(payload, indent=2, sort_keys=True))

    if getattr(args, "write_kill", False):
        print(
            "Warning: --write-kill has no effect; screen results are SCREENED_OUT, not kills, "
            "and are never written to the kill ledger",
            file=sys.stderr,
        )

    if result.verdict == "kill":
        sys.exit(2)


# ---------------------------------------------------------------------------
# Slice-D Task 12: cmd_alpha_kill
# ---------------------------------------------------------------------------


def cmd_alpha_kill(args: argparse.Namespace) -> None:
    """Manually record a kill in the kill ledger (operator path).

    Does NOT mutate ``manifest.yaml`` (kill_reason is no longer a manifest
    field; the kill ledger is the source of truth).

    Exit codes:
      * 0 — row inserted (or duplicate; idempotent)
      * 1 — empty/whitespace reason or import failure
    """
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        print("Error: --reason cannot be empty or whitespace", file=sys.stderr)
        sys.exit(1)

    try:
        from hft_platform.alpha.kill_ledger import KillRecord, append_kill
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to import kill ledger: {exc}")
        sys.exit(1)

    gate = getattr(args, "gate", None) or "manual"
    operator = getattr(args, "killed_by", None) or "operator"
    record = KillRecord(
        alpha_id=args.alpha_id,
        gate=gate,
        reason=reason,
        stable_artifact_hash="",
        scorecard_id="",
        killed_by=f"cli:kill:{operator}",
    )
    inserted = append_kill(record)
    payload = {
        "alpha_id": record.alpha_id,
        "gate": record.gate,
        "reason": record.reason,
        "kill_id": record.kill_id(),
        "inserted": bool(inserted),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    # Duplicate kill is idempotent success — exit 0 either way.


# ---------------------------------------------------------------------------
# Slice-D Task 13: cmd_alpha_cluster
# ---------------------------------------------------------------------------


def cmd_alpha_cluster(args: argparse.Namespace) -> None:
    """Run hierarchical correlation clustering across alphas.

    Does NOT mutate ``manifest.yaml`` (cluster_id is no longer a manifest
    field). Optionally persists a sidecar JSON keyed by
    ``(threshold, metric, base_dir, corpus)``.

    Exit codes:
      * 0 — clustering succeeded
      * 1 — import failure
      * 2 — empty corpus
    """
    try:
        from hft_platform.alpha.cluster import EmptyCorpusError, cluster_alphas
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to import alpha cluster: {exc}")
        sys.exit(1)

    try:
        assignments = cluster_alphas(
            base_dir=getattr(args, "base_dir", None) or "research/experiments",
            threshold=float(args.threshold),
            metric=args.metric,
            write_artifact=bool(getattr(args, "write_artifact", False)),
        )
    except EmptyCorpusError:
        print("No signals available to cluster (empty corpus).", file=sys.stderr)
        sys.exit(2)

    if getattr(args, "json", False):
        payload = [
            {
                "alpha_id": a.alpha_id,
                "cluster_id": a.cluster_id,
                "cluster_size": a.cluster_size,
                "max_intra_cluster_corr": a.max_intra_cluster_corr,
            }
            for a in assignments
        ]
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    header = f"{'alpha_id':<40} {'cluster_id':<28} {'size':<5} {'max_corr':<10}"
    print(header)
    print("-" * len(header))
    for a in assignments:
        print(f"{a.alpha_id:<40} {a.cluster_id:<28} {a.cluster_size:<5} {a.max_intra_cluster_corr:<10.4f}")


# ── Stage 5: canonical pipeline orchestrator wrappers (D2) ──────────────────
# Thin delegates onto research.pipeline._run_pipeline so that
# `hft alpha pipeline {run,triage}` and `make research[-triage]` share a single
# orchestration codepath. Do NOT duplicate orchestration logic here.
