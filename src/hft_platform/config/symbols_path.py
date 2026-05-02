"""Single source of truth for resolving the ``symbols.yaml`` file path.

Hemorrhage #3 of the Option-3 migration (see
``.agent/memory/contract_rollover_fix_2026_04.md`` for the 2026-04-15 incident
where ``SYMBOLS_CONFIG`` pointed at a stale file for 30 minutes because five
independent callers resolved the path with different fallback chains).

Callers that previously rolled their own fallback (``bootstrap.py``,
``feed_adapter/normalizer.py``, ``feed_adapter/shioaji/client.py``,
``feed_adapter/shioaji/_config.py``) now delegate here.

Precedence order (first non-empty wins):

1. ``explicit`` argument from the caller
2. ``SYMBOLS_CONFIG`` environment variable
3. ``paths_setting`` (from merged config, ``settings["paths"]["symbols"]``)
4. ``{project_root}/config/symbols.yaml`` when the file exists
5. ``{project_root}/config/base/symbols.yaml`` (canonical checked-in fallback)

Resolution is always project-root-anchored so tests that change cwd remain
deterministic.
"""

from __future__ import annotations

import os
from pathlib import Path

from structlog import get_logger

logger = get_logger("config.symbols_path")

# ``symbols_path.py`` lives at src/hft_platform/config/symbols_path.py —
# four .parent hops reach the repository root.
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent


def resolve_symbols_config_path(
    explicit: str | None = None,
    *,
    paths_setting: str | None = None,
) -> str:
    """Return the absolute path to ``symbols.yaml``.

    Always returns an absolute string; callers may open it directly. Logs the
    tier that won and whether the returned file exists so operators can spot a
    misconfiguration like the 2026-04-15 incident without digging through code.
    """
    tier, raw_path = _pick(explicit, paths_setting=paths_setting)
    abs_path = str(Path(raw_path).expanduser().resolve())
    logger.debug(
        "symbols_config_resolved",
        tier=tier,
        path=abs_path,
        exists=Path(abs_path).is_file(),
    )
    return abs_path


def _pick(explicit: str | None, *, paths_setting: str | None) -> tuple[str, str]:
    if explicit:
        return "explicit", explicit

    env_val = os.environ.get("SYMBOLS_CONFIG", "").strip()
    if env_val:
        return "env", env_val

    if paths_setting:
        return "settings", paths_setting

    cwd_candidate = _PROJECT_ROOT / "config" / "symbols.yaml"
    if cwd_candidate.is_file():
        return "project_cwd_file", str(cwd_candidate)

    return "base_default", str(_PROJECT_ROOT / "config" / "base" / "symbols.yaml")


def propagate_env(path: str) -> None:
    """Publish ``path`` to the ``SYMBOLS_CONFIG`` environment variable.

    Uses ``setdefault`` so an operator-provided override is preserved. Call
    once at bootstrap so later-constructed consumers (e.g. ``SymbolMetadata``)
    observe the same file the service graph chose.
    """
    os.environ.setdefault("SYMBOLS_CONFIG", path)
