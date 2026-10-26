"""RunStamp v1: what produced a research run (ADR-005).

A commit hash alone cannot identify the code behind a result: much of
``research/`` is git-ignored local code, and a dirty tree changes the answer
without changing the commit. The stamp therefore records the commit, whether
the tree was dirty, a sha256 of every loaded module that lives in the checkout,
a sha256 of each dataset (including files derived from it, such as the
``hftbt.npz`` the taker runner actually reads), and the config hash and profile
the run used.

v1 adds the run mode, the verdict-rules version, engine versions, the
pre-registered protocol's sha256 and the calibration state. ``None`` in any of
them means "not recorded", never "not needed". Refusing a governed run on a
dirty tree is ``alpha.evidence``'s job; the stamp only records.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from collections.abc import Iterable
from importlib import metadata
from pathlib import Path
from typing import Any

from structlog import get_logger

from hft_platform.alpha.outcomes import VERDICT_RULES_VERSION
from hft_platform.alpha.paths import RepoRootNotFoundError, repo_root

logger = get_logger("alpha.provenance")

RUN_STAMP_SCHEMA = "run_stamp.v1"

# Distributions whose version changes a backtest's numbers.
_ENGINE_DISTRIBUTIONS = ("hftbacktest", "numpy", "numba")

_GIT_TIMEOUT_S = 5.0
_HASH_CHUNK = 1 << 20

# (resolved path, size, mtime_ns) -> sha256, so a file is hashed once per process.
_file_hash_cache: dict[tuple[str, int, int], str] = {}


def file_sha256(path: str | Path, *, cached: bool = True) -> str:
    """sha256 of a file's bytes, cached on (path, size, mtime).

    Pass ``cached=False`` to check integrity: a rewrite of the same size within
    one mtime tick keeps the cache key, so the cache could return the old hash.
    """
    resolved = Path(path).resolve()
    stat = resolved.stat()
    key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    hit = _file_hash_cache.get(key) if cached else None
    if hit is not None:
        return hit
    digest = hashlib.sha256()
    with resolved.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _file_hash_cache[key] = value
    return value


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("run_stamp: git unavailable", args=args, error=str(exc))
        return None
    if proc.returncode != 0:
        logger.warning("run_stamp: git failed", args=args, returncode=proc.returncode)
        return None
    return proc.stdout


def git_state(root: Path) -> tuple[str | None, bool | None]:
    """``(HEAD commit, dirty)`` of ``root``; either is ``None`` when git cannot say."""
    head = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain")
    commit = head.strip() if head else None
    dirty = None if status is None else bool(status.strip())
    return commit, dirty


def _loaded_module_hashes(root: Path) -> dict[str, str]:
    """sha256 of every loaded module whose source file is inside ``root``, keyed by relative path."""
    hashes: dict[str, str] = {}
    for module in list(sys.modules.values()):
        source = getattr(module, "__file__", None)
        if not source or not source.endswith(".py"):
            continue
        try:
            path = Path(source).resolve()
            rel = path.relative_to(root)
        except (OSError, ValueError):
            continue
        if rel.parts and rel.parts[0] in {".venv", "tests"}:
            continue
        try:
            hashes[rel.as_posix()] = file_sha256(path)
        except OSError:
            continue
    return dict(sorted(hashes.items()))


def _dataset_hashes(data_paths: Iterable[str]) -> dict[str, str | None]:
    """sha256 per dataset path; ``None`` when the file cannot be read."""
    hashes: dict[str, str | None] = {}
    for raw in data_paths:
        try:
            hashes[str(raw)] = file_sha256(raw)
        except OSError as exc:
            logger.warning("run_stamp: dataset not hashable", path=str(raw), error=str(exc))
            hashes[str(raw)] = None
    return hashes


def _engine_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {"python": platform.python_version()}
    for dist in _ENGINE_DISTRIBUTIONS:
        try:
            versions[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            versions[dist] = None
    return versions


def build_run_stamp(
    *,
    data_paths: Iterable[str],
    config_hash: str,
    profile: str | None = None,
    mode: str = "exploratory",
    protocol_sha256: str | None = None,
    calibration: str | None = None,
) -> dict[str, Any]:
    """The RunStamp v1 mapping written into a run's ``meta.json``.

    ``data_paths`` should include derived inputs the engine reads (e.g. the
    converted ``hftbt.npz``), not only the files the caller was given.
    """
    try:
        root: Path | None = repo_root()
    except RepoRootNotFoundError:
        root = None
    commit, dirty = git_state(root) if root is not None else (None, None)
    modules = _loaded_module_hashes(root) if root is not None else {}
    modules_digest = hashlib.sha256("\n".join(f"{k} {v}" for k, v in modules.items()).encode("utf-8")).hexdigest()
    return {
        "schema": RUN_STAMP_SCHEMA,
        "mode": str(mode),
        "git_commit": commit,
        "git_dirty": dirty,
        "modules_sha256": modules_digest,
        "modules": modules,
        "datasets": _dataset_hashes(data_paths),
        "config_hash": str(config_hash),
        "profile": profile,
        "verdict_rules_version": VERDICT_RULES_VERSION,
        "engine_versions": _engine_versions(),
        "protocol_sha256": protocol_sha256,
        "calibration": calibration,
    }
