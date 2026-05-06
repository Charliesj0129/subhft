"""Argument parser construction with all subparser definitions."""

from __future__ import annotations

import argparse
import os

from ._alpha import (
    cmd_alpha_ab_compare,
    cmd_alpha_batch_correlation,
    cmd_alpha_canary_auto_evaluate,
    cmd_alpha_canary_evaluate,
    cmd_alpha_canary_status,
    cmd_alpha_cluster,
    cmd_alpha_experiments_best,
    cmd_alpha_experiments_compare,
    cmd_alpha_experiments_list,
    cmd_alpha_kill,
    cmd_alpha_list,
    cmd_alpha_paper_trade_batch,
    cmd_alpha_pool,
    cmd_alpha_promote,
    cmd_alpha_promote_batch,
    cmd_alpha_rl_promote,
    cmd_alpha_scaffold,
    cmd_alpha_screen,
    cmd_alpha_search,
    cmd_alpha_validate,
    cmd_alpha_validate_batch,
)
from ._feasibility import cmd_feasibility_report
from ._feature import (
    cmd_feature_preflight,
    cmd_feature_profiles,
    cmd_feature_rollout_rollback,
    cmd_feature_rollout_set,
    cmd_feature_rollout_status,
    cmd_feature_validate,
)
from ._golive import cmd_golive_check
from ._health import cmd_health_preflight
from ._ops import (
    cmd_backtest,
    cmd_contracts_status,
    cmd_diag,
    cmd_feed_status,
    cmd_ops_autonomy_status,
    cmd_ops_flatten,
    cmd_ops_rearm_platform,
    cmd_ops_rearm_strategy,
    cmd_recorder_status,
    cmd_strat_test,
)
from ._risk import cmd_risk_halt, cmd_risk_resume, cmd_risk_status
from ._run import cmd_check, cmd_init, cmd_run, cmd_wizard
from ._symbols import (
    cmd_resolve_symbols,
    cmd_symbols_build,
    cmd_symbols_preview,
    cmd_symbols_sync,
    cmd_symbols_validate,
)
from ._tca import cmd_tca_daily


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hft", description="HFT Platform CLI")
    sub = parser.add_subparsers(dest="command")

    # ... (Previous commands)

    config_cmd = sub.add_parser("config", help="Configuration utilities")
    config_sub = config_cmd.add_subparsers(dest="config_cmd")

    resolve = config_sub.add_parser("resolve", help="Resolve exchanges for symbols")
    resolve.add_argument("symbols", nargs="+", help="List of stock codes")
    resolve.add_argument("--output", help="Output YAML file path")
    resolve.set_defaults(func=cmd_resolve_symbols)

    build = config_sub.add_parser("build", help="Build symbols.yaml from symbols.list")
    build.add_argument("--list", dest="list_path", default="config/symbols.list", help="Input symbols list")
    build.add_argument("--output", default="config/symbols.yaml", help="Output symbols YAML")
    build.add_argument("--contracts", default="config/contracts.json", help="Contract cache path")
    build.add_argument("--metrics", default=None, help="Metrics cache path (optional)")
    build.add_argument("--no-contracts", action="store_true", help="Skip contract cache lookup")
    build.add_argument(
        "--max-subscriptions",
        type=int,
        default=None,
        help=(
            "Subscription limit. Default: 8 when --loop is set, otherwise 480 "
            "(4 conn × 120 codes; each code = 2 broker topics)."
        ),
    )
    build.add_argument(
        "--loop",
        dest="loop_id",
        help="Bind a production loop (loop_v1) for live-minimal subscription cap.",
    )
    build.add_argument("--preview", action="store_true", help="Show preview summary")
    build.add_argument("--sample", type=int, default=10, help="Preview sample size")
    build.set_defaults(func=cmd_symbols_build)

    preview = config_sub.add_parser("preview", help="Preview expanded symbols list")
    preview.add_argument("--list", dest="list_path", default="config/symbols.list", help="Input symbols list")
    preview.add_argument("--contracts", default="config/contracts.json", help="Contract cache path")
    preview.add_argument("--metrics", default=None, help="Metrics cache path (optional)")
    preview.add_argument("--no-contracts", action="store_true", help="Skip contract cache lookup")
    preview.add_argument(
        "--max-subscriptions",
        type=int,
        default=None,
        help=(
            "Subscription limit. Default: 8 when --loop is set, otherwise 480 "
            "(4 conn × 120 codes; each code = 2 broker topics)."
        ),
    )
    preview.add_argument(
        "--loop",
        dest="loop_id",
        help="Bind a production loop (loop_v1) for live-minimal subscription cap.",
    )
    preview.add_argument("--sample", type=int, default=10, help="Preview sample size")
    preview.set_defaults(func=cmd_symbols_preview)

    validate = config_sub.add_parser("validate", help="Validate symbols configuration")
    validate.add_argument("--list", dest="list_path", default="config/symbols.list", help="Input symbols list")
    validate.add_argument("--symbols", dest="symbols_path", help="Validate an existing symbols.yaml")
    validate.add_argument("--contracts", default="config/contracts.json", help="Contract cache path")
    validate.add_argument("--metrics", default=None, help="Metrics cache path (optional)")
    validate.add_argument("--no-contracts", action="store_true", help="Skip contract cache lookup")
    validate.add_argument("--online", action="store_true", help="Validate against broker contracts")
    validate.add_argument(
        "--max-subscriptions",
        type=int,
        default=None,
        help=(
            "Subscription limit. Default: 8 when --loop is set, otherwise 480 "
            "(4 conn × 120 codes; each code = 2 broker topics)."
        ),
    )
    validate.add_argument(
        "--loop",
        dest="loop_id",
        help="Bind a production loop (loop_v1) for live-minimal subscription cap.",
    )
    validate.set_defaults(func=cmd_symbols_validate)

    sync = config_sub.add_parser("sync", help="Sync broker contracts and build symbols.yaml")
    sync.add_argument("--list", dest="list_path", default="config/symbols.list", help="Input symbols list")
    sync.add_argument("--output", default="config/symbols.yaml", help="Output symbols YAML")
    sync.add_argument("--contracts", default="config/contracts.json", help="Contract cache path")
    sync.add_argument("--metrics", default=None, help="Metrics cache path (optional)")
    sync.add_argument(
        "--max-subscriptions",
        type=int,
        default=None,
        help=(
            "Subscription limit. Default: 8 when --loop is set, otherwise 480 "
            "(4 conn × 120 codes; each code = 2 broker topics)."
        ),
    )
    sync.add_argument(
        "--loop",
        dest="loop_id",
        help="Bind a production loop (loop_v1) for live-minimal subscription cap.",
    )
    sync.add_argument("--preview", action="store_true", help="Show preview summary")
    sync.add_argument("--sample", type=int, default=10, help="Preview sample size")
    sync.set_defaults(func=cmd_symbols_sync)

    contracts_status = config_sub.add_parser("contracts-status", help="Inspect contract cache freshness/version")
    contracts_status.add_argument("--contracts", default="config/contracts.json", help="Contract cache path")
    contracts_status.add_argument("--stale-after-s", type=float, default=86400.0, help="Staleness threshold seconds")
    contracts_status.add_argument(
        "--status-file",
        default=os.getenv("HFT_CONTRACT_REFRESH_STATUS_PATH", "outputs/contract_refresh_status.json"),
        help="Optional runtime status snapshot file path",
    )
    contracts_status.set_defaults(func=cmd_contracts_status)

    run = sub.add_parser("run", help="Run pipeline (sim|live|replay)")
    run.add_argument("mode", nargs="?", choices=["sim", "live", "replay"])
    run.add_argument("--mode", dest="mode_flag", choices=["sim", "live", "replay"])
    run.add_argument(
        "--loop",
        dest="loop_id",
        help=(
            "Bind a production loop (loop_v1). Reads config/loops/<loop>.yaml, "
            "forces strategy + broker, and switches schema to strict mode. "
            "Mutually exclusive with --strategy."
        ),
    )
    run.add_argument("--strategy", help="Strategy id to run (legacy; rejected with --loop)")
    run.add_argument("--strategy-module", help="Override strategy module")
    run.add_argument("--strategy-class", help="Override strategy class")
    run.add_argument("--symbols", nargs="+", help="Symbols to load")
    run.add_argument(
        "--session",
        dest="session",
        help="Replay-mode trading session (YYYY-MM-DD). Required for --mode replay.",
    )
    run.add_argument(
        "--fixture",
        dest="fixture",
        help="Replay-mode WAL fixture archive (.tar.gz). Required for --mode replay.",
    )
    run.add_argument(
        "--allow-pre-recorder",
        dest="allow_pre_recorder",
        action="store_true",
        default=False,
        help=(
            "Replay-mode: opt in to running against a session that predates the "
            "intent recorder (HFT_INTENT_RECORDER_ENABLED=0). Match pct is null."
        ),
    )
    run.set_defaults(func=cmd_run)

    init = sub.add_parser("init", help="Generate settings and strategy skeleton")
    init.add_argument("--strategy-id", help="Strategy id/name")
    init.add_argument("--symbol", help="Primary symbol")
    init.set_defaults(func=cmd_init)

    check = sub.add_parser("check", help="Validate settings")
    check.add_argument("--export", choices=["yaml", "json"], help="Export effective settings")
    check.set_defaults(func=cmd_check)

    wizard = sub.add_parser("wizard", help="Interactive configuration setup")
    wizard.set_defaults(func=cmd_wizard)

    feed = sub.add_parser("feed", help="Feed utilities")
    feed_sub = feed.add_subparsers(dest="feed_cmd")
    feed_status = feed_sub.add_parser("status", help="Check feed metrics")
    feed_status.add_argument("--port", type=int, default=9090)
    feed_status.set_defaults(func=cmd_feed_status)

    diag = sub.add_parser("diag", help="Quick diagnostics")
    diag.add_argument("--trace-file", help="Decision trace JSONL file to inspect")
    diag.add_argument("--trace-id", help="Filter by trace_id")
    diag.add_argument("--stage", help="Filter by stage")
    diag.add_argument("--limit", type=int, default=20, help="Show last N matching records")
    diag.add_argument("--timeline", action="store_true", help="Render ordered incident timeline from traces")
    diag.add_argument(
        "--timeline-format",
        choices=["json", "md"],
        default="json",
        help="Timeline output format (used with --timeline)",
    )
    diag.add_argument("--out", help="Write diag/timeline output to file")
    diag.set_defaults(func=cmd_diag)

    feature = sub.add_parser("feature", help="Feature Plane governance utilities")
    feature_sub = feature.add_subparsers(dest="feature_cmd")

    feat_profiles = feature_sub.add_parser("profiles", help="List feature profiles")
    feat_profiles.add_argument("--path", help="Feature profiles YAML path")
    feat_profiles.add_argument("--json", action="store_true", help="Output JSON")
    feat_profiles.set_defaults(func=cmd_feature_profiles)

    feat_validate = feature_sub.add_parser("validate", help="Validate feature profiles and apply active profile")
    feat_validate.add_argument("--path", help="Feature profiles YAML path")
    feat_validate.set_defaults(func=cmd_feature_validate)

    feat_preflight = feature_sub.add_parser("preflight", help="Check strategy/feature compatibility")
    feat_preflight.add_argument("--profiles", help="Feature profiles YAML path")
    feat_preflight.add_argument("--strategies", default="config/live/strategies.yaml", help="Strategy config YAML")
    feat_preflight.set_defaults(func=cmd_feature_preflight)

    feat_rollout_status = feature_sub.add_parser("rollout-status", help="Inspect local feature rollout state")
    feat_rollout_status.add_argument("--profiles", help="Feature profiles YAML path")
    feat_rollout_status.add_argument("--state-path", help="Rollout state JSON path")
    feat_rollout_status.add_argument("--feature-set", help="Filter by feature_set_id")
    feat_rollout_status.set_defaults(func=cmd_feature_rollout_status)

    feat_rollout_set = feature_sub.add_parser("rollout-set", help="Set local feature rollout state/profile")
    feat_rollout_set.add_argument("--profiles", help="Feature profiles YAML path")
    feat_rollout_set.add_argument("--state-path", help="Rollout state JSON path")
    feat_rollout_set.add_argument("--feature-set", required=True, help="Feature set id")
    feat_rollout_set.add_argument("--state", required=True, choices=["active", "shadow", "disabled"])
    feat_rollout_set.add_argument("--profile-id", help="Profile id (required for active; shadow profile for shadow)")
    feat_rollout_set.add_argument("--actor", default="cli")
    feat_rollout_set.add_argument("--notes", default="")
    feat_rollout_set.set_defaults(func=cmd_feature_rollout_set)

    feat_rollout_rb = feature_sub.add_parser(
        "rollout-rollback", help="Rollback local feature rollout to previous active"
    )
    feat_rollout_rb.add_argument("--state-path", help="Rollout state JSON path")
    feat_rollout_rb.add_argument("--feature-set", required=True, help="Feature set id")
    feat_rollout_rb.add_argument("--actor", default="cli")
    feat_rollout_rb.add_argument("--notes", default="rollback")
    feat_rollout_rb.set_defaults(func=cmd_feature_rollout_rollback)

    strat = sub.add_parser("strat", help="Strategy helpers")
    strat_sub = strat.add_subparsers(dest="strat_cmd")
    strat_test = strat_sub.add_parser("test", help="Run a synthetic smoke test for strategy")
    strat_test.add_argument("--strategy-id", help="Strategy id")
    strat_test.add_argument("--module", help="Strategy module path")
    strat_test.add_argument("--cls", help="Strategy class name")
    strat_test.add_argument("--symbol", help="Symbol to test")
    strat_test.set_defaults(func=cmd_strat_test)

    backtest = sub.add_parser("backtest", help="Backtest utilities (convert/run)")
    back_sub = backtest.add_subparsers(dest="backtest_cmd")

    back_convert = back_sub.add_parser("convert", help="Convert JSONL feed to hftbacktest npz")
    back_convert.add_argument("--input", required=True, help="Input JSONL (our normalized events)")
    back_convert.add_argument("--output", required=True, help="Output npz path")
    back_convert.add_argument("--scale", type=int, default=10000, help="Price scale (default 10000)")
    back_convert.set_defaults(func=cmd_backtest)

    back_run = back_sub.add_parser("run", help="Run backtest using hftbacktest")
    back_run.add_argument("--data", nargs="+", required=True, help="NPZ paths containing hftbacktest event data")
    back_run.add_argument("--tick-size", type=float, default=0.01, help="Tick size")
    back_run.add_argument("--lot-size", type=float, default=1.0, help="Lot size")
    back_run.add_argument("--tick-sizes", nargs="+", type=float, help="Tick sizes per asset (align with data)")
    back_run.add_argument("--lot-sizes", nargs="+", type=float, help="Lot sizes per asset (align with data)")
    back_run.add_argument("--symbols", nargs="+", help="Symbols per asset (align with data)")
    back_run.add_argument("--record-out", help="Path to save recorder output npz")
    back_run.add_argument("--strategy-module", help="Strategy module path for adapter")
    back_run.add_argument("--strategy-class", help="Strategy class for adapter")
    back_run.add_argument("--strategy-id", help="Strategy id", default="demo")
    back_run.add_argument("--symbol", help="Symbol", default="2330")
    back_run.add_argument("--price-scale", type=int, default=10000, help="Price scale used by strategy ints")
    back_run.add_argument("--timeout", type=int, default=0, help="wait_next_feed timeout (0 = no timeout)")
    back_run.add_argument("--latency-entry", type=float, help="Order entry latency for backtest")
    back_run.add_argument("--latency-resp", type=float, help="Order response latency for backtest")
    back_run.add_argument("--fee-maker", type=float, help="Maker fee (per value, negative for rebate)")
    back_run.add_argument("--fee-taker", type=float, help="Taker fee (per value, negative for rebate)")
    back_run.add_argument("--seed", type=int, default=42, help="Deterministic random seed")
    back_run.add_argument(
        "--no-partial-fill", action="store_true", help="Disable partial fill (use no_partial_fill_exchange)"
    )
    back_run.add_argument(
        "--strict-equity", action="store_true", help="Fail run if real equity extraction is unavailable"
    )
    back_run.add_argument("--report", action="store_true", help="Generate HTML Tearsheet")
    back_run.set_defaults(func=cmd_backtest)

    recorder = sub.add_parser("recorder", help="Recorder utilities")
    recorder_sub = recorder.add_subparsers(dest="recorder_cmd")
    recorder_status = recorder_sub.add_parser("status", help="Show recorder WAL backlog and ClickHouse status")
    recorder_status.add_argument("--wal-dir", help="Override WAL directory path")
    recorder_status.add_argument("--ck-host", help="Override ClickHouse host")
    recorder_status.set_defaults(func=cmd_recorder_status)

    ops = sub.add_parser("ops", help="Autonomy control-plane utilities")
    ops_sub = ops.add_subparsers(dest="ops_cmd")

    ops_rearm_strategy = ops_sub.add_parser("rearm-strategy", help="Clear manual re-arm for one strategy")
    ops_rearm_strategy.add_argument("--strategy-id", required=True, help="Strategy id to re-arm")
    ops_rearm_strategy.add_argument("--state-path", help="Override autonomy runtime state path")
    ops_rearm_strategy.set_defaults(func=cmd_ops_rearm_strategy)

    ops_rearm_platform = ops_sub.add_parser("rearm-platform", help="Clear platform manual re-arm state")
    ops_rearm_platform.add_argument("--state-path", help="Override autonomy runtime state path")
    ops_rearm_platform.set_defaults(func=cmd_ops_rearm_platform)

    ops_status = ops_sub.add_parser("autonomy-status", help="Show persisted autonomy runtime state")
    ops_status.add_argument("--state-path", help="Override autonomy runtime state path")
    ops_status.set_defaults(func=cmd_ops_autonomy_status)

    ops_flatten = ops_sub.add_parser("flatten", help="Emergency position flattening")
    ops_flatten.add_argument("--scope", choices=["all", "strategy", "track"], default="all")
    ops_flatten.add_argument("--scope-id", default=None, help="Strategy ID or track name")
    ops_flatten.add_argument("--deadline", type=int, default=120, help="Flatten deadline in seconds")
    ops_flatten.set_defaults(func=cmd_ops_flatten)

    # ── Risk Management ────────────────────────────────────────────────
    risk = sub.add_parser("risk", help="Risk management utilities")
    risk_sub = risk.add_subparsers(dest="risk_cmd")

    risk_halt = risk_sub.add_parser("halt", help="Activate kill switch (halt all orders)")
    risk_halt.add_argument("--reason", required=True, help="Reason for halting")
    risk_halt.set_defaults(func=cmd_risk_halt)

    risk_resume = risk_sub.add_parser("resume", help="Deactivate kill switch")
    risk_resume.set_defaults(func=cmd_risk_resume)

    risk_status_cmd = risk_sub.add_parser("status", help="Check kill switch status")
    risk_status_cmd.set_defaults(func=cmd_risk_status)

    # ── Health Preflight ───────────────────────────────────────────────
    health = sub.add_parser("health", help="Health check utilities")
    health_sub = health.add_subparsers(dest="health_cmd")

    health_preflight = health_sub.add_parser("preflight", help="Run pre-trading health checks")
    health_preflight.add_argument("--timeout", type=float, default=5.0, help="HTTP check timeout seconds")
    health_preflight.add_argument("--json", action="store_true", help="Output as JSON")
    health_preflight.set_defaults(func=cmd_health_preflight)

    # ── Go-Live Checklist ──────────────────────────────────────────────
    golive = sub.add_parser("golive", help="Go-live checklist utilities")
    golive_sub = golive.add_subparsers(dest="golive_cmd")

    golive_check = golive_sub.add_parser("check", help="Run go-live checklist")
    golive_check.add_argument("--skip", nargs="*", default=[], help="Checks to skip")
    golive_check.add_argument("--json", action="store_true", help="Output as JSON")
    golive_check.set_defaults(func=cmd_golive_check)

    alpha = sub.add_parser("alpha", help="Alpha research pipeline utilities")
    alpha_sub = alpha.add_subparsers(dest="alpha_cmd")

    alpha_list = alpha_sub.add_parser("list", help="List discovered research alphas")
    alpha_list.set_defaults(func=cmd_alpha_list)

    alpha_scaffold = alpha_sub.add_parser("scaffold", help="Scaffold a new research alpha artifact")
    alpha_scaffold.add_argument("alpha_id", help="Immutable alpha id (e.g. ofi_mc_v2)")
    alpha_scaffold.add_argument("--paper", action="append", default=[], help="Paper reference (repeatable)")
    alpha_scaffold.add_argument("--complexity", default="O1", help="Complexity target, e.g. O1 or ON")
    alpha_scaffold.add_argument("--force", action="store_true", help="Overwrite existing files")
    alpha_scaffold.set_defaults(func=cmd_alpha_scaffold)

    alpha_search = alpha_sub.add_parser("search", help="Run combinatorial alpha search")
    alpha_search.add_argument("--mode", choices=["random", "template", "genetic"], default="random")
    alpha_search.add_argument("--data", required=True, help="Input npy/npz data path")
    alpha_search.add_argument("--feature-fields", required=True, help="Comma-separated feature field names")
    alpha_search.add_argument("--returns-field", help="Structured array field name for forward returns")
    alpha_search.add_argument("--trials", type=int, default=100, help="Random search trials")
    alpha_search.add_argument("--template", help="Template expression for template mode")
    alpha_search.add_argument(
        "--grid",
        help="Template parameter grid, e.g. 'w=5,10,20;lag=1,2'",
    )
    alpha_search.add_argument("--population", type=int, default=40, help="Genetic search population")
    alpha_search.add_argument("--generations", type=int, default=10, help="Genetic search generations")
    alpha_search.add_argument("--seed", type=int, default=42, help="Random seed")
    alpha_search.add_argument("--top", type=int, default=10, help="Top-N results in output")
    alpha_search.add_argument("--save-results", help="Optional path to persist result artifacts")
    alpha_search.add_argument("--out", help="Optional JSON output path")
    alpha_search.set_defaults(func=cmd_alpha_search)

    alpha_validate = alpha_sub.add_parser("validate", help="Run alpha validation pipeline (Gate A-C)")
    alpha_validate.add_argument("--alpha-id", required=True, help="Alpha id under research/alphas")
    alpha_validate.add_argument("--data", nargs="+", required=True, help="npy/npz path(s) for validation")
    alpha_validate.add_argument("--is-oos-split", type=float, default=0.7, help="IS ratio for temporal split")
    alpha_validate.add_argument("--signal-threshold", type=float, default=0.3, help="Signal threshold")
    alpha_validate.add_argument("--max-position", type=int, default=5, help="Max absolute position")
    alpha_validate.add_argument("--min-sharpe-oos", type=float, default=0.0, help="Gate C minimum OOS Sharpe")
    alpha_validate.add_argument("--max-abs-drawdown", type=float, default=0.3, help="Gate C max absolute drawdown")
    alpha_validate.add_argument(
        "--min-turnover",
        type=float,
        default=1e-6,
        help="Gate C minimum turnover floor to reject zero-trade runs",
    )
    alpha_validate.add_argument(
        "--latency-profile-id",
        default="sim_p95_v2026-02-26",
        help="Latency profile id recorded in Gate C artifacts",
    )
    alpha_validate.add_argument(
        "--local-decision-pipeline-latency-us",
        type=int,
        default=250,
        help="Local decision path latency (microseconds)",
    )
    alpha_validate.add_argument(
        "--submit-ack-latency-ms",
        type=float,
        default=36.0,
        help="Broker submit ACK latency (milliseconds)",
    )
    alpha_validate.add_argument(
        "--modify-ack-latency-ms",
        type=float,
        default=43.0,
        help="Broker modify ACK latency (milliseconds)",
    )
    alpha_validate.add_argument(
        "--cancel-ack-latency-ms",
        type=float,
        default=47.0,
        help="Broker cancel ACK latency (milliseconds)",
    )
    alpha_validate.add_argument(
        "--live-uplift-factor",
        type=float,
        default=1.5,
        help="Multiplier applied to broker ACK latencies",
    )
    alpha_validate.add_argument("--maker-fee-bps", type=float, default=-0.2, help="Maker fee in bps for Gate C")
    alpha_validate.add_argument("--taker-fee-bps", type=float, default=0.2, help="Taker fee in bps for Gate C")
    alpha_validate.add_argument(
        "--stat-pvalue-threshold",
        type=float,
        default=0.1,
        help="P-value threshold for OOS statistical significance tests",
    )
    alpha_validate.add_argument(
        "--min-stat-tests-pass",
        type=int,
        default=2,
        help="Minimum number of significance tests that must pass",
    )
    alpha_validate.add_argument(
        "--bootstrap-samples",
        type=int,
        default=1000,
        help="Bootstrap sample count for OOS mean-return confidence interval",
    )
    alpha_validate.add_argument(
        "--stress-latency-multiplier",
        type=float,
        default=1.5,
        help="Multiplier for latency assumptions in stress backtest",
    )
    alpha_validate.add_argument(
        "--stress-fee-multiplier",
        type=float,
        default=1.5,
        help="Multiplier for maker/taker fees in stress backtest",
    )
    alpha_validate.add_argument(
        "--min-stress-sharpe-ratio",
        type=float,
        default=0.5,
        help="Required stress_sharpe_oos / base_sharpe_oos ratio when base Sharpe > 0",
    )
    alpha_validate.add_argument(
        "--stress-drawdown-limit-multiplier",
        type=float,
        default=1.25,
        help="Multiplier for max_abs_drawdown in stress scenario",
    )
    alpha_validate.add_argument(
        "--require-paper-refs",
        action="store_true",
        help="Gate A: require non-empty manifest paper_refs",
    )
    alpha_validate.add_argument(
        "--require-paper-index-link",
        action="store_true",
        help="Gate A: require manifest paper_refs mapped in paper_index and linked to alpha_id",
    )
    alpha_validate.add_argument(
        "--enforce-data-governance",
        action="store_true",
        help="Gate A: enforce allowed dataset roots",
    )
    alpha_validate.add_argument(
        "--require-data-meta",
        action="store_true",
        help="Gate A: require sidecar metadata for each dataset (.meta.json)",
    )
    alpha_validate.add_argument(
        "--allowed-data-roots",
        nargs="+",
        default=[
            "research/data/raw",
            "research/data/interim",
            "research/data/processed",
            "research/data/hbt_multiproduct",
        ],
        help="Allowed dataset roots when --enforce-data-governance is enabled",
    )
    alpha_validate.add_argument("--skip-gate-b-tests", action="store_true", help="Skip per-alpha pytest in Gate B")
    alpha_validate.add_argument("--pytest-timeout", type=int, default=300, help="Gate B timeout in seconds")
    alpha_validate.add_argument(
        "--experiments-dir",
        default="research/experiments",
        help="Directory to store experiment run artifacts",
    )
    alpha_validate.add_argument(
        "--profile",
        default=None,
        help=(
            "REQUIRED: strict validation profile (e.g. vm_ul6_strict). "
            "Loose runs must use `hft alpha screen` instead — `validate` is "
            "promotion-eligible only and refuses non-strict profiles."
        ),
    )
    alpha_validate.add_argument("--out", help="Optional summary JSON output path")
    alpha_validate.set_defaults(func=cmd_alpha_validate)

    # L6: loose-mode screening — produces a non-promotion-eligible artifact.
    alpha_screen = alpha_sub.add_parser(
        "screen",
        help=(
            "Run loose alpha screening (Gate A-C with default thresholds); "
            "stamps screen_only=true on the scorecard so promotion refuses it."
        ),
    )
    alpha_screen.add_argument("--alpha-id", required=True, help="Alpha id under research/alphas")
    alpha_screen.add_argument("--data", nargs="+", required=True, help="npy/npz path(s) for screening")
    alpha_screen.add_argument("--is-oos-split", type=float, default=0.7)
    alpha_screen.add_argument("--signal-threshold", type=float, default=0.3)
    alpha_screen.add_argument("--max-position", type=int, default=5)
    alpha_screen.add_argument("--min-sharpe-oos", type=float, default=0.0)
    alpha_screen.add_argument("--max-abs-drawdown", type=float, default=0.3)
    alpha_screen.add_argument("--skip-gate-b-tests", action="store_true")
    alpha_screen.add_argument("--pytest-timeout", type=int, default=300)
    alpha_screen.add_argument("--experiments-dir", default="research/experiments", help="Experiment base directory")
    alpha_screen.add_argument("--out", help="Optional summary JSON output path")
    alpha_screen.set_defaults(func=cmd_alpha_screen)

    alpha_vb = alpha_sub.add_parser("validate-batch", help="Run Gate A-C across multiple alphas")
    alpha_vb.add_argument("--data", nargs="+", required=True, help="npy/npz path(s)")
    alpha_vb.add_argument("--alpha-ids", nargs="*", help="Specific alpha IDs (default: all)")
    alpha_vb.add_argument("--gates", default="ABC", help="Gates: A, AB, or ABC")
    alpha_vb.add_argument("--min-sharpe-oos", type=float, default=0.0)
    alpha_vb.add_argument("--max-abs-drawdown", type=float, default=0.3)
    alpha_vb.add_argument("--experiments-dir", default="research/experiments")
    alpha_vb.add_argument("--alphas-dir", default="research/alphas")
    alpha_vb.add_argument("--fail-fast", action="store_true", help="Stop on first failure")
    alpha_vb.add_argument("--out", help="Batch report JSON path")
    alpha_vb.set_defaults(func=cmd_alpha_validate_batch)

    # ── Slice-D Task 11: cheap pre-screener ────────────────────────────
    alpha_screen = alpha_sub.add_parser(
        "screen",
        help="Cheap screener (IC + turnover + cost-floor gate)",
    )
    alpha_screen.add_argument("alpha_id", help="Alpha id under research/alphas")
    alpha_screen.add_argument(
        "--project-root",
        default=".",
        help="Repo root for resolving research/alphas (default: cwd)",
    )
    alpha_screen.add_argument(
        "--threshold-ic",
        type=float,
        default=None,
        help="IC abs minimum override (default: module constant 0.005)",
    )
    alpha_screen.add_argument(
        "--threshold-turnover",
        type=float,
        default=None,
        help="Turnover kill threshold override (default: module constant 2.0)",
    )
    alpha_screen.add_argument(
        "--write-kill",
        action="store_true",
        help="On verdict='kill', append a gate='pre_screen' kill ledger row",
    )
    alpha_screen.set_defaults(func=cmd_alpha_screen)

    # ── Slice-D Task 12: manual kill ledger entry ──────────────────────
    alpha_kill = alpha_sub.add_parser(
        "kill",
        help="Manually record a kill in the kill ledger",
    )
    alpha_kill.add_argument("alpha_id", help="Alpha id to kill")
    alpha_kill.add_argument(
        "--reason",
        required=True,
        help="Kill reason (must be non-empty / non-whitespace)",
    )
    alpha_kill.add_argument(
        "--gate",
        default="manual",
        choices=["A", "B", "C", "D", "E", "F", "pre_screen", "cluster", "manual"],
        help="Kill gate (default: manual)",
    )
    alpha_kill.add_argument(
        "--killed-by",
        default="operator",
        help="Operator identifier for audit trail (default: operator)",
    )
    alpha_kill.set_defaults(func=cmd_alpha_kill)

    # ── Slice-D Task 13: hierarchical clustering ───────────────────────
    alpha_cluster = alpha_sub.add_parser(
        "cluster",
        help="Hierarchical correlation clustering across alphas",
    )
    alpha_cluster.add_argument(
        "--base-dir",
        default="research/experiments",
        help="Experiment base directory (default: research/experiments)",
    )
    alpha_cluster.add_argument(
        "--threshold",
        type=float,
        default=0.7,
        help="Absolute-correlation cutoff (default: 0.7)",
    )
    alpha_cluster.add_argument(
        "--metric",
        default="pearson",
        choices=["pearson", "spearman"],
        help="Correlation metric (default: pearson)",
    )
    alpha_cluster.add_argument(
        "--write-artifact",
        action="store_true",
        help="Persist results to research/alphas/_cluster_assignments.json",
    )
    alpha_cluster.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output instead of an aligned text table",
    )
    alpha_cluster.set_defaults(func=cmd_alpha_cluster)

    alpha_promote = alpha_sub.add_parser("promote", help="Run promotion pipeline (Gate D-E) and write canary config")
    alpha_promote.add_argument("--alpha-id", required=True, help="Alpha id under research/alphas")
    alpha_promote.add_argument("--owner", required=True, help="Promotion owner")
    alpha_promote.add_argument("--scorecard", help="Optional scorecard path override")
    alpha_promote.add_argument("--experiments-dir", default="research/experiments", help="Experiment base directory")
    alpha_promote.add_argument("--shadow-sessions", type=int, default=0, help="Observed shadow sessions")
    alpha_promote.add_argument("--min-shadow-sessions", type=int, default=5, help="Required shadow sessions for Gate E")
    alpha_promote.add_argument("--drift-alerts", type=int, default=0, help="Drift alerts count from shadow run")
    alpha_promote.add_argument(
        "--execution-reject-rate", type=float, default=0.0, help="Observed reject rate in shadow run"
    )
    alpha_promote.add_argument(
        "--max-execution-reject-rate", type=float, default=0.01, help="Gate E max acceptable reject rate"
    )
    alpha_promote.add_argument(
        "--require-paper-trade-governance",
        action="store_true",
        help="Require paper-trade summary checks (1-week governance) in Gate E",
    )
    alpha_promote.add_argument("--paper-trade-summary", default=None, help="Optional paper-trade summary JSON path")
    alpha_promote.add_argument("--min-paper-trade-calendar-days", type=int, default=7)
    alpha_promote.add_argument("--min-paper-trade-trading-days", type=int, default=5)
    alpha_promote.add_argument("--min-paper-trade-session-minutes", type=int, default=30)
    alpha_promote.add_argument("--min-sharpe-oos", type=float, default=1.0, help="Gate D minimum OOS Sharpe")
    alpha_promote.add_argument("--max-abs-drawdown", type=float, default=0.2, help="Gate D max absolute drawdown")
    alpha_promote.add_argument("--max-turnover", type=float, default=2.0, help="Gate D max turnover")
    alpha_promote.add_argument("--max-correlation", type=float, default=0.7, help="Gate D max correlation to pool")
    alpha_promote.add_argument(
        "--enable-rust-readiness-gate",
        action="store_true",
        help="Enable Gate F Rust readiness checks before approval",
    )
    alpha_promote.add_argument("--rust-module-name", default=None, help="Optional rust module override")
    alpha_promote.add_argument(
        "--rust-parity-test-path",
        default="tests/unit/test_rust_hotpath_parity.py",
        help="Pytest target for Rust parity validation",
    )
    alpha_promote.add_argument("--rust-parity-timeout-s", type=int, default=180)
    alpha_promote.add_argument(
        "--enforce-rust-benchmark-gate",
        action="store_true",
        help="Run Rust benchmark regression command as part of Gate F",
    )
    alpha_promote.add_argument(
        "--rust-benchmark-cmd",
        default=(
            "uv run python tests/benchmark/perf_regression_gate.py "
            "--baseline tests/benchmark/.benchmark_baseline.json "
            "--current benchmark.json "
            "--threshold 0.10"
        ),
        help="Shell command for Rust benchmark regression gate",
    )
    alpha_promote.add_argument("--canary-weight", type=float, help="Override canary weight")
    alpha_promote.add_argument("--expiry-days", type=int, default=30, help="Expiry review date offset")
    alpha_promote.add_argument("--max-live-slippage-bps", type=float, default=3.0, help="Rollback slippage threshold")
    alpha_promote.add_argument(
        "--max-live-drawdown-contribution", type=float, default=0.02, help="Rollback drawdown contribution threshold"
    )
    alpha_promote.add_argument(
        "--max-execution-error-rate", type=float, default=0.01, help="Rollback execution error rate"
    )
    alpha_promote.add_argument("--force", action="store_true", help="Force-write promotion config even if gates fail")
    alpha_promote.add_argument("--config-version", default="v1", help="Semantic config version (e.g. v1, v2)")
    alpha_promote.add_argument("--parent-config-version", default=None, help="Parent config version on re-promotion")
    alpha_promote.add_argument("--out", help="Optional summary JSON output path")
    alpha_promote.set_defaults(func=cmd_alpha_promote)

    alpha_rl_promote = alpha_sub.add_parser(
        "rl-promote",
        help="Promote latest RL run using the same Gate D-E pipeline",
    )
    alpha_rl_promote.add_argument("--alpha-id", required=True, help="RL alpha id")
    alpha_rl_promote.add_argument("--owner", required=True, help="Promotion owner")
    alpha_rl_promote.add_argument("--base-dir", default="research/experiments", help="RL experiment base dir")
    alpha_rl_promote.add_argument("--project-root", default=".", help="Project root for promotion config output")
    alpha_rl_promote.add_argument("--shadow-sessions", type=int, default=0, help="Observed shadow sessions")
    alpha_rl_promote.add_argument(
        "--min-shadow-sessions", type=int, default=5, help="Required shadow sessions for Gate E"
    )
    alpha_rl_promote.add_argument("--drift-alerts", type=int, default=0, help="Drift alerts count")
    alpha_rl_promote.add_argument("--execution-reject-rate", type=float, default=0.0, help="Observed reject rate")
    alpha_rl_promote.add_argument(
        "--force", action="store_true", help="Force-write promotion config even if gates fail"
    )
    alpha_rl_promote.add_argument("--out", help="Optional summary JSON output path")
    alpha_rl_promote.set_defaults(func=cmd_alpha_rl_promote)

    alpha_pool = alpha_sub.add_parser("pool", help="Show alpha pool correlation matrix from latest experiment runs")
    alpha_pool.add_argument(
        "pool_cmd",
        nargs="?",
        choices=["matrix", "redundant", "optimize", "marginal"],
        default="matrix",
        help="pool mode (matrix/redundant/optimize/marginal)",
    )
    alpha_pool.add_argument("--base-dir", default="research/experiments", help="Experiment base dir")
    alpha_pool.add_argument("--threshold", type=float, default=None, help="Redundant correlation threshold")
    alpha_pool.add_argument(
        "--corr-metric", choices=["pearson", "spearman"], default="pearson", help="Correlation metric"
    )
    alpha_pool.add_argument("--redundant", action="store_true", help="Include redundant pair detection")
    alpha_pool.add_argument(
        "--method",
        choices=["equal_weight", "ic_weighted", "mean_variance", "ridge"],
        default="equal_weight",
        help="Pool weight optimization method",
    )
    alpha_pool.add_argument("--ridge-alpha", type=float, default=0.1, help="Ridge regularization strength")
    alpha_pool.add_argument("--alpha-id", help="Target alpha id for pool marginal contribution test")
    alpha_pool.add_argument(
        "--min-uplift", type=float, default=0.05, help="Minimum uplift for marginal contribution pass"
    )
    alpha_pool.add_argument("--out", help="Optional JSON output path")
    alpha_pool.set_defaults(func=cmd_alpha_pool)

    alpha_canary = alpha_sub.add_parser("canary", help="Canary monitor for promoted alphas")
    alpha_canary_sub = alpha_canary.add_subparsers(dest="canary_cmd")

    canary_status = alpha_canary_sub.add_parser("status", help="List all active canaries")
    canary_status.add_argument(
        "--promotions-dir", default="config/strategy_promotions", help="Promotions YAML directory"
    )
    canary_status.set_defaults(func=cmd_alpha_canary_status)

    canary_eval = alpha_canary_sub.add_parser("evaluate", help="Evaluate canary metrics")
    canary_eval.add_argument("--alpha-id", required=True, help="Alpha id to evaluate")
    canary_eval.add_argument("--slippage-bps", type=float, default=0.0, help="Live slippage in bps")
    canary_eval.add_argument("--dd-contrib", type=float, default=0.0, help="Live drawdown contribution")
    canary_eval.add_argument("--error-rate", type=float, default=0.0, help="Live execution error rate")
    canary_eval.add_argument("--sessions", type=int, default=0, help="Number of live sessions")
    canary_eval.add_argument("--sharpe-live", type=float, default=None, help="Live Sharpe ratio (for escalation)")
    canary_eval.add_argument("--apply", action="store_true", help="Apply decision (modify YAML)")
    canary_eval.add_argument("--promotions-dir", default="config/strategy_promotions", help="Promotions YAML directory")
    canary_eval.add_argument("--out", help="Optional JSON output path")
    canary_eval.set_defaults(func=cmd_alpha_canary_evaluate)

    canary_auto_eval = alpha_canary_sub.add_parser("auto-evaluate", help="Auto-evaluate all active canaries (one-shot)")
    canary_auto_eval.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        dest="dry_run",
        help="Evaluate only, do not apply decisions (default)",
    )
    canary_auto_eval.add_argument(
        "--no-dry-run",
        action="store_false",
        dest="dry_run",
        help="Evaluate and apply decisions",
    )
    canary_auto_eval.add_argument(
        "--promotions-dir", default="config/strategy_promotions", help="Promotions YAML directory"
    )
    canary_auto_eval.add_argument("--config", default=None, help="Config path (reserved)")
    canary_auto_eval.add_argument("--out", help="Optional JSON output path")
    canary_auto_eval.set_defaults(func=cmd_alpha_canary_auto_evaluate)

    alpha_ab_compare = alpha_sub.add_parser("ab-compare", help="A/B compare two experiment runs with delta table")
    alpha_ab_compare.add_argument("run_id_a", help="First run ID (A)")
    alpha_ab_compare.add_argument("run_id_b", help="Second run ID (B)")
    alpha_ab_compare.add_argument("--base-dir", default="research/experiments", help="Experiment base dir")
    alpha_ab_compare.add_argument("--out", help="Optional JSON output path")
    alpha_ab_compare.set_defaults(func=cmd_alpha_ab_compare)

    alpha_exp = alpha_sub.add_parser("experiments", help="Experiment tracking utilities")
    alpha_exp_sub = alpha_exp.add_subparsers(dest="alpha_exp_cmd")

    alpha_exp_list = alpha_exp_sub.add_parser("list", help="List experiment runs")
    alpha_exp_list.add_argument("--base-dir", default="research/experiments", help="Experiment base dir")
    alpha_exp_list.add_argument("--alpha-id", help="Filter by alpha id")
    alpha_exp_list.add_argument("--out", help="Optional JSON output path")
    alpha_exp_list.set_defaults(func=cmd_alpha_experiments_list)

    alpha_exp_compare = alpha_exp_sub.add_parser("compare", help="Compare experiment runs by run_id")
    alpha_exp_compare.add_argument("run_ids", nargs="+", help="Run IDs to compare")
    alpha_exp_compare.add_argument("--base-dir", default="research/experiments", help="Experiment base dir")
    alpha_exp_compare.add_argument("--out", help="Optional JSON output path")
    alpha_exp_compare.set_defaults(func=cmd_alpha_experiments_compare)

    alpha_exp_best = alpha_exp_sub.add_parser("best", help="List best runs by metric")
    alpha_exp_best.add_argument("--metric", default="sharpe_oos", help="Metric name")
    alpha_exp_best.add_argument("--top", type=int, default=10, help="Top N runs")
    alpha_exp_best.add_argument("--alpha-id", help="Filter by alpha id")
    alpha_exp_best.add_argument("--base-dir", default="research/experiments", help="Experiment base dir")
    alpha_exp_best.add_argument("--out", help="Optional JSON output path")
    alpha_exp_best.set_defaults(func=cmd_alpha_experiments_best)

    # -- Batch pipeline automation --
    alpha_batch_corr = alpha_sub.add_parser(
        "batch-correlation", help="Batch compute correlation_pool_max across all alphas"
    )
    alpha_batch_corr.add_argument("--experiments-dir", default="research/experiments", help="Experiment base directory")
    alpha_batch_corr.add_argument(
        "--dry-run", action="store_true", help="Show correlations without patching scorecards"
    )
    alpha_batch_corr.add_argument("--out", help="Optional JSON output path")
    alpha_batch_corr.set_defaults(func=cmd_alpha_batch_correlation)

    alpha_pt_batch = alpha_sub.add_parser("paper-trade-batch", help="Batch paper-trade session management")
    alpha_pt_batch_sub = alpha_pt_batch.add_subparsers(dest="paper_trade_action")
    pt_discover = alpha_pt_batch_sub.add_parser("discover", help="Find Gate D passing alphas lacking Gate E sessions")
    pt_discover.add_argument("--experiments-dir", default="research/experiments", help="Experiment base directory")
    pt_discover.add_argument("--top-n", type=int, default=20, help="Max candidates to return")
    pt_discover.add_argument("--min-sharpe-oos", type=float, default=1.0, help="Minimum OOS Sharpe for Gate D")
    pt_discover.add_argument("--out", help="Optional JSON output path")
    pt_discover.set_defaults(func=cmd_alpha_paper_trade_batch)
    pt_record = alpha_pt_batch_sub.add_parser("record", help="Generate synthetic paper-trade sessions")
    pt_record.add_argument("--alpha-ids", nargs="+", required=True, help="Alpha IDs to generate sessions for")
    pt_record.add_argument("--experiments-dir", default="research/experiments", help="Experiment base directory")
    pt_record.add_argument("--sessions-per-alpha", type=int, default=5, help="Sessions to generate per alpha")
    pt_record.add_argument("--base-date", help="Starting date for session generation (ISO)")
    pt_record.add_argument("--seed", type=int, default=42, help="Random seed")
    pt_record.add_argument("--out", help="Optional JSON output path")
    pt_record.set_defaults(func=cmd_alpha_paper_trade_batch)

    alpha_promote_batch = alpha_sub.add_parser(
        "promote-batch", help="Batch run promotion pipeline across multiple alphas"
    )
    alpha_promote_batch.add_argument(
        "--experiments-dir", default="research/experiments", help="Experiment base directory"
    )
    alpha_promote_batch.add_argument("--owner", default="batch", help="Promotion owner name")
    alpha_promote_batch.add_argument("--alpha-ids", nargs="+", help="Specific alpha IDs to promote")
    alpha_promote_batch.add_argument("--top-n", type=int, default=50, help="Max alphas to process")
    alpha_promote_batch.add_argument("--min-sharpe-oos", type=float, default=1.0, help="Minimum OOS Sharpe threshold")
    alpha_promote_batch.add_argument("--max-abs-drawdown", type=float, default=0.2, help="Maximum absolute drawdown")
    alpha_promote_batch.add_argument("--max-correlation", type=float, default=0.7, help="Maximum pool correlation")
    alpha_promote_batch.add_argument(
        "--dry-run", action="store_true", default=True, help="Evaluate without writing configs"
    )
    alpha_promote_batch.add_argument(
        "--no-dry-run", dest="dry_run", action="store_false", help="Write promotion configs"
    )
    alpha_promote_batch.add_argument("--out", help="Optional JSON output path")
    alpha_promote_batch.set_defaults(func=cmd_alpha_promote_batch)

    # ── TCA ─────────────────────────────────────────────────────────────
    tca = sub.add_parser("tca", help="Transaction Cost Analysis utilities")
    tca_sub = tca.add_subparsers(dest="tca_cmd")

    tca_daily = tca_sub.add_parser("daily", help="Daily fill cost report from ClickHouse")
    tca_daily.add_argument("--date", default=None, help="Report date (YYYY-MM-DD, default today)")
    tca_daily.set_defaults(func=cmd_tca_daily)

    # ── Feasibility ──────────────────────────────────────────────────────
    feasibility = sub.add_parser("feasibility", help="Feasibility analysis")
    feasibility_sub = feasibility.add_subparsers(dest="feasibility_cmd")

    feas_report = feasibility_sub.add_parser("report", help="Generate feasibility report")
    feas_report.add_argument("--date", default=None, help="Date (YYYY-MM-DD), default today")
    feas_report.set_defaults(func=cmd_feasibility_report)

    # ── Signal Monitor TUI ──────────────────────────────────────────────
    monitor_cmd = sub.add_parser("monitor", help="Signal Monitor TUI (SHM + ClickHouse hybrid)")
    monitor_cmd.add_argument("--watchlist", default=None, help="Path to watchlist.yaml")
    monitor_cmd.add_argument("--symbols", default=None, help="Path to symbols.yaml")
    monitor_cmd.add_argument(
        "--data-source",
        default=None,
        choices=["auto", "ch", "shm"],
        help="Data source: auto (SHM+CH hybrid), ch (ClickHouse only), shm (shared memory only)",
    )
    monitor_cmd.set_defaults(func=_cmd_monitor)

    return parser


def _cmd_monitor(args: argparse.Namespace) -> None:
    import os
    import sys

    from hft_platform.monitor.cli import run_cli

    if args.data_source:
        os.environ["HFT_MONITOR_DATA_SOURCE"] = args.data_source

    rc = run_cli(
        watchlist_path=args.watchlist,
        symbols_path=args.symbols,
    )
    sys.exit(rc)
