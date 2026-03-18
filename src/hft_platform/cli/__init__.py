"""HFT Platform CLI — package entry point.

All public symbols previously exported from ``hft_platform.cli`` are
re-exported here so that ``from hft_platform.cli import X`` continues
to work for every downstream consumer.
"""

from __future__ import annotations

from hft_platform.utils.logging import configure_logging

from ._alpha import (
    cmd_alpha_ab_compare,
    cmd_alpha_canary_evaluate,
    cmd_alpha_canary_status,
    cmd_alpha_experiments_best,
    cmd_alpha_experiments_compare,
    cmd_alpha_experiments_list,
    cmd_alpha_list,
    cmd_alpha_pool,
    cmd_alpha_promote,
    cmd_alpha_rl_promote,
    cmd_alpha_scaffold,
    cmd_alpha_search,
    cmd_alpha_validate,
)
from ._backtest import cmd_backtest
from ._config import (
    cmd_contracts_status,
    cmd_resolve_symbols,
    cmd_symbols_build,
    cmd_symbols_preview,
    cmd_symbols_sync,
    cmd_symbols_validate,
)
from ._diag import cmd_diag, cmd_feed_status, cmd_strat_test
from ._feature import (
    cmd_feature_preflight,
    cmd_feature_profiles,
    cmd_feature_rollout_rollback,
    cmd_feature_rollout_set,
    cmd_feature_rollout_status,
    cmd_feature_validate,
)
from ._parser import build_parser
from ._recorder import cmd_recorder_status
from ._run import cmd_check, cmd_init, cmd_run, cmd_wizard
from ._utils import _ensure_project_root_on_path, _resolve_default_mode, _safe_write

__all__ = [
    "build_parser",
    "cmd_alpha_ab_compare",
    "cmd_alpha_canary_evaluate",
    "cmd_alpha_canary_status",
    "cmd_alpha_experiments_best",
    "cmd_alpha_experiments_compare",
    "cmd_alpha_experiments_list",
    "cmd_alpha_list",
    "cmd_alpha_pool",
    "cmd_alpha_promote",
    "cmd_alpha_rl_promote",
    "cmd_alpha_scaffold",
    "cmd_alpha_search",
    "cmd_alpha_validate",
    "cmd_backtest",
    "cmd_check",
    "cmd_contracts_status",
    "cmd_diag",
    "cmd_feed_status",
    "cmd_feature_preflight",
    "cmd_feature_profiles",
    "cmd_feature_rollout_rollback",
    "cmd_feature_rollout_set",
    "cmd_feature_rollout_status",
    "cmd_feature_validate",
    "cmd_init",
    "cmd_recorder_status",
    "cmd_resolve_symbols",
    "cmd_run",
    "cmd_strat_test",
    "cmd_symbols_build",
    "cmd_symbols_preview",
    "cmd_symbols_sync",
    "cmd_symbols_validate",
    "cmd_wizard",
    "main",
    "_resolve_default_mode",
    "_safe_write",
]


def main(argv: list[str] | None = None) -> int:
    _ensure_project_root_on_path()
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    args.func(args)
    return 0
