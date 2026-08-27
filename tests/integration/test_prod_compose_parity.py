"""Integration test: docker-compose.prod.locked.yml service-property parity gate.

Compares `docker compose -f docker-compose.prod.locked.yml config` vs
`docker compose -f docker-compose.yml -f docker-compose.production.yml config`.

They must differ ONLY in the `volumes` arrays for source-path entries;
every other key (environment, command, healthcheck, etc.) must be identical
when parsed via pyyaml.

Skips cleanly if `docker` CLI is unavailable in the test env (CI-friendly).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent

# Source bind-mount target paths that are intentionally stripped in locked compose.
_STRIPPED_TARGETS = frozenset({"/app/src", "/app/scripts", "/app/config"})

_LOCKED_COMPOSE = REPO_ROOT / "docker-compose.prod.locked.yml"
_LOCKED_NAME = _LOCKED_COMPOSE.name
_BASE_NAME = "docker-compose.yml"
_PROD_NAME = "docker-compose.production.yml"

# ``compose_render`` (tests/integration/conftest.py) renders these from a scratch
# copy with interpolation on. See that module for why neither file may be read
# in place or with ``--no-interpolate``.
Renderer = Callable[..., dict[str, Any]]

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


def _vol_key(v: dict[str, Any]) -> tuple[str, str, bool, str]:
    return (
        str(v.get("source", "")),
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
def test_service_properties_match_base_plus_prod(compose_render: Renderer) -> None:
    """Non-volume service properties must be identical in locked vs base+prod."""
    locked = compose_render(_LOCKED_NAME)
    base_prod = compose_render(_BASE_NAME, _PROD_NAME)

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
def test_volumes_differ_only_on_source_paths(compose_render: Renderer) -> None:
    """After removing stripped source-path entries, remaining volumes must match.

    Both sides are rendered from the same staging directory, so a host path is
    identical on both or the artifact genuinely disagrees with base+prod. That
    is only true because the locked file records paths relative to itself; while
    it baked in absolute paths from the generating checkout, this comparison
    could not be made from a git worktree at all.
    """
    locked = compose_render(_LOCKED_NAME)
    base_prod = compose_render(_BASE_NAME, _PROD_NAME)

    locked_services: dict[str, Any] = locked.get("services", {})
    base_prod_services: dict[str, Any] = base_prod.get("services", {})

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

        bp_set = {_vol_key(v) for v in bp_vols_filtered}
        locked_set = {_vol_key(v) for v in locked_vols_filtered}

        only_in_bp = bp_set - locked_set
        only_in_locked = locked_set - bp_set

        if only_in_bp:
            mismatches.append(f"service={svc_name!r}: base+prod has extra non-source volumes: {only_in_bp}")
        if only_in_locked:
            mismatches.append(f"service={svc_name!r}: locked has extra volumes not in base+prod: {only_in_locked}")

    assert not mismatches, "Volume mismatches (beyond expected source-path differences):\n" + "\n".join(mismatches)


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available in this environment")
def test_no_source_bind_mounts_in_locked(compose_render: Renderer) -> None:
    """The locked compose must contain zero broad source bind mounts."""
    locked = compose_render(_LOCKED_NAME)
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


@pytest.mark.skipif(not _docker_available(), reason="docker CLI not available in this environment")
def test_locked_compose_loads_outside_the_checkout_that_generated_it(
    compose_render: Renderer,
) -> None:
    """Compose must accept the locked file anywhere. Every check above is moot otherwise.

    Two defects made this false for the whole life of the file, and neither was
    visible to a check that read it with ``--no-interpolate``:

    - a ``${VAR}``-sourced mount renders as long-syntax ``type: volume`` with
      interpolation off, because there is no resolved source to classify as a
      path; fed back to Compose that is a named-volume reference nothing
      declares, and the file is rejected outright;
    - ``docker compose config`` emits every host path absolute, so ``env_file``
      and 35 bind sources named the checkout that generated the file. A
      ``required`` ``env_file`` that does not exist on the deploy host is a hard
      load failure.

    Rendering from a scratch directory (``compose_render``) exercises both: the
    file is parsed with interpolation, and from a directory that is not the one
    it was generated in.
    """
    services = compose_render(_LOCKED_NAME).get("services") or {}
    assert services, "the locked compose rendered with no services"
