"""Repo-anchored paths for research governance files.

Research config and ledgers used to be ``Path("config/...")`` relative to the
process cwd, so a run started from another directory silently read a different
(or no) latency profile and wrote its ledgers somewhere else. Everything here
resolves against the checkout that holds this module instead.

``repo_root`` raises when no checkout is found: research config must come from
the files, not from whatever happens to sit under the cwd. Callers that run at
import time or while building a CLI parser use ``repo_path_or_relative``, which
keeps the old relative path when there is no checkout (an installed wheel), so
that importing the package never fails.
"""

from __future__ import annotations

import functools
from pathlib import Path

# A directory is the repo root when it holds both of these.
_ROOT_MARKERS: tuple[str, ...] = ("pyproject.toml", "config/research")


class RepoRootNotFoundError(RuntimeError):
    """No checkout (``pyproject.toml`` + ``config/research``) above this module."""


def _is_repo_root(path: Path) -> bool:
    return all((path / marker).exists() for marker in _ROOT_MARKERS)


@functools.cache
def _find_repo_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if _is_repo_root(candidate):
            return candidate
    return None


def repo_root() -> Path:
    """The checkout this module was imported from; raises if there is none."""
    found = _find_repo_root(Path(__file__).resolve().parent)
    if found is None:
        raise RepoRootNotFoundError(
            f"no repo root above {Path(__file__).resolve()} (looked for {', '.join(_ROOT_MARKERS)})"
        )
    return found


def repo_path(relative: str | Path) -> Path:
    """``relative`` anchored at the repo root; an absolute path is returned as is."""
    path = Path(relative)
    return path if path.is_absolute() else repo_root() / path


def repo_path_or_relative(relative: str | Path) -> Path:
    """Like ``repo_path`` but never raises: without a checkout, ``relative`` is returned unchanged."""
    try:
        return repo_path(relative)
    except RepoRootNotFoundError:
        return Path(relative)
