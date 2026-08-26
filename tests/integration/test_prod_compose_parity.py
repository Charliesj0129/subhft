"""Integration test: docker-compose.prod.locked.yml service-property parity gate.

Compares `docker compose -f docker-compose.prod.locked.yml config` vs
`docker compose -f docker-compose.yml -f docker-compose.production.yml config`.

They must differ ONLY in the `volumes` arrays for source-path entries;
every other key (environment, command, healthcheck, etc.) must be identical
when parsed via pyyaml.

Skips cleanly if `docker` CLI is unavailable in the test env (CI-friendly).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent

# Source bind-mount target paths that are intentionally stripped in locked compose.
_STRIPPED_TARGETS = frozenset({"/app/src", "/app/scripts", "/app/config"})

_LOCKED_COMPOSE = REPO_ROOT / "docker-compose.prod.locked.yml"
_BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
_PROD_OVERLAY = REPO_ROOT / "docker-compose.production.yml"

# Properties we compare between locked and base+prod configs.
_COMPARED_KEYS = (
    "command",
    "environment",
    "depends_on",
    "networks",
    "healthcheck",
    "restart",
    "deploy",
    "read_only",
    "tmpfs",
    "security_opt",
    "image",
    "ulimits",
    "logging",
    "ports",
)


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _run_compose_config(files: list[Path], timeout: int = 60) -> dict[str, Any]:
    """Run `docker compose config` and parse the YAML output.

    Older docker compose plugin versions (e.g. some GitHub Actions runners
    with the bundled v2.x plugin) fail to validate volume specs containing
    ``${VAR:-default}`` substitutions under ``--no-interpolate`` (counted
    as "too many colons"). Skip cleanly in that case rather than misreport
    a failure on parity intent.
    """
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", str(f)]
    cmd += ["config", "--no-interpolate"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        # Some older docker compose plugin versions reject ``${VAR:-default}``
        # under ``--no-interpolate`` with "too many colons". Skip rather than
        # misreport a parity failure caused by the CI tool, not the compose
        # files.
        if "too many colons" in result.stderr:
            pytest.skip(
                f"docker compose plugin in this env rejects parameterized volume "
                f"specs under --no-interpolate; rc={result.returncode}"
            )
        pytest.fail(f"docker compose config failed (rc={result.returncode}):\n{result.stderr}")
    return yaml.safe_load(result.stdout)


def _infer_locked_root(locked: dict[str, Any], base_prod: dict[str, Any]) -> str:
    """The checkout the locked file's absolute bind sources were generated in.

    ``docker compose config`` resolves a relative bind source against the
    compose file's directory, while the locked file carries absolute paths
    baked in by its generator. Those two roots are equal only when the test
    runs from the same checkout the locked file was generated in -- from any
    git worktree every bind mount differs, and the test reported dozens of
    mismatches that were entirely its own working directory, burying the two
    real ones.

    Inferred rather than assumed: find a bind mount both configs declare for
    the same service and target, and strip the base+prod side's repo-relative
    suffix off the locked side's absolute path. System mounts (``/proc``,
    ``/sys``, ``/dev/hugepages``) are why a common-prefix scan cannot be used
    -- they drag it to ``/``.
    """
    repo_root = str(REPO_ROOT)
    for svc_name, locked_svc in locked.get("services", {}).items():
        bp_svc = base_prod.get("services", {}).get(svc_name)
        if not bp_svc:
            continue
        bp_by_target = {
            str(v.get("target", "")): str(v.get("source", ""))
            for v in (bp_svc.get("volumes") or [])
            if str(v.get("source", "")).startswith(repo_root + os.sep)
        }
        for vol in locked_svc.get("volumes") or []:
            bp_source = bp_by_target.get(str(vol.get("target", "")))
            locked_source = str(vol.get("source", ""))
            if not bp_source or not locked_source.startswith("/"):
                continue
            suffix = os.sep + os.path.relpath(bp_source, repo_root)
            if locked_source.endswith(suffix):
                return locked_source[: -len(suffix)]
    return repo_root


def _vol_key(v: dict[str, Any], root: str = "") -> tuple[str, str, bool, str]:
    source = str(v.get("source", ""))
    if root and source.startswith(root):
        # Repo-relative, so the comparison asks "the same file?" rather than
        # "the same checkout?" -- which is what this test claims to compare.
        source = os.path.relpath(source, root)
    return (
        source,
        str(v.get("target", "")),
        bool(v.get("read_only", False)),
        str(v.get("type", "bind")),
    )


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available in this environment")
def test_locked_compose_exists() -> None:
    """The locked compose file must exist before parity tests run."""
    assert _LOCKED_COMPOSE.exists(), (
        f"docker-compose.prod.locked.yml not found at {_LOCKED_COMPOSE}. "
        "Run: python scripts/ops/generate_locked_compose.py"
    )


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available in this environment")
def test_service_properties_match_base_plus_prod() -> None:
    """Non-volume service properties must be identical in locked vs base+prod."""
    if not _LOCKED_COMPOSE.exists():
        pytest.skip("docker-compose.prod.locked.yml not generated yet")

    locked = _run_compose_config([_LOCKED_COMPOSE])
    base_prod = _run_compose_config([_BASE_COMPOSE, _PROD_OVERLAY])

    locked_services: dict[str, Any] = locked.get("services", {})
    base_prod_services: dict[str, Any] = base_prod.get("services", {})

    mismatches: list[str] = []
    for svc_name, locked_svc in locked_services.items():
        if svc_name not in base_prod_services:
            continue
        bp_svc = base_prod_services[svc_name]
        for key in _COMPARED_KEYS:
            locked_val = locked_svc.get(key)
            bp_val = bp_svc.get(key)
            if locked_val != bp_val:
                mismatches.append(f"service={svc_name!r} key={key!r}: locked={locked_val!r} != base+prod={bp_val!r}")

    assert not mismatches, (
        f"Service property mismatches between locked and base+prod compose "
        f"({len(mismatches)} issue(s)):\n" + "\n".join(mismatches)
    )


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available in this environment")
def test_volumes_differ_only_on_source_paths() -> None:
    """After removing stripped source-path entries, remaining volumes must match."""
    if not _LOCKED_COMPOSE.exists():
        pytest.skip("docker-compose.prod.locked.yml not generated yet")

    locked = _run_compose_config([_LOCKED_COMPOSE])
    base_prod = _run_compose_config([_BASE_COMPOSE, _PROD_OVERLAY])

    locked_services: dict[str, Any] = locked.get("services", {})
    base_prod_services: dict[str, Any] = base_prod.get("services", {})

    locked_root = _infer_locked_root(locked, base_prod)
    bp_root = str(REPO_ROOT)

    mismatches: list[str] = []
    for svc_name, locked_svc in locked_services.items():
        if svc_name not in base_prod_services:
            continue
        bp_svc = base_prod_services[svc_name]

        locked_vols: list[dict[str, Any]] = locked_svc.get("volumes") or []
        bp_vols: list[dict[str, Any]] = bp_svc.get("volumes") or []

        # Strip the broad source mounts from base+prod side for a fair comparison
        bp_vols_filtered = [v for v in bp_vols if v.get("target") not in _STRIPPED_TARGETS]
        locked_vols_filtered = [v for v in locked_vols if v.get("target") not in _STRIPPED_TARGETS]

        bp_set = {_vol_key(v, bp_root) for v in bp_vols_filtered}
        locked_set = {_vol_key(v, locked_root) for v in locked_vols_filtered}

        only_in_bp = bp_set - locked_set
        only_in_locked = locked_set - bp_set

        if only_in_bp:
            mismatches.append(f"service={svc_name!r}: base+prod has extra non-source volumes: {only_in_bp}")
        if only_in_locked:
            mismatches.append(f"service={svc_name!r}: locked has extra volumes not in base+prod: {only_in_locked}")

    assert not mismatches, "Volume mismatches (beyond expected source-path differences):\n" + "\n".join(mismatches)


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available in this environment")
def test_no_source_bind_mounts_in_locked() -> None:
    """The locked compose must contain zero broad source bind mounts."""
    if not _LOCKED_COMPOSE.exists():
        pytest.skip("docker-compose.prod.locked.yml not generated yet")

    locked = _run_compose_config([_LOCKED_COMPOSE])
    locked_services: dict[str, Any] = locked.get("services", {})

    violations: list[str] = []
    for svc_name, svc in locked_services.items():
        for vol in svc.get("volumes") or []:
            target = str(vol.get("target", ""))
            if target in _STRIPPED_TARGETS:
                violations.append(f"service={svc_name!r}: broad source mount at target={target!r}")

    assert not violations, "Locked compose contains broad source bind mounts that should be stripped:\n" + "\n".join(
        violations
    )
