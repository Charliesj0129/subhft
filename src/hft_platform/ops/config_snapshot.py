"""Startup config snapshot — captures non-secret env vars + config hash to ClickHouse.

Security: uses ALLOWLIST strategy. Only HFT_* vars that are NOT secret are captured.
Defense-in-depth: any var name containing PASSWORD, SECRET, TOKEN, KEY, or CERT is excluded
regardless of prefix.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("ops.config_snapshot")

REDACT_KEYWORDS: frozenset[str] = frozenset(
    {
        "PASSWORD",
        "SECRET",
        "TOKEN",
        "KEY",
        "CERT",
    }
)

_ALLOWED_PREFIXES: tuple[str, ...] = ("HFT_",)


def is_secret_var(name: str) -> bool:
    upper = name.upper()
    return any(kw in upper for kw in REDACT_KEYWORDS)


def collect_allowed_env_vars() -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in sorted(os.environ.items()):
        if not any(name.startswith(p) for p in _ALLOWED_PREFIXES):
            continue
        if is_secret_var(name):
            continue
        result[name] = value
    return result


def _compute_yaml_hash(yaml_paths: list[str]) -> str:
    h = hashlib.sha256()
    for path in sorted(yaml_paths):
        try:
            with open(path, "rb") as f:
                h.update(f.read())
        except FileNotFoundError:
            h.update(f"MISSING:{path}".encode())
    return h.hexdigest()[:16]


def _get_git_sha() -> str:
    # P2-d (2026-04-27): prefer the build-time env var baked by Dockerfile
    # via --build-arg GIT_SHA. The container runtime has no .git directory,
    # so the subprocess fallback below would always return "unknown".
    env_sha = os.environ.get("HFT_GIT_SHA", "").strip()
    if env_sha and env_sha != "unknown":
        # Match the abbreviated form returned by `git rev-parse --short`.
        return env_sha[:7]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def build_snapshot(
    *,
    yaml_paths: list[str] | None = None,
    git_sha: str = "",
) -> dict[str, Any]:
    env_vars = collect_allowed_env_vars()
    return {
        "boot_ts": timebase.now_ns() // 1_000_000,
        "config_hash": _compute_yaml_hash(yaml_paths or []),
        "git_sha": git_sha or _get_git_sha(),
        "env_json": json.dumps(env_vars, ensure_ascii=False),
        "yaml_json": json.dumps(yaml_paths or []),
    }


async def write_snapshot_to_clickhouse(
    ch_client: Any,
    snapshot: dict[str, Any],
) -> bool:
    try:
        ch_client.insert(
            "hft.config_snapshots",
            [
                [
                    snapshot["boot_ts"],
                    snapshot["config_hash"],
                    snapshot["git_sha"],
                    snapshot["env_json"],
                    snapshot["yaml_json"],
                ]
            ],
            column_names=["boot_ts", "config_hash", "git_sha", "env_json", "yaml_json"],
        )
        logger.info("config_snapshot_written", config_hash=snapshot["config_hash"])
        return True
    except Exception:  # noqa: BLE001
        logger.warning("config_snapshot_write_failed", exc_info=True)
        logger.info("config_snapshot_fallback", **snapshot)
        return False
