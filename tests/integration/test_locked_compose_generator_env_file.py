"""The locked-compose generator must not copy ``env_file`` values into the artifact it writes to a public repo.

``docker compose config`` merges every service's ``env_file`` into ``environment`` unless told otherwise. Checked on
Compose v5.0.2 with a throwaway file: the plain command prints the dummy value, while ``--no-interpolate`` (what the
generator passes) and ``--no-env-resolution`` each leave the ``env_file`` entry as a path. The generator relied on
``--no-interpolate`` for that and no test pinned it, so a Compose release that changed the flag's meaning would have
put ``env_file`` values (``.env`` is one of them in the real compose) into ``docker-compose.prod.locked.yml``.

The compose and env files here are throwaway files with dummy values; the operator's real ``.env`` is never read.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "generate_locked_compose.py"

_KEY = "DUMMY_SECRET_LIKE"
_VALUE = "value-from-env-file"


def _compose_plugin_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30, check=False).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _load_generator(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("generate_locked_compose_env_file_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # setitem, not assignment: the module must not outlive the test and leak into a later one in the same worker.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(not _compose_plugin_available(), reason="docker compose plugin not available in this environment")
def test_generator_resolution_keeps_env_file_values_out_of_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "dummy.env").write_text(f"{_KEY}={_VALUE}\n", encoding="utf-8")
    base = tmp_path / "docker-compose.yml"
    base.write_text("services:\n  app:\n    image: busybox\n    env_file:\n      - dummy.env\n", encoding="utf-8")
    overlay = tmp_path / "docker-compose.production.yml"
    overlay.write_text("services:\n  app:\n    read_only: true\n", encoding="utf-8")
    module = _load_generator(monkeypatch)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "BASE_COMPOSE", base)
    monkeypatch.setattr(module, "PROD_OVERLAY", overlay)

    output = module._resolve_compose()

    assert _VALUE not in output
    assert _KEY not in output
    assert "dummy.env" in output  # the entry is still there, as a path to a file the deploy host provides
    assert "read_only: true" in output  # the overlay was merged too, as in the real two-file command
