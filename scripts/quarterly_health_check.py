#!/usr/bin/env python3
"""scripts/quarterly_health_check.py — Automated quarterly infrastructure health check.

Checks: ClickHouse TTL, Prometheus storage, OS updates, SMART, Shioaji SDK pin.
Outputs JSON report + optional Telegram summary.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    status: str  # PASS, WARN, FAIL
    detail: str


@dataclass
class QuarterlyReport:
    checks: list[CheckResult] = field(default_factory=list)

    def summary(self) -> str:
        lines = ["<b>Quarterly Health Check</b>\n"]
        for c in self.checks:
            icon = {"PASS": "\u2705", "WARN": "\u26a0\ufe0f", "FAIL": "\u274c"}[c.status]
            lines.append(f"{icon} <b>{c.name}</b>: {c.status}\n  {c.detail}")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps([asdict(c) for c in self.checks], indent=2)


def check_clickhouse_ttl() -> CheckResult:
    try:
        from clickhouse_driver import Client
        host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
        port = int(os.getenv("HFT_CLICKHOUSE_PORT", "9000"))
        client = Client(host=host, port=port)
        rows = client.execute(
            "SELECT count() FROM hft.market_data "
            "WHERE toDateTime(ingest_ts / 1000000000) < now() - INTERVAL 6 MONTH"
        )
        count = rows[0][0] if rows else 0
        if count == 0:
            return CheckResult("ClickHouse TTL", "PASS", "No expired rows found")
        return CheckResult("ClickHouse TTL", "FAIL", f"{count} rows older than 6 months")
    except Exception as exc:
        return CheckResult("ClickHouse TTL", "WARN", f"Cannot connect: {exc}")


def check_prometheus_storage() -> CheckResult:
    try:
        import urllib.request
        prom_url = os.getenv("HFT_PROMETHEUS_URL", "http://localhost:9091")
        url = f"{prom_url}/api/v1/query?query=prometheus_tsdb_storage_size_bytes"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        results = data.get("data", {}).get("result", [])
        if not results:
            return CheckResult("Prometheus Storage", "WARN", "No metric found")
        size_bytes = float(results[0]["value"][1])
        size_gb = size_bytes / (1024**3)
        status = "PASS" if size_gb < 10 else ("WARN" if size_gb < 20 else "FAIL")
        return CheckResult("Prometheus Storage", status, f"{size_gb:.1f} GB")
    except Exception as exc:
        return CheckResult("Prometheus Storage", "WARN", f"Cannot query: {exc}")


def check_os_updates() -> CheckResult:
    try:
        result = subprocess.run(
            ["apt", "list", "--upgradable"],
            capture_output=True, text=True, timeout=30
        )
        lines = [l for l in result.stdout.strip().split("\n") if "/" in l]
        count = len(lines)
        if count == 0:
            return CheckResult("OS Updates", "PASS", "System up to date")
        status = "WARN" if count < 50 else "FAIL"
        return CheckResult("OS Updates", status, f"{count} packages upgradable")
    except Exception as exc:
        return CheckResult("OS Updates", "WARN", f"Cannot check: {exc}")


def check_smart() -> CheckResult:
    script = Path(__file__).parent / "smart_check.sh"
    if not script.exists():
        return CheckResult("SMART Health", "WARN", "smart_check.sh not found")
    try:
        subprocess.run([str(script)], capture_output=True, timeout=30, check=True)
        prom_file = Path("/var/lib/node-exporter/textfile/smartmon.prom")
        if prom_file.exists():
            content = prom_file.read_text()
            realloc_match = re.search(r"smartmon_reallocated_sectors\{.*\}\s+(\d+)", content)
            realloc = int(realloc_match.group(1)) if realloc_match else 0
            if realloc > 100:
                return CheckResult("SMART Health", "FAIL", f"Reallocated sectors: {realloc}")
            if realloc > 0:
                return CheckResult("SMART Health", "WARN", f"Reallocated sectors: {realloc}")
            return CheckResult("SMART Health", "PASS", "No reallocated sectors")
        return CheckResult("SMART Health", "WARN", "Textfile not generated")
    except Exception as exc:
        return CheckResult("SMART Health", "WARN", f"Script failed: {exc}")


def check_shioaji_pin() -> CheckResult:
    repo_root = Path(__file__).parent.parent
    pyproject = repo_root / "pyproject.toml"
    lock_file = repo_root / "uv.lock"

    if not pyproject.exists():
        return CheckResult("Shioaji SDK Pin", "WARN", "pyproject.toml not found")

    pyproject_text = pyproject.read_text()
    pin_match = re.search(r'shioaji\[speed\]==([0-9.]+)', pyproject_text)
    if not pin_match:
        return CheckResult("Shioaji SDK Pin", "FAIL", "Not pinned (missing ==X.Y.Z)")
    pinned_version = pin_match.group(1)

    if lock_file.exists():
        lock_text = lock_file.read_text()
        lock_match = re.search(r'name = "shioaji"\nversion = "([0-9.]+)"', lock_text)
        if lock_match:
            locked_version = lock_match.group(1)
            if locked_version != pinned_version:
                return CheckResult(
                    "Shioaji SDK Pin", "WARN",
                    f"Pin={pinned_version} but lock={locked_version} — run uv lock"
                )

    return CheckResult("Shioaji SDK Pin", "PASS", f"Pinned at {pinned_version}")


def main() -> None:
    report = QuarterlyReport()
    report.checks.append(check_clickhouse_ttl())
    report.checks.append(check_prometheus_storage())
    report.checks.append(check_os_updates())
    report.checks.append(check_smart())
    report.checks.append(check_shioaji_pin())

    print(report.to_json())

    print("\n" + "=" * 60)
    print(report.summary())

    if os.getenv("HFT_TELEGRAM_BOT_TOKEN") and os.getenv("HFT_TELEGRAM_CHAT_ID"):
        try:
            import asyncio
            from hft_platform.notifications.telegram import TelegramSender

            async def _send() -> None:
                sender = TelegramSender(enabled=True)
                await sender.send(report.summary())
                await sender.close()

            asyncio.run(_send())
            print("\nTelegram notification sent.")
        except Exception as exc:
            print(f"\nTelegram send failed: {exc}")

    if any(c.status == "FAIL" for c in report.checks):
        sys.exit(1)


if __name__ == "__main__":
    main()
