"""HFT Platform CLI — package entry point.

``from hft_platform.cli import main`` continues to work after the
cli.py -> cli/ package refactor.
"""

from __future__ import annotations

# Backward-compat: tests patch these at "hft_platform.cli" scope
import subprocess as subprocess  # noqa: F401
import sys
from pathlib import Path

from hft_platform.config.loader import load_settings as load_settings  # noqa: F401
from hft_platform.utils.logging import configure_logging

# ---------------------------------------------------------------------------
# Backward-compatible re-exports so that existing test imports like
#   ``from hft_platform.cli import cmd_check, cmd_init, cmd_run``
# keep working.
# ---------------------------------------------------------------------------
from ._alpha import (  # noqa: F401
    cmd_alpha_ab_compare,
    cmd_alpha_batch_correlation,
    cmd_alpha_canary_evaluate,
    cmd_alpha_canary_status,
    cmd_alpha_experiments_best,
    cmd_alpha_experiments_compare,
    cmd_alpha_experiments_list,
    cmd_alpha_list,
    cmd_alpha_paper_trade_batch,
    cmd_alpha_pool,
    cmd_alpha_promote,
    cmd_alpha_promote_batch,
)
from ._feasibility import cmd_feasibility_report  # noqa: F401
from ._feature import (  # noqa: F401
    cmd_feature_preflight,
    cmd_feature_profiles,
    cmd_feature_rollout_rollback,
    cmd_feature_rollout_set,
    cmd_feature_rollout_status,
    cmd_feature_validate,
)
from ._ops import (  # noqa: F401
    cmd_backtest,
    cmd_contracts_status,
    cmd_diag,
    cmd_feed_status,
    cmd_ops_flatten,
    cmd_recorder_status,
    cmd_strat_test,
)
from ._parser import build_parser
from ._research import forward_to_research, run_research_alpha
from ._run import (  # noqa: F401
    _resolve_default_mode,
    cmd_check,
    cmd_init,
    cmd_run,
    cmd_wizard,
)
from ._symbols import (  # noqa: F401
    _resolve_symbols_shioaji,  # noqa: F401
    cmd_resolve_symbols,
    cmd_symbols_build,
    cmd_symbols_preview,
    cmd_symbols_sync,
    cmd_symbols_validate,
)
from ._utils import _safe_write  # noqa: F401


def _ensure_project_root_on_path() -> None:
    """Put the checkout root on sys.path so alpha discovery can import research/alphas/<id>/impl.py by name."""
    root = Path(__file__).resolve().parents[3]
    if (root / "research").exists():
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _ensure_project_root_on_path()
    # Reduce default thread stack from 8 MB to 2 MB.
    # Most platform threads are I/O waiters; 2 MB is sufficient.
    # Does NOT affect Shioaji SDK C++ threads (they set their own stack size).
    import os
    import threading

    _stack_mb = int(os.getenv("HFT_THREAD_STACK_SIZE_MB", "2"))
    if _stack_mb > 0:
        threading.stack_size(_stack_mb * 1024 * 1024)
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    if args.func is forward_to_research:
        return run_research_alpha(argv[argv.index("alpha") + 1 :])
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
