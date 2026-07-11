"""Throwaway-venv orchestration for multi-version surface capture.

For each requested shioaji version: create an isolated venv under ``/tmp`` (never
inside the repo tree), install ``shioaji[speed]==<version>`` from PyPI, run the
in-venv capture engine as a subprocess, and write the resulting surface snapshot
to ``tests/golden/shioaji_sdk/surface_<version>.json`` atomically.

Hard safety rails:
  * venvs live ONLY under ``/tmp/shioaji_api_diff_venvs`` (gitignored); the code
    refuses to operate on any path inside the repo tree.
  * it NEVER runs ``uv sync``/``uv add`` and never writes ``pyproject.toml`` /
    ``uv.lock`` — the project's pinned shioaji is untouched.
  * the capture subprocess env is scrubbed of ``HFT_*`` / ``SHIOAJI_*`` / SOL_*.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ._capture_entrypoint import canonical_json
from .paths import GOLDEN_DIR, REPO_ROOT

VENV_CACHE = Path(tempfile.gettempdir()) / "shioaji_api_diff_venvs"
_CAPTURE_MODULE = "scripts.shioaji_api_diff._capture_entrypoint"
_INSTALL_TIMEOUT_S = 600
_CAPTURE_TIMEOUT_S = 120


class OrchestrationError(RuntimeError):
    pass


def _uv_bin() -> str:
    candidate = os.environ.get("HFT_UV_BIN") or shutil.which("uv") or "/home/charlie/.local/bin/uv"
    if not Path(candidate).exists():
        raise OrchestrationError(f"uv not found (looked at {candidate!r}); set HFT_UV_BIN")
    return candidate


def _scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith(("HFT_", "SHIOAJI_", "SOL_")))}
    env["PYTHONPATH"] = str(REPO_ROOT)
    env.pop("VIRTUAL_ENV", None)
    return env


def _venv_python(venv: Path) -> Path:
    return venv / "bin" / "python"


def _assert_safe_venv(venv: Path) -> None:
    resolved = venv.resolve()
    if resolved == REPO_ROOT.resolve() or REPO_ROOT.resolve() in resolved.parents:
        raise OrchestrationError(f"refusing to use a venv inside the repo tree: {resolved}")


def _venv_has_version(venv: Path, version: str) -> bool:
    py = _venv_python(venv)
    if not py.exists():
        return False
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv
            [str(py), "-c", "import shioaji,sys;sys.stdout.write(shioaji.__version__)"],
            capture_output=True, text=True, timeout=30, check=False)
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0 and proc.stdout.strip() == version


def _ensure_venv(version: str, refresh: bool) -> Path:
    venv = VENV_CACHE / version
    _assert_safe_venv(venv)
    if not refresh and _venv_has_version(venv, version):
        return venv
    if venv.exists():
        shutil.rmtree(venv, ignore_errors=True)
    VENV_CACHE.mkdir(parents=True, exist_ok=True)
    uv = _uv_bin()
    _run([uv, "venv", "--python", "3.12", str(venv)], timeout=180,
         what=f"create venv for {version}")
    _run([uv, "pip", "install", "--python", str(_venv_python(venv)),
          f"shioaji[speed]=={version}"], timeout=_INSTALL_TIMEOUT_S,
         what=f"install shioaji=={version}")
    if not _venv_has_version(venv, version):
        raise OrchestrationError(f"installed venv does not report shioaji=={version}")
    return venv


def _run(argv: list[str], timeout: int, what: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,  # noqa: S603
                          check=False, env=_scrubbed_env())
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise OrchestrationError(f"failed to {what}: " + " / ".join(tail))
    return proc


def capture_version(version: str, refresh: bool, keep_venv: bool) -> dict[str, Any]:
    """Install + capture one version, returning its surface snapshot dict."""
    import json
    venv = _ensure_venv(version, refresh)
    proc = subprocess.run(  # noqa: S603 - fixed argv
        [str(_venv_python(venv)), "-m", _CAPTURE_MODULE, "--emit-json"],
        capture_output=True, text=True, timeout=_CAPTURE_TIMEOUT_S, check=False,
        env=_scrubbed_env(), cwd=str(REPO_ROOT))
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        raise OrchestrationError(f"capture failed for {version}: " + " / ".join(tail))
    snapshot = json.loads(proc.stdout)
    got = (snapshot.get("dist") or {}).get("version")
    if got != version:
        raise OrchestrationError(f"captured version {got!r} != requested {version!r}")
    if not keep_venv:
        shutil.rmtree(venv, ignore_errors=True)
    return snapshot


def surface_path(version: str) -> Path:
    return GOLDEN_DIR / f"surface_{version}.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; committed goldens want 0644
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def orchestrate(versions: list[str], *, refresh: bool = False, keep_venv: bool = True,
                jobs: int = 3) -> dict[str, Path]:
    """Capture all versions (parallel), write golden snapshots, return paths."""
    todo: list[str] = []
    written: dict[str, Path] = {}
    for v in versions:
        path = surface_path(v)
        if path.exists() and not refresh:
            written[v] = path
            sys.stderr.write(f"[skip] surface_{v}.json exists (use --refresh to rebuild)\n")
        else:
            todo.append(v)

    def _one(version: str) -> tuple[str, dict[str, Any]]:
        return version, capture_version(version, refresh=refresh, keep_venv=keep_venv)

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(todo)))) as pool:
            for version, snapshot in pool.map(_one, todo):
                path = surface_path(version)
                _atomic_write(path, canonical_json(snapshot))
                written[version] = path
                sys.stderr.write(f"[ok]   wrote {path.name} "
                                 f"(sha {snapshot.get('snapshot_sha256', '?')[:12]})\n")
    return written
