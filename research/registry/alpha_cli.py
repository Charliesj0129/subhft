"""`hft alpha` research subcommands, run in research's own process (ADR-005, research v2 C5).

``hft alpha {scaffold,search,mine,screen,validate,validate-batch,pipeline}``
parses its arguments with the platform parser, then re-runs as
``python -m research alpha <same argv>``; this module is that entry. The
argument definitions stay in ``hft_platform.cli._parser`` (one source, and
what the docs contract check reads); only the command bodies live here, so
the platform never imports research code. Validation commands inject the
research Gate C runtime (``build_gate_c_runtime``) instead of the platform
importing it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any

from hft_platform.cli import _alpha as _platform_alpha
from research.backtest.gate_c_runtime import build_gate_c_runtime


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
        "trials": engine.trial_summary(),
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


def cmd_alpha_mine_init(args: argparse.Namespace) -> None:
    """SubHFT Alpha Mining v2 Phase 1: build and write a partition manifest.

    Loads a session-id column from the data file and splits it into
    Discovery/Selection/Locked-validation/Final-holdout partitions
    (contiguous in time order, with an embargo gap at each boundary), then
    writes the immutable ``partition_manifest.json``. Locked-validation and
    final-holdout stay unreadable until a candidate is frozen — see
    ``DatasetPartitionManager.get_rows`` / ``freeze_candidate``.
    """
    try:
        np = import_module("numpy")
        partitioning = import_module("research.combinatorial.partitioning")
    except Exception as exc:
        print(f"Failed to import partitioning module: {exc}")
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

    if not arr.dtype.names:
        print("--data must be a structured array with named fields (--session-field requires one)")
        sys.exit(2)
    if args.session_field not in arr.dtype.names:
        print(f"Session field not found in data: {args.session_field}")
        sys.exit(2)

    dataset_fingerprint = partitioning.dataset_fingerprint(arr)
    session_ids = arr[args.session_field].tolist()

    ratios = {
        "discovery": float(args.discovery_ratio),
        "selection": float(args.selection_ratio),
        "locked_validation": float(args.locked_ratio),
        "final_holdout": float(args.holdout_ratio),
    }

    symbols = [s.strip() for s in str(args.symbols or "").split(",") if s.strip()]

    try:
        manager = partitioning.DatasetPartitionManager(
            session_ids=session_ids,
            dataset_fingerprint=dataset_fingerprint,
            embargo_rows=int(args.embargo_rows),
            manifest_dir=args.out_dir,
            ratios=ratios,
            random_seed=int(args.seed),
            symbols=symbols,
        )
    except partitioning.PartitionConfigError as exc:
        print(f"Invalid partition configuration: {exc}")
        sys.exit(2)

    try:
        manifest_path = manager.write_manifest(Path(args.out_dir) / "partition_manifest.json")
    except partitioning.PartitionConfigError as exc:
        print(f"Refusing to overwrite an immutable manifest: {exc}")
        sys.exit(2)

    payload: dict[str, Any] = {
        "partition_manifest_path": manifest_path,
        "manifest_hash": manager.manifest_hash,
        "dataset_fingerprint": dataset_fingerprint,
        "partitions": manager.partition_summary(),
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))


def cmd_alpha_mine_promote(args: argparse.Namespace) -> None:
    """SubHFT Alpha Mining v2 Phase 4: scaffold a ``research/alphas/<id>/`` package
    from a GP-discovered expression -- the intake path into the existing,
    unmodified Gate A/B/C alpha-governance pipeline (``research/combinatorial/
    promote.py``). Stops at ``status=DRAFT``; no gate runs automatically, same
    boundary ``hft alpha scaffold`` already has.
    """
    try:
        from research.combinatorial.promote import promote_candidate, promote_from_results
    except Exception as exc:
        print(f"Failed to import alpha promote module: {exc}")
        sys.exit(1)

    expression = getattr(args, "expression", None)
    from_results = getattr(args, "from_results", None)
    if bool(expression) == bool(from_results):
        print("Exactly one of --expression or --from-results is required")
        sys.exit(2)

    common_kwargs: dict[str, Any] = {
        "alpha_id": args.alpha_id,
        "owner": args.owner,
        "strategy_type": args.strategy_type,
        "instrument": args.instrument,
        "force": bool(args.force),
    }

    try:
        if expression:
            alpha_dir = promote_candidate(expression, **common_kwargs)
        else:
            rank = getattr(args, "rank", None)
            if rank is None:
                print("--rank is required with --from-results")
                sys.exit(2)
            alpha_dir = promote_from_results(str(from_results), int(rank), **common_kwargs)
    except FileExistsError as exc:
        print(f"[hft alpha mine promote] {exc}")
        sys.exit(2)
    except (ValueError, SyntaxError, IndexError, FileNotFoundError, KeyError) as exc:
        print(f"[hft alpha mine promote] {exc}")
        sys.exit(2)

    print(f"Promoted GP candidate to: {alpha_dir}")
    print("[INFO] Status: DRAFT -- no live registry entry yet.")
    print(f"  Next: review {alpha_dir}/manifest.yaml, then `hft alpha validate --alpha-id {args.alpha_id} ...`")


def cmd_alpha_mine_run(args: argparse.Namespace) -> None:
    """Run or resume the bounded SMMA mining workflow."""
    try:
        from research.combinatorial.smma_dataset import DatasetGovernanceError
        from research.combinatorial.smma_runner import RunConfig, RunIntegrityError, run_mining
        from research.combinatorial.tick_dataset import TickDatasetGovernanceError

        config = RunConfig(
            run_dir=Path(args.run_dir),
            family=str(args.family),
            wall_time_hours=float(args.wall_time_hours),
            max_candidates=int(args.max_candidates),
            workers=int(args.workers),
            seeds=tuple(int(seed) for seed in args.seeds),
            timeframes_minutes=tuple(int(value) for value in args.timeframes_minutes),
            smma_lengths=tuple(int(value) for value in args.smma_lengths),
            posthoc_diagnostic=bool(args.posthoc_diagnostic),
            resume=bool(args.resume),
            unlock_final_holdout=bool(getattr(args, "unlock_final_holdout", False)),
            dataset_cache_dir=(Path(args.dataset_cache_dir) if getattr(args, "dataset_cache_dir", None) else None),
            cost_mode=str(getattr(args, "cost_mode", "per_contract")),
        )
        report = run_mining(config)
    except (DatasetGovernanceError, TickDatasetGovernanceError, RunIntegrityError, ValueError, OSError) as exc:
        print(f"[hft alpha mine run] {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(report, indent=2, sort_keys=True))


def cmd_alpha_mine_status(args: argparse.Namespace) -> None:
    """Inspect manifest/checkpoint/heartbeat without changing a mining run."""
    try:
        from research.combinatorial.smma_runner import RunIntegrityError, mining_status

        payload = mining_status(args.run_dir)
    except (RunIntegrityError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"[hft alpha mine status] {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(payload, indent=2, sort_keys=True))


def _write_alpha_mining_campaign_report(
    campaign_dir: Path,
    campaign_id: str,
    outcomes: list[dict[str, Any]],
    minimum_days_for_promotion: int,
) -> dict[str, Any]:
    payload = {
        "schema": "alpha_mining_campaign.v3",
        "campaign_id": campaign_id,
        "screen_only": True,
        "execution_policy": "full_per_contract_when_cost_profiles_complete_else_bounded_root_proxy_diagnostic",
        "promotion_policy": (
            f"minimum_{minimum_days_for_promotion}_eligible_trading_days; independent_of_search_breadth"
        ),
        "final_holdout_unlocked": False,
        "legs": list(outcomes),
    }
    payload["campaign_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    report_path = campaign_dir / "campaign_report.json"
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=campaign_dir,
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(report_path)
    return payload


def cmd_alpha_mine_campaign(args: argparse.Namespace) -> None:
    """Run the six governed family/timeframe legs with resumable per-leg artifacts."""
    try:
        from research.combinatorial.smma_dataset import DatasetGovernanceError
        from research.combinatorial.smma_runner import (
            MIN_DAYS_FOR_PROMOTION,
            MiningRun,
            RunConfig,
            RunIntegrityError,
        )
        from research.combinatorial.tick_dataset import TickDatasetGovernanceError

        run_root = Path(args.run_root).resolve()
        campaign_id = str(args.campaign_id)
        if not campaign_id or Path(campaign_id).name != campaign_id:
            raise ValueError("campaign-id must be one filesystem-safe path component")
        if run_root in {Path("/"), Path.home().resolve(), Path.cwd().resolve()}:
            raise ValueError("run-root must be a dedicated artifact parent, not a broad filesystem root")
        campaign_dir = run_root / campaign_id
        campaign_dir.mkdir(parents=True, exist_ok=True)
        legs = [
            (family, label, timeframes)
            for family in ("bidask", "kbar", "tick")
            for label, timeframes in (("2m", (2,)), ("h1h2h4", (60, 120, 240)))
        ]
        outcomes: list[dict[str, Any]] = []
        for family, label, timeframes in legs:
            leg_name = f"{family}-{label}-{campaign_id}"
            leg_dir = campaign_dir / leg_name
            try:
                preflight_config = RunConfig(
                    run_dir=leg_dir,
                    family=family,
                    wall_time_hours=min(float(args.wall_time_hours), float(args.diagnostic_wall_time_hours)),
                    max_candidates=min(int(args.max_candidates), int(args.diagnostic_max_candidates)),
                    workers=int(args.workers),
                    seeds=tuple(int(seed) for seed in args.seeds),
                    timeframes_minutes=timeframes,
                    posthoc_diagnostic=True,
                    resume=False,
                    unlock_final_holdout=False,
                    dataset_cache_dir=campaign_dir / "dataset_cache",
                    cost_mode="root_proxy",
                )
                preflight = MiningRun(preflight_config)
                preflight.run_dir.mkdir(parents=True, exist_ok=True)
                dataset = preflight._load_or_export_dataset()
                preflight._validate_frozen_dataset_scope(dataset)
                sidecar = json.loads(Path(str(preflight.dataset_path) + ".meta.json").read_text(encoding="utf-8"))
                eligible_days = int(sidecar["trading_day_count"])
                cost_coverage = preflight._cost_profile_coverage(dataset)
                full_search_eligible = bool(cost_coverage["complete"])
                promotion_day_count_eligible = eligible_days >= MIN_DAYS_FOR_PROMOTION
                if full_search_eligible:
                    mode = "full" if promotion_day_count_eligible else "full_needs_more_days"
                else:
                    mode = "bounded_diagnostic"
                eligibility_reasons: list[str] = []
                if not promotion_day_count_eligible:
                    eligibility_reasons.append(f"eligible_trading_days={eligible_days}<{MIN_DAYS_FOR_PROMOTION}")
                if not full_search_eligible:
                    eligibility_reasons.append(f"missing_cost_profiles={cost_coverage['missing_contracts']}")
                config = RunConfig(
                    run_dir=leg_dir,
                    family=family,
                    wall_time_hours=(
                        float(args.wall_time_hours)
                        if full_search_eligible
                        else min(float(args.wall_time_hours), float(args.diagnostic_wall_time_hours))
                    ),
                    max_candidates=(
                        int(args.max_candidates)
                        if full_search_eligible
                        else min(int(args.max_candidates), int(args.diagnostic_max_candidates))
                    ),
                    workers=int(args.workers),
                    seeds=tuple(int(seed) for seed in args.seeds),
                    timeframes_minutes=timeframes,
                    posthoc_diagnostic=not full_search_eligible,
                    resume=bool(args.resume) and (leg_dir / "run_manifest.json").exists(),
                    unlock_final_holdout=False,
                    dataset_cache_dir=campaign_dir / "dataset_cache",
                    cost_mode="per_contract" if full_search_eligible else "root_proxy",
                )
                mining_run = MiningRun(config)
                mining_run._dataset_cache_evidence = dict(preflight._dataset_cache_evidence)
                report = mining_run.run()
                outcomes.append(
                    {
                        "leg": leg_name,
                        "run_dir": str(leg_dir),
                        "status": "complete",
                        "verdict": report.get("verdict"),
                        "report_hash": report.get("report_hash"),
                        "mode": mode,
                        "eligible_trading_days": eligible_days,
                        "minimum_days_for_promotion": MIN_DAYS_FOR_PROMOTION,
                        "promotion_day_count_eligible": promotion_day_count_eligible,
                        "full_search_eligible": full_search_eligible,
                        "cost_profile_coverage": cost_coverage,
                        "eligibility_reasons": eligibility_reasons,
                        "candidate_cap": config.max_candidates,
                        "wall_time_hours": config.wall_time_hours,
                        "cost_mode": config.cost_mode,
                        "cost_claim_eligible": report.get("cost_claim_eligible", False),
                    }
                )
            except (
                DatasetGovernanceError,
                TickDatasetGovernanceError,
                RunIntegrityError,
                ValueError,
                OSError,
            ) as exc:
                outcomes.append(
                    {
                        "leg": leg_name,
                        "run_dir": str(leg_dir),
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            payload = _write_alpha_mining_campaign_report(
                campaign_dir,
                campaign_id,
                outcomes,
                MIN_DAYS_FOR_PROMOTION,
            )
    except (ValueError, OSError) as exc:
        print(f"[hft alpha mine campaign] {exc}", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(payload, indent=2, sort_keys=True))
    if any(outcome["status"] == "failed" for outcome in outcomes):
        sys.exit(2)


def cmd_alpha_screen(args: argparse.Namespace) -> None:
    """Loose-mode alpha screening — stamps ``screen_only=true`` on the scorecard.

    A screen artifact is **not** promotion-eligible; ``hft alpha promote``
    rejects any scorecard with ``screen_only=true``. Use this for early
    triage during research; use ``hft alpha validate --profile strict``
    when an artifact must be eligible for Gate D.
    """
    try:
        from research.combinatorial.smma_screen import (
            SMMAScreenError,
            is_smma_screen_package,
            run_smma_screen,
        )
    except (ImportError, AttributeError) as exc:
        print(f"Failed to import SMMA screen adapter: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        if is_smma_screen_package(args.alpha_id):
            if len(args.data) != 1:
                raise SMMAScreenError("SMMA screen requires exactly one governed dataset")
            summary = run_smma_screen(
                alpha_id=str(args.alpha_id),
                data_path=str(args.data[0]),
                experiments_dir=str(getattr(args, "experiments_dir", "research/experiments")),
                skip_gate_b_tests=bool(getattr(args, "skip_gate_b_tests", False)),
            )
            if args.out:
                out_path = Path(args.out)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
            print(json.dumps(summary, indent=2, sort_keys=True))
            if not summary["passed"]:
                sys.exit(2)
            return
    except (SMMAScreenError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"[hft alpha screen smma] {exc}", file=sys.stderr)
        sys.exit(2)

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
    result = run_alpha_validation(config, gate_c_runtime=build_gate_c_runtime())
    summary = result.to_dict()

    try:
        scorecard_path = Path(result.scorecard_path)
        if scorecard_path.exists():
            payload = json.loads(scorecard_path.read_text())
            payload["screen_only"] = True
            payload["screen_profile"] = "loose_default"
            payload["screen_timestamp"] = _dt.datetime.now(tz=_dt.timezone.utc).isoformat()
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
    profile = _platform_alpha._load_strict_validation_profile(getattr(args, "profile", None))
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
    result = run_alpha_validation(config, gate_c_runtime=build_gate_c_runtime())
    summary = result.to_dict()
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not result.passed:
        sys.exit(2)


def cmd_alpha_validate_batch(args: argparse.Namespace) -> None:
    """Run Gate A-C validation across multiple alphas sequentially."""
    import time

    try:
        from hft_platform.alpha.validation import ValidationConfig, run_alpha_validation
    except Exception as exc:
        print(f"Failed to import alpha validation pipeline: {exc}")
        sys.exit(1)

    try:
        from hft_platform.alpha.discovery import AlphaDiscoveryRegistry
    except Exception as exc:
        print(f"Failed to import alpha discovery registry: {exc}")
        sys.exit(1)

    alphas_dir = getattr(args, "alphas_dir", "research/alphas")
    registry = AlphaDiscoveryRegistry()
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
            result = run_alpha_validation(config, gate_c_runtime=build_gate_c_runtime())
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


def cmd_alpha_pipeline_run(args: argparse.Namespace) -> None:
    from research.pipeline import _run_pipeline

    code = int(_run_pipeline(args, mode="strict"))
    if code != 0:
        sys.exit(code)


def cmd_alpha_pipeline_triage(args: argparse.Namespace) -> None:
    import os

    from research.pipeline import _run_pipeline

    if os.environ.get("HFT_RESEARCH_ALLOW_TRIAGE", "0") != "1":
        print(
            "[hft alpha pipeline triage] disabled by default. "
            "Set HFT_RESEARCH_ALLOW_TRIAGE=1 to acknowledge non-promotable bypass mode.",
            file=sys.stderr,
        )
        sys.exit(2)
    code = int(_run_pipeline(args, mode="triage"))
    if code != 0:
        sys.exit(code)


# (alpha_cmd, nested subcommand or None) -> command body. Every command the
# platform parser marks as forwarded has exactly one entry
# (tests/unit/research/test_alpha_cli.py checks both directions).
HANDLERS: dict[tuple[str, str | None], Callable[[argparse.Namespace], None]] = {
    ("scaffold", None): cmd_alpha_scaffold,
    ("search", None): cmd_alpha_search,
    ("mine", "init"): cmd_alpha_mine_init,
    ("mine", "run"): cmd_alpha_mine_run,
    ("mine", "status"): cmd_alpha_mine_status,
    ("mine", "campaign"): cmd_alpha_mine_campaign,
    ("mine", "promote"): cmd_alpha_mine_promote,
    ("screen", None): cmd_alpha_screen,
    ("validate", None): cmd_alpha_validate,
    ("validate-batch", None): cmd_alpha_validate_batch,
    ("pipeline", "run"): cmd_alpha_pipeline_run,
    ("pipeline", "triage"): cmd_alpha_pipeline_triage,
}

_NESTED_DEST = {"mine": "mine_cmd", "pipeline": "alpha_pipeline_cmd"}


def handler_for(args: argparse.Namespace) -> Callable[[argparse.Namespace], None] | None:
    """The command body for a namespace parsed by the platform ``hft`` parser."""
    cmd = getattr(args, "alpha_cmd", None)
    nested = getattr(args, _NESTED_DEST[cmd], None) if cmd in _NESTED_DEST else None
    return HANDLERS.get((str(cmd), nested))


def main(argv: list[str]) -> int:
    """Run ``hft alpha <argv>`` for a forwarded research subcommand."""
    from hft_platform.cli._parser import build_parser
    from hft_platform.utils.logging import configure_logging

    parser = build_parser()
    args = parser.parse_args(["alpha", *argv])
    handler = handler_for(args)
    if handler is None:
        known = ", ".join(sorted(" ".join(p for p in key if p) for key in HANDLERS))
        print(f"`python -m research alpha` runs only the research subcommands: {known}", file=sys.stderr)
        return 2
    configure_logging()
    try:
        handler(args)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return 1
    return 0
