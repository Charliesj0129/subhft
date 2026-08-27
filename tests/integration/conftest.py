"""Shared staging for the production-compose integration tests.

``test_prod_compose_parity.py`` and ``test_prod_compose_immutability.py`` both
render the compose artifacts, and both must do it with interpolation ON.
Reading the locked file with ``--no-interpolate`` is what hid the fact that
Compose could not load it at all (a ``${VAR}``-sourced mount renders as a
named-volume reference nothing declares), and newer compose plugins refuse to
parse the short-syntax replacement in that mode at all ("too many colons"), so
the checks skipped instead of running.

Interpolation means Compose loads ``env_file``, which is ``required: true``.
Rendering inside the repo would therefore read the operator's real ``.env`` --
absent on CI, and a secret leak the moment an assertion prints a mismatched
value. Every render instead happens in a scratch directory holding copies of
the compose files, a placeholder ``.env``, and an environment carrying no
platform variables at all. Both sides render from the same root, so host paths
compare equal without any location-stripping, and the staging doubles as proof
that the locked artifact is not tied to this checkout.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).parent.parent.parent
_COMPOSE_FILES = (
    _REPO_ROOT / "docker-compose.yml",
    _REPO_ROOT / "docker-compose.production.yml",
    _REPO_ROOT / "docker-compose.prod.locked.yml",
)

# ``${VAR:?message}`` -- Compose aborts when one of these is unset.
_REQUIRED_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?")

# The docker CLI needs these to find the daemon and its config. Nothing else is
# inherited, so no ``SHIOAJI_*`` / ``HFT_*`` / password value can reach either
# render -- and therefore none can reach an assertion message.
_DOCKER_ENV_KEYS = frozenset({"PATH", "HOME", "USER", "XDG_RUNTIME_DIR", "TMPDIR"})

_PLACEHOLDER = "placeholder-for-compose-tests"


def _docker_env(required: set[str]) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in _DOCKER_ENV_KEYS or key.startswith("DOCKER_")}
    env.update({name: _PLACEHOLDER for name in required})
    return env


@pytest.fixture(scope="session")
def compose_render(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Callable[..., dict[str, Any]]]:
    """Render compose files by name from a scratch copy, parsed and cached.

    Call as ``compose_render("docker-compose.prod.locked.yml")`` or with several
    names to overlay them in order.
    """
    root = tmp_path_factory.mktemp("compose-staging")
    required: set[str] = set()
    for source in _COMPOSE_FILES:
        if not source.exists():
            continue
        shutil.copy2(source, root / source.name)
        required |= set(_REQUIRED_VAR.findall(source.read_text(encoding="utf-8")))
    (root / ".env").write_text("# placeholder; the operator's real .env is never read by tests\n", encoding="utf-8")

    env = _docker_env(required)
    cache: dict[tuple[str, ...], dict[str, Any]] = {}

    def render(*names: str) -> dict[str, Any]:
        key = tuple(names)
        if key in cache:
            return cache[key]
        for name in key:
            if not (root / name).exists():
                pytest.skip(f"{name} not generated yet")
        cmd = ["docker", "compose"]
        for name in key:
            cmd += ["-f", name]
        cmd += ["config"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(root),
            env=env,
            check=False,
        )
        if result.returncode != 0:
            errors = "\n".join(line for line in result.stderr.splitlines() if "level=warning" not in line)
            pytest.fail(f"docker compose config failed for {' + '.join(key)} (rc={result.returncode}):\n{errors}")
        cache[key] = yaml.safe_load(result.stdout)
        return cache[key]

    yield render
