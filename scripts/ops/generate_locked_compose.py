#!/usr/bin/env python3
"""Generate ``docker-compose.prod.locked.yml`` from base + production overlay.

The base ``docker-compose.yml`` bind-mounts ``./src``, ``./scripts``, ``./config``
into every service that inherits ``*hft-common``. ``docker-compose.production.yml``
adds ``read_only: true`` and tmpfs but *does not* redeclare ``volumes``, so the
broad source bind mounts propagate into production. The result: production
silently runs the host working tree, ``build_info.git_sha`` lies, and rolling
back the image does not roll back the code.

This generator produces the locked production compose by running
``docker compose -f docker-compose.yml -f docker-compose.production.yml config
--no-interpolate`` and stripping every volume whose target is one of the
broad source paths (``/app/src``, ``/app/scripts``, ``/app/config``). Every
other service property (command, environment, depends_on, networks,
healthcheck, restart, deploy, read_only, tmpfs, security_opt, image, ulimits,
logging, ports) is preserved verbatim, so service-property parity vs base+prod
is enforced by ``tests/integration/test_prod_compose_parity.py``.

Usage::

    uv run python scripts/ops/generate_locked_compose.py
    uv run python scripts/ops/generate_locked_compose.py --check    # stdout only

The header recorded at the top of the locked file pins the source compose
digests, the git SHA, and the generation timestamp so operators can trace any
diff back to a reproducible input.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
PROD_OVERLAY = REPO_ROOT / "docker-compose.production.yml"
LOCKED_COMPOSE = REPO_ROOT / "docker-compose.prod.locked.yml"

_STRIPPED_TARGETS = frozenset({"/app/src", "/app/scripts", "/app/config"})


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=5,
            check=False,
        )
        sha = result.stdout.strip()
        return sha or "unknown"
    except (subprocess.SubprocessError, OSError):
        return "unknown"


def _resolve_compose() -> str:
    cmd = [
        "docker",
        "compose",
        "-f",
        str(BASE_COMPOSE),
        "-f",
        str(PROD_OVERLAY),
        "config",
        "--no-interpolate",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"docker compose config failed (rc={result.returncode}); see stderr above."
        )
    return result.stdout


def _strip_source_volumes(compose: dict) -> tuple[dict, int]:
    """Return ``(compose, n_stripped)``.

    Removes every volume whose target is one of the broad source paths.
    Mutates ``compose`` in-place but also returns it so callers can chain.
    """
    stripped = 0
    services = compose.get("services") or {}
    for svc in services.values():
        vols = svc.get("volumes")
        if not vols:
            continue
        kept: list = []
        for vol in vols:
            target = vol.get("target", "") if isinstance(vol, dict) else ""
            if target in _STRIPPED_TARGETS:
                stripped += 1
                continue
            kept.append(vol)
        if kept:
            svc["volumes"] = kept
        else:
            svc.pop("volumes", None)
    return compose, stripped


def _render(compose: dict, *, stripped_count: int) -> str:
    base_digest = _digest(BASE_COMPOSE)
    prod_digest = _digest(PROD_OVERLAY)
    git_sha = _git_sha()
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    header = (
        "# docker-compose.prod.locked.yml — IMMUTABLE PRODUCTION COMPOSE (auto-generated)\n"
        "#\n"
        "# DO NOT EDIT BY HAND. Regenerate with:\n"
        "#   uv run python scripts/ops/generate_locked_compose.py\n"
        "#\n"
        f"# Generated:        {ts} (UTC)\n"
        f"# Git SHA:          {git_sha}\n"
        f"# Base digest:      {base_digest}  ({BASE_COMPOSE.name})\n"
        f"# Prod digest:      {prod_digest}  ({PROD_OVERLAY.name})\n"
        f"# Volumes stripped: {stripped_count} (broad source-path bind mounts)\n"
        "#\n"
        "# Stripped targets: /app/src, /app/scripts, /app/config\n"
        "# Reason: production must run the image-side code, not the host working\n"
        "# tree. Broad source bind mounts override read_only at the mount path,\n"
        "# silently masking what was actually shipped (build_info lies, rollback\n"
        "# does not roll back the code). See loop_v1 plan section L3a and\n"
        "# docs/runbooks/deployment.md.\n"
        "#\n"
        "# Service-property parity vs base+prod is enforced by\n"
        "#   tests/integration/test_prod_compose_parity.py\n"
        "#\n"
        "# Smoke check after regen:\n"
        "#   docker compose -f docker-compose.prod.locked.yml run --rm hft-monitor \\\n"
        "#       python scripts/monitor_runtime_health.py --check\n"
    )
    body = yaml.safe_dump(
        compose,
        sort_keys=False,
        default_flow_style=False,
        width=120,
    )
    return header + "\n" + body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Generate to stdout without writing the file (CI dry-run).",
    )
    args = parser.parse_args(argv)

    raw_yaml = _resolve_compose()
    compose = yaml.safe_load(raw_yaml)
    if not isinstance(compose, dict):
        raise SystemExit(f"docker compose config produced non-dict YAML: {type(compose)!r}")

    compose, stripped_count = _strip_source_volumes(compose)
    output = _render(compose, stripped_count=stripped_count)

    if args.check:
        sys.stdout.write(output)
        return 0

    LOCKED_COMPOSE.write_text(output, encoding="utf-8")
    print(
        f"Generated {LOCKED_COMPOSE.relative_to(REPO_ROOT)} "
        f"({len(output)} bytes, {stripped_count} source mounts stripped)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
