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
broad source paths (``/app/src``, ``/app/scripts``, ``/app/config``). It then
re-emits parameterized mounts in short syntax and rewrites every host path that
lies inside this checkout as relative to the file, so the artifact loads on the
host it is deployed to rather than only on the machine that wrote it. Every
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
        raise SystemExit(f"docker compose config failed (rc={result.returncode}); see stderr above.")
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
        "# Host paths are relative to THIS FILE's directory, not to the checkout that\n"
        "# generated it: `docker compose config` emits them absolute, which pinned the\n"
        "# artifact (and its `required` env_file) to one machine. Parameterized mounts\n"
        "# stay in short syntax so Compose, not this script, decides bind vs volume.\n"
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


def _restore_parameterized_short_syntax(compose: dict) -> int:
    """Re-emit ``${VAR}``-sourced mounts in short syntax. Returns how many.

    Compose decides bind-vs-named-volume for a short-syntax mount by looking at
    the *interpolated* source: a path becomes a bind, a bare name becomes a
    named volume. ``config --no-interpolate`` has no interpolated source to look
    at, so it renders every parameterized mount as long-syntax ``type: volume``
    -- and feeding that back to Compose fails with "refers to undefined volume
    ./backups/clickhouse", because that is now a named-volume reference rather
    than a bind. Every locked file generated so far has been unloadable for this
    reason.

    Rewriting the classification here would mean guessing what the variable
    expands to, and the guess is wrong for exactly the case the variable exists
    for (``CH_DATA_HOT`` defaults to a named volume but is meant to be
    overridable with an NVMe path). Short syntax has no classification to get
    wrong: it hands the decision back to Compose at interpolation time, which is
    what ``docker-compose.yml`` itself does.
    """
    restored = 0
    for svc in (compose.get("services") or {}).values():
        vols = svc.get("volumes")
        if not vols:
            continue
        rewritten: list = []
        for vol in vols:
            if not isinstance(vol, dict) or "${" not in str(vol.get("source", "")):
                rewritten.append(vol)
                continue
            spec = f"{vol['source']}:{vol.get('target', '')}"
            if vol.get("read_only"):
                spec += ":ro"
            rewritten.append(spec)
            restored += 1
        svc["volumes"] = rewritten
    return restored


def _repo_relative(value: str) -> str | None:
    """``value`` expressed relative to the repo root, or ``None`` if it is elsewhere."""
    if not value.startswith("/"):
        return None
    try:
        relative = Path(value).relative_to(REPO_ROOT)
    except ValueError:
        return None  # a real host path, not one of ours
    return "." if relative == Path(".") else f"./{relative}"


def _relativize_volumes(svc: dict) -> int:
    rewritten = 0
    for vol in svc.get("volumes") or []:
        if not isinstance(vol, dict):
            continue  # short syntax: parameterized, and never absolute
        replacement = _repo_relative(str(vol.get("source", "")))
        if replacement is not None:
            vol["source"] = replacement
            rewritten += 1
    return rewritten


def _relativize_build(svc: dict) -> int:
    build = svc.get("build")
    if isinstance(build, str):
        replacement = _repo_relative(build)
        if replacement is None:
            return 0
        svc["build"] = replacement
        return 1
    if not isinstance(build, dict):
        return 0
    rewritten = 0
    for key in ("context", "dockerfile"):
        replacement = _repo_relative(str(build.get(key, "")))
        if replacement is not None:
            build[key] = replacement
            rewritten += 1
    return rewritten


def _relativize_env_file(svc: dict) -> int:
    env_file = svc.get("env_file")
    if isinstance(env_file, str):
        replacement = _repo_relative(env_file)
        if replacement is None:
            return 0
        svc["env_file"] = replacement
        return 1
    if not isinstance(env_file, list):
        return 0
    rewritten = 0
    entries: list = []
    for entry in env_file:
        if isinstance(entry, dict):
            replacement = _repo_relative(str(entry.get("path", "")))
            if replacement is not None:
                entry["path"] = replacement
                rewritten += 1
            entries.append(entry)
        elif isinstance(entry, str):
            replacement = _repo_relative(entry)
            rewritten += replacement is not None
            entries.append(entry if replacement is None else replacement)
        else:
            entries.append(entry)
    svc["env_file"] = entries
    return rewritten


def _relativize_repo_paths(compose: dict) -> int:
    """Rewrite paths inside the generating checkout as relative. Returns how many.

    ``docker compose config`` resolves every host-side path against the compose
    file's directory and emits it absolute, so the generated file records the
    checkout that produced it: an ``env_file`` under that root plus every bind
    source. That makes the artifact unusable anywhere else, and not quietly --
    Compose refuses to load a file whose ``required`` ``env_file`` is missing,
    so the locked compose only ever parsed on the machine that wrote it.

    A *relative* host path is resolved against the compose file's own directory,
    which is the semantics this artifact wants: whatever directory it is
    deployed into becomes its root. Paths outside the checkout (``/proc``,
    ``/var/run/docker.sock``) are genuine host locations and stay absolute.
    """
    return sum(
        _relativize_volumes(svc) + _relativize_build(svc) + _relativize_env_file(svc)
        for svc in (compose.get("services") or {}).values()
    )


def _generator_local_paths(compose: dict) -> list[str]:
    """Every string in ``compose`` that still points inside the generating checkout.

    Deliberately a blind recursive walk rather than a list of the fields
    ``_relativize_repo_paths`` knows about: the rewrite has to name its fields,
    so the check that the rewrite was complete must not. ``build.context`` was
    found this way -- it is a path-valued key that neither ``volumes`` nor
    ``env_file`` handling would ever have touched.
    """
    root = str(REPO_ROOT)
    found: list[str] = []

    def walk(node: object, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{trail}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{trail}[{index}]")
        elif isinstance(node, str) and (node == root or node.startswith(f"{root}/")):
            found.append(f"{trail.lstrip('.')} = {node}")

    walk(compose.get("services") or {}, "")
    return found


def _assert_no_generator_local_paths(compose: dict) -> None:
    """The artifact must carry no path from the machine that generated it.

    This is the invariant ``_relativize_repo_paths`` exists to establish, checked
    separately so that a field it does not know about (a future ``build.context``,
    a new path-valued key) fails the generation instead of being shipped. An
    absolute path from one checkout is the failure mode that kept this file
    machine-local for five months.
    """
    leaked = _generator_local_paths(compose)
    if not leaked:
        return
    detail = "\n".join(f"  {item}" for item in leaked[:5])
    more = f"\n  ... and {len(leaked) - 5} more" if len(leaked) > 5 else ""
    raise SystemExit(
        f"refusing to write {LOCKED_COMPOSE.name}: {len(leaked)} path(s) still point inside "
        f"{REPO_ROOT}, the checkout this file was generated from. The locked compose is "
        f"deployed to a host that does not have that directory, so it would fail to load "
        f"there:\n{detail}{more}\n"
        f"Teach _relativize_repo_paths about the field(s) above."
    )


def _assert_matches_committed_identity(compose: dict) -> None:
    """Refuse to regenerate into a different Compose project than the live one.

    Neither ``docker-compose.yml`` nor the production overlay declares ``name:``,
    so Compose derives the project name from the *directory name* and stamps it
    onto the default network and every named volume. Regenerating from a git
    worktree therefore renames ``hft_platform_ch_data_hot`` to
    ``<worktree>_ch_data_hot``: the stack comes up attached to brand-new empty
    volumes instead of production's ClickHouse data, and nothing about the diff
    says so.

    It is silent, so check it against the file being replaced rather than
    trusting the caller's working directory. Pin with ``COMPOSE_PROJECT_NAME``
    to satisfy this. Host *paths* no longer carry the same dependency -- see
    ``_relativize_repo_paths`` -- but the project name is not a path and cannot
    be relativized away.
    """
    if not LOCKED_COMPOSE.exists():
        return
    try:
        committed = yaml.safe_load(LOCKED_COMPOSE.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemExit(f"cannot read the existing {LOCKED_COMPOSE.name} to verify identity: {exc}") from exc
    if not isinstance(committed, dict):
        # Only a *missing* file can mean "nothing to preserve". A file that
        # exists and does not parse as a mapping is damage, and reading it as
        # an absent baseline would disable the check below on exactly the
        # regeneration that most needs it.
        raise SystemExit(
            f"refusing to write {LOCKED_COMPOSE.name}: the existing file parses as "
            f"{type(committed).__name__}, not a mapping, so the project name it records "
            f"cannot be read. Restore it from git before regenerating."
        )

    old_name, new_name = committed.get("name"), compose.get("name")
    if old_name and new_name != old_name:
        raise SystemExit(
            f"refusing to write {LOCKED_COMPOSE.name}: Compose project name would change "
            f"{old_name!r} -> {new_name!r}. That renames the default network and every named "
            f"volume, so production would come up on empty volumes. Re-run with "
            f"COMPOSE_PROJECT_NAME={old_name}."
        )


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
    restored_count = _restore_parameterized_short_syntax(compose)
    relativized_count = _relativize_repo_paths(compose)
    _assert_no_generator_local_paths(compose)
    _assert_matches_committed_identity(compose)
    output = _render(compose, stripped_count=stripped_count)

    if args.check:
        sys.stdout.write(output)
        return 0

    LOCKED_COMPOSE.write_text(output, encoding="utf-8")
    print(
        f"Generated {LOCKED_COMPOSE.relative_to(REPO_ROOT)} "
        f"({len(output)} bytes, {stripped_count} source mounts stripped, "
        f"{restored_count} parameterized mounts kept in short syntax, "
        f"{relativized_count} paths relativized)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
