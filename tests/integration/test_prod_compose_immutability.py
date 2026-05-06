"""Integration test: docker-compose.prod.locked.yml must be IMMUTABLE.

Production code must come from the image (built at known SHA), not the host
working tree. This test enforces that invariant on the LOCKED compose by
asserting that no service has a bind mount whose target is, or descends
from, one of the broad source paths (``/app/src``, ``/app/scripts``,
``/app/config``).

Why YAML-level (and not a live container tamper):
- ``test_prod_compose_parity.py::test_no_source_bind_mounts_in_locked``
  proves the locked file does not declare any of the three broad targets
  by exact match.
- This file extends that to PREFIX matches (e.g. ``/app/src/foo``) and
  bind-only types, which is what would actually let a host file shadow
  image code at runtime.
- A live container-tamper test would need a build + start cycle and is
  brittle in CI. The YAML invariant catches the same defect class at
  zero cost. A manual smoke check is documented in the locked compose
  header for operator regression coverage.

Skips cleanly if ``docker`` CLI is unavailable in the test env.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent

_LOCKED_COMPOSE = REPO_ROOT / "docker-compose.prod.locked.yml"

# Bind mounts whose target is one of these (or descends from one) shadow the
# image-side code with the host working tree.
_FORBIDDEN_SOURCE_TARGETS = ("/app/src", "/app/scripts", "/app/config")


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _run_compose_config(compose: Path, timeout: int = 60) -> dict[str, Any]:
    cmd = ["docker", "compose", "-f", str(compose), "config", "--no-interpolate"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"docker compose config failed (rc={result.returncode}):\n{result.stderr}")
    return yaml.safe_load(result.stdout)


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available in this environment")
def test_locked_compose_has_no_bind_mounts_under_source_paths() -> None:
    """No service in the locked compose may bind-mount onto a source-code
    target. Catches both exact matches (``/app/src``) and descendants
    (``/app/src/whatever``)."""
    if not _LOCKED_COMPOSE.exists():
        pytest.skip("docker-compose.prod.locked.yml not generated yet")

    locked = _run_compose_config(_LOCKED_COMPOSE)
    services: dict[str, Any] = locked.get("services", {})

    violations: list[str] = []
    for svc_name, svc in services.items():
        for vol in svc.get("volumes") or []:
            if not isinstance(vol, dict):
                continue
            vol_type = str(vol.get("type", "bind"))
            target = str(vol.get("target", ""))
            source = vol.get("source", "")
            # Only bind mounts can shadow image content from the host.
            if vol_type != "bind":
                continue
            for forbidden in _FORBIDDEN_SOURCE_TARGETS:
                if target == forbidden or target.startswith(forbidden + "/"):
                    violations.append(
                        f"service={svc_name!r}: bind mount target={target!r} source={source!r} "
                        f"would shadow image-side code"
                    )
                    break

    assert not violations, (
        "Locked compose still has source-shadowing bind mounts; production would "
        "run host code instead of image-side build:\n" + "\n".join(violations)
    )


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available in this environment")
def test_locked_compose_engine_runs_image_command() -> None:
    """The hft-engine command in locked compose must invoke the image-side
    Python module, not a host script. ``python -m hft_platform.main`` is
    served from the image's installed package."""
    if not _LOCKED_COMPOSE.exists():
        pytest.skip("docker-compose.prod.locked.yml not generated yet")

    locked = _run_compose_config(_LOCKED_COMPOSE)
    engine = locked.get("services", {}).get("hft-engine", {})
    cmd = engine.get("command")
    assert cmd, f"hft-engine has no command in locked compose: {engine!r}"
    flat = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    assert "python -m hft_platform.main" in flat, (
        f"hft-engine command does not invoke image-side hft_platform.main: {flat!r}"
    )


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available in this environment")
def test_locked_compose_engine_is_read_only() -> None:
    """The hft-engine service must declare ``read_only: true`` so the only
    writable surfaces are the explicitly-allowed tmpfs / state mounts."""
    if not _LOCKED_COMPOSE.exists():
        pytest.skip("docker-compose.prod.locked.yml not generated yet")

    locked = _run_compose_config(_LOCKED_COMPOSE)
    engine = locked.get("services", {}).get("hft-engine", {})
    assert engine.get("read_only") is True, (
        f"hft-engine.read_only must be true in locked compose; got {engine.get('read_only')!r}"
    )
