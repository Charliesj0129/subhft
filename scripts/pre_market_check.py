#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


def _check_docker_health() -> tuple[str, str]:
    """Verify all Docker Compose services are running and healthy."""
    try:
        proc = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode != 0:
            return STATUS_FAIL, f"docker compose ps failed: {proc.stderr.strip() or proc.stdout.strip()}"

        payload = proc.stdout.strip()
        if not payload:
            return STATUS_FAIL, "no services found"

        rows: list[dict] = []
        if payload.startswith("["):
            rows = json.loads(payload)
        else:
            for line in payload.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not rows:
            return STATUS_FAIL, "no services found in docker compose output"

        issues: list[str] = []
        for svc in rows:
            name = str(svc.get("Service") or svc.get("Name") or "unknown")
            state = str(svc.get("State") or "").lower()
            health = str(svc.get("Health") or "").lower()
            if state != "running":
                issues.append(f"{name}: state={state}")
            elif health and health not in {"healthy", ""}:
                issues.append(f"{name}: health={health}")

        if issues:
            return STATUS_FAIL, "; ".join(issues)
        return STATUS_PASS, f"{len(rows)} services running and healthy"
    except Exception as exc:
        return STATUS_FAIL, str(exc)


def _check_clickhouse(clickhouse_url: str) -> tuple[str, str]:
    """HTTP GET to ClickHouse with SELECT 1 query."""
    try:
        url = clickhouse_url.rstrip("/") + "/?query=SELECT+1"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8").strip()
        if body == "1":
            return STATUS_PASS, "SELECT 1 returned 1"
        return STATUS_FAIL, f"unexpected response: {body!r}"
    except Exception as exc:
        return STATUS_FAIL, str(exc)


def _check_redis() -> tuple[str, str]:
    """Run redis-cli ping and expect PONG."""
    try:
        proc = subprocess.run(
            ["redis-cli", "ping"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = proc.stdout.strip()
        if output == "PONG":
            return STATUS_PASS, "redis-cli ping returned PONG"
        return STATUS_FAIL, f"unexpected response: {output!r} (stderr: {proc.stderr.strip()!r})"
    except FileNotFoundError:
        return STATUS_FAIL, "redis-cli not found"
    except Exception as exc:
        return STATUS_FAIL, str(exc)


def _check_wal_disk(wal_dir: str) -> tuple[str, str]:
    """Check .wal/ directory total size. WARN > 1GB, FAIL > 5GB."""
    wal_path = Path(wal_dir)
    if not wal_path.exists():
        return STATUS_PASS, "WAL directory does not exist (no backlog)"

    total_bytes = 0
    file_count = 0
    try:
        for f in wal_path.rglob("*"):
            if f.is_file():
                total_bytes += f.stat().st_size
                file_count += 1
    except Exception as exc:
        return STATUS_FAIL, f"error scanning WAL dir: {exc}"

    total_mb = total_bytes / (1024 * 1024)
    total_gb = total_bytes / (1024 * 1024 * 1024)
    detail = f"{file_count} files, {total_mb:.1f} MB"

    if total_gb > 5.0:
        return STATUS_FAIL, f"{detail} (>5 GB)"
    if total_gb > 1.0:
        return STATUS_WARN, f"{detail} (>1 GB)"
    return STATUS_PASS, detail


def _check_metrics_endpoint(metrics_url: str) -> tuple[str, str]:
    """HTTP GET to Prometheus metrics endpoint, expect 200."""
    try:
        req = urllib.request.Request(metrics_url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            status_code = resp.getcode()
        if status_code == 200:
            return STATUS_PASS, "metrics endpoint returned 200"
        return STATUS_FAIL, f"unexpected status code: {status_code}"
    except Exception as exc:
        return STATUS_FAIL, str(exc)


def _check_config_git_status() -> tuple[str, str]:
    """Check for uncommitted config changes."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "config/"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = proc.stdout.strip()
        if proc.returncode != 0:
            return STATUS_WARN, f"git diff failed: {proc.stderr.strip()}"
        if not output:
            return STATUS_PASS, "no uncommitted config changes"
        changed = output.splitlines()
        return STATUS_WARN, f"{len(changed)} uncommitted config file(s): {', '.join(changed)}"
    except Exception as exc:
        return STATUS_FAIL, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-market daily check for HFT platform")
    parser.add_argument("--wal-dir", default=".wal", help="WAL directory path (default: .wal)")
    parser.add_argument(
        "--metrics-url",
        default="http://localhost:9090/metrics",
        help="Prometheus metrics endpoint URL",
    )
    parser.add_argument(
        "--clickhouse-url",
        default="http://localhost:8123",
        help="ClickHouse HTTP URL",
    )
    args = parser.parse_args(argv)

    checks: list[tuple[str, str, str]] = []

    check_fns: list[tuple[str, object]] = [
        ("Docker health", lambda: _check_docker_health()),
        ("ClickHouse health", lambda: _check_clickhouse(args.clickhouse_url)),
        ("Redis health", lambda: _check_redis()),
        ("WAL disk usage", lambda: _check_wal_disk(args.wal_dir)),
        ("Metrics endpoint", lambda: _check_metrics_endpoint(args.metrics_url)),
        ("Config git status", lambda: _check_config_git_status()),
    ]

    for name, fn in check_fns:
        try:
            status, detail = fn()  # type: ignore[operator]
        except Exception as exc:
            status, detail = STATUS_FAIL, f"unhandled error: {exc}"
        checks.append((name, status, detail))

    # Print summary table
    print("")
    print("=" * 72)
    print("  PRE-MARKET CHECK SUMMARY")
    print("=" * 72)
    print(f"  {'Check':<25} {'Status':<8} {'Detail'}")
    print("-" * 72)
    has_fail = False
    for name, status, detail in checks:
        tag = status.upper()
        print(f"  {name:<25} {tag:<8} {detail}")
        if status == STATUS_FAIL:
            has_fail = True
    print("-" * 72)

    pass_count = sum(1 for _, s, _ in checks if s == STATUS_PASS)
    warn_count = sum(1 for _, s, _ in checks if s == STATUS_WARN)
    fail_count = sum(1 for _, s, _ in checks if s == STATUS_FAIL)
    print(f"  Total: {len(checks)} checks | pass={pass_count} warn={warn_count} fail={fail_count}")
    print("=" * 72)
    print("")

    if has_fail:
        print("RESULT: FAIL -- resolve issues before market open")
        return 1
    if warn_count > 0:
        print("RESULT: WARN -- review warnings before market open")
    else:
        print("RESULT: PASS -- all pre-market checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
