#!/usr/bin/env python3
"""Pre-market 6-point health check script for HFT platform.

Runs before strategy start (cron 08:15 weekdays). Checks:
  1. Broker connectivity (Shioaji login + margin)
  2. ClickHouse availability
  3. Redis availability
  4. Disk space (WAL/logs/data < 80%)
  5. Reconciliation status (yesterday)
  6. System resources (RAM/CPU)

Usage:
    python scripts/pre_market_check.py [--dry-run]

Exit codes:
    0  All checks passed
    1  One or more checks failed
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import structlog

# ---------------------------------------------------------------------------
# Platform imports — guarded so tests can mock easily
# ---------------------------------------------------------------------------
try:
    from hft_platform.core import timebase  # noqa: F401 — validates import
    from hft_platform.core.market_calendar import get_calendar
except ImportError:  # pragma: no cover
    get_calendar = None  # type: ignore[assignment]

try:
    from hft_platform.notifications.dispatcher import NotificationDispatcher
    from hft_platform.notifications.telegram import TelegramSender
except ImportError:  # pragma: no cover
    NotificationDispatcher = None  # type: ignore[assignment]
    TelegramSender = None  # type: ignore[assignment]

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

logger = structlog.get_logger("scripts.pre_market_check")

# ---------------------------------------------------------------------------
# Shioaji SDK — optional; guarded import so platform can run without it
# ---------------------------------------------------------------------------
try:
    import shioaji as sj
except ImportError:  # pragma: no cover
    sj = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DISK_WARN_PCT = 80  # percent used threshold
RAM_MIN_GB = 2.0
CPU_MAX_PCT = 80.0
BROKER_TIMEOUT_S = 30
CH_TIMEOUT_S = 10
REDIS_TIMEOUT_S = 5

_REPO_ROOT = Path(__file__).parent.parent
_WAL_DIR = _REPO_ROOT / ".wal"
_LOGS_DIR = _REPO_ROOT / "logs"
_DATA_DIR = _REPO_ROOT / "data"


# ---------------------------------------------------------------------------
# Individual check functions — each returns (ok: bool, detail: str)
# ---------------------------------------------------------------------------


def check_broker_connectivity() -> tuple[bool, str]:
    """Login to Shioaji, activate CA, fetch contract, check margin ≥ 15000, logout.

    Returns:
        (True, detail) on success; (False, reason) on failure.
    """
    if sj is None:
        return False, "shioaji SDK not installed"

    api_key = os.environ.get("SHIOAJI_API_KEY", "")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY", "")
    ca_path = os.environ.get("SHIOAJI_CA_PATH", "")
    ca_passwd = os.environ.get("SHIOAJI_CA_PASSWORD", "")
    person_id = os.environ.get("SHIOAJI_PERSON_ID", "")

    if not api_key or not secret_key:
        return False, "SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY not set"

    import signal

    class _Timeout(Exception):
        pass

    def _handler(signum: int, frame: object) -> None:  # type: ignore[type-arg]
        raise _Timeout

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(BROKER_TIMEOUT_S)
    api_obj = None
    try:
        api_obj = sj.Shioaji()
        api_obj.login(api_key=api_key, secret_key=secret_key, fetch_contract=False)

        if ca_path and ca_passwd and person_id:
            try:
                api_obj.activate_ca(
                    ca_path=ca_path,
                    ca_passwd=ca_passwd,
                    person_id=person_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("broker_check.activate_ca_failed", error=str(exc))

        # Fetch one contract to validate connectivity
        try:
            api_obj.Contracts.update()
        except Exception:  # noqa: BLE001
            pass  # Non-fatal — connectivity is proven by login

        # Check margin
        margin: int = 0
        try:
            accounts = api_obj.list_accounts()
            for acct in accounts or []:
                if hasattr(acct, "margin"):
                    margin = max(margin, int(acct.margin))
        except Exception as exc:  # noqa: BLE001
            logger.warning("broker_check.margin_fetch_failed", error=str(exc))

        if margin > 0 and margin < 15000:
            return False, f"margin too low: {margin} < 15000"

        margin_info = f"margin={margin}" if margin > 0 else "margin=N/A"
        return True, f"login OK, {margin_info}"

    except _Timeout:
        return False, f"timeout after {BROKER_TIMEOUT_S}s"
    except Exception as exc:  # noqa: BLE001
        return False, f"login failed: {exc}"
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        if api_obj is not None:
            try:
                api_obj.logout()
            except Exception:  # noqa: BLE001
                pass


def check_clickhouse(
    clickhouse_url: str = "http://localhost:8123",
    password: str = "",
) -> tuple[bool, str]:
    """SELECT 1 and verify hft.market_data table exists.

    Args:
        clickhouse_url: ClickHouse HTTP endpoint.
        password: Optional ClickHouse password.

    Returns:
        (True, detail) on success; (False, reason) on failure.
    """

    def _query(q: str) -> tuple[str | None, str | None]:
        params: dict[str, str] = {"query": q}
        if password:
            params["password"] = password
        url = clickhouse_url.rstrip("/") + "/?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=CH_TIMEOUT_S) as resp:
                return resp.read().decode("utf-8").strip(), None
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    result, err = _query("SELECT 1")
    if err is not None:
        return False, f"ClickHouse unreachable: {err}"
    if result != "1":
        return False, f"SELECT 1 returned unexpected: {result!r}"

    result2, err2 = _query(
        "SELECT count() FROM system.tables WHERE database='hft' AND name='market_data'"
    )
    if err2 is not None:
        return False, f"table existence check failed: {err2}"
    try:
        count = int(result2 or "0")
    except ValueError:
        return False, f"unexpected table count response: {result2!r}"
    if count == 0:
        return False, "hft.market_data table does not exist"

    return True, "SELECT 1 OK, hft.market_data exists"


def check_redis(
    host: str = "",
    port: int = 0,
    password: str = "",
) -> tuple[bool, str]:
    """PING Redis and expect PONG.

    Args:
        host: Redis host (default: HFT_MONITOR_REDIS_HOST or localhost).
        port: Redis port (default: HFT_MONITOR_REDIS_PORT or 6379).
        password: Optional Redis password.

    Returns:
        (True, detail) on success; (False, reason) on failure.
    """
    _host = host or os.environ.get("HFT_MONITOR_REDIS_HOST", "localhost")
    _port = port or int(os.environ.get("HFT_MONITOR_REDIS_PORT", "6379"))
    _password = password or os.environ.get("HFT_MONITOR_REDIS_PASSWORD", "")

    try:
        import redis  # type: ignore[import-untyped]
    except ImportError:
        return False, "redis-py not installed"

    try:
        client = redis.Redis(
            host=_host,
            port=_port,
            password=_password or None,
            socket_connect_timeout=REDIS_TIMEOUT_S,
            socket_timeout=REDIS_TIMEOUT_S,
        )
        response = client.ping()
        client.close()
        if response:
            return True, f"PING OK ({_host}:{_port})"
        return False, "PING returned falsy"
    except Exception as exc:  # noqa: BLE001
        return False, f"Redis unreachable: {exc}"


def check_disk_space(
    directories: list[str] | None = None,
) -> tuple[bool, str]:
    """Check that WAL/logs/data directories have < 80% disk usage.

    Args:
        directories: Paths to check (default: .wal, logs, data).

    Returns:
        (True, detail) on success; (False, reason) on failure.
    """
    if directories is None:
        dirs_to_check: list[Path] = [_WAL_DIR, _LOGS_DIR, _DATA_DIR]
    else:
        dirs_to_check = [Path(d) for d in directories]

    # Deduplicate by mount point (resolves symlinks)
    seen_mounts: set[str] = set()
    results: list[str] = []
    failed: list[str] = []

    for directory in dirs_to_check:
        # Use parent if dir doesn't exist (still valid mount point check)
        check_path = directory if directory.exists() else directory.parent
        try:
            usage = shutil.disk_usage(str(check_path))
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{directory}: {exc}")
            continue

        mount_key = str(check_path.resolve())
        if mount_key in seen_mounts:
            continue
        seen_mounts.add(mount_key)

        used_pct = (usage.used / usage.total) * 100 if usage.total > 0 else 0
        free_gb = usage.free / (1024**3)
        info = f"{directory.name}: {used_pct:.1f}% used ({free_gb:.1f}GB free)"
        results.append(info)

        if used_pct >= DISK_WARN_PCT:
            failed.append(f"{directory.name}: {used_pct:.1f}% >= {DISK_WARN_PCT}%")

    if failed:
        return False, "disk space critical: " + "; ".join(failed)

    detail = ", ".join(results) if results else "all directories OK"
    return True, detail


def check_reconciliation(
    clickhouse_url: str = "http://localhost:8123",
    password: str = "",
) -> tuple[bool, str]:
    """Query hft.reconciliation for yesterday; status must be MATCH.

    If the reconciliation table doesn't exist yet, PASS (first run).

    Args:
        clickhouse_url: ClickHouse HTTP endpoint.
        password: Optional ClickHouse password.

    Returns:
        (True, detail) on success; (False, reason) on failure.
    """
    # Check table existence first
    def _query(q: str) -> tuple[str | None, str | None]:
        params: dict[str, str] = {"query": q}
        if password:
            params["password"] = password
        url = clickhouse_url.rstrip("/") + "/?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=CH_TIMEOUT_S) as resp:
                return resp.read().decode("utf-8").strip(), None
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    table_check, err = _query(
        "SELECT count() FROM system.tables WHERE database='hft' AND name='reconciliation'"
    )
    if err is not None:
        # ClickHouse unreachable — treat as first run PASS (check_clickhouse covers outage)
        return True, "ClickHouse unreachable — skip reconciliation check"

    try:
        table_count = int(table_check or "0")
    except ValueError:
        table_count = 0

    if table_count == 0:
        return True, "hft.reconciliation table does not exist (first run — skipping)"

    # Query yesterday's reconciliation status
    result, err2 = _query(
        "SELECT status FROM hft.reconciliation "
        "WHERE toDate(session_date) = yesterday() "
        "ORDER BY created_at DESC LIMIT 1"
    )
    if err2 is not None:
        return False, f"reconciliation query failed: {err2}"

    if not result:
        return True, "no reconciliation record for yesterday (first run — skipping)"

    if result.strip().upper() == "MATCH":
        return True, f"reconciliation yesterday: {result.strip()}"

    return False, f"reconciliation yesterday: {result.strip()} (expected MATCH)"


def check_system_resources() -> tuple[bool, str]:
    """Check RAM > 2GB available and CPU < 80%.

    Returns:
        (True, detail) on success; (False, reason) on failure.
    """
    if psutil is None:
        return False, "psutil not installed"

    issues: list[str] = []
    details: list[str] = []

    # RAM check
    try:
        vm = psutil.virtual_memory()
        available_gb = vm.available / (1024**3)
        details.append(f"RAM available={available_gb:.1f}GB")
        if available_gb < RAM_MIN_GB:
            issues.append(f"RAM low: {available_gb:.1f}GB < {RAM_MIN_GB}GB required")
    except Exception as exc:  # noqa: BLE001
        issues.append(f"RAM check failed: {exc}")

    # CPU check (non-blocking 0.5s interval)
    try:
        cpu_pct = psutil.cpu_percent(interval=0.5)
        details.append(f"CPU={cpu_pct:.1f}%")
        if cpu_pct >= CPU_MAX_PCT:
            issues.append(f"CPU high: {cpu_pct:.1f}% >= {CPU_MAX_PCT}%")
    except Exception as exc:  # noqa: BLE001
        issues.append(f"CPU check failed: {exc}")

    detail_str = ", ".join(details)
    if issues:
        return False, "; ".join(issues) + f" ({detail_str})"
    return True, detail_str


# ---------------------------------------------------------------------------
# Notification helper
# ---------------------------------------------------------------------------


async def _send_notifications(
    passed: bool,
    failed_names: list[str],
    dry_run: bool,
) -> None:
    """Send pass/fail notification via Telegram dispatcher."""
    if dry_run:
        logger.info("pre_market_check.dry_run_skip_notify", passed=passed, failed=failed_names)
        return

    if TelegramSender is None or NotificationDispatcher is None:
        logger.warning("pre_market_check.notifications_unavailable")
        return

    token = os.environ.get("HFT_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("HFT_TELEGRAM_CHAT_ID", "")
    enabled = bool(token and chat_id)

    sender = TelegramSender(bot_token=token, chat_id=chat_id, enabled=enabled)
    dispatcher = NotificationDispatcher(sender)

    try:
        if passed:
            await dispatcher.notify_pre_market_pass()
        else:
            await dispatcher.notify_pre_market_fail(failed_checks=failed_names)
    finally:
        await sender.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run all 6 pre-market checks and notify.

    Args:
        argv: CLI arguments (default: sys.argv[1:]).

    Returns:
        0 if all checks pass, 1 if any fail.
    """
    parser = argparse.ArgumentParser(
        description="Pre-market 6-point health check for HFT platform"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run checks but skip sending Telegram notifications",
    )
    parser.add_argument(
        "--clickhouse-url",
        default=os.environ.get("HFT_CLICKHOUSE_URL", "http://localhost:8123"),
        help="ClickHouse HTTP URL (default: http://localhost:8123)",
    )
    parser.add_argument(
        "--clickhouse-password",
        default=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        help="ClickHouse password (default: CLICKHOUSE_PASSWORD env var)",
    )
    args = parser.parse_args(argv)

    # ------------------------------------------------------------------
    # Trading day guard
    # ------------------------------------------------------------------
    if get_calendar is not None:
        try:
            cal = get_calendar()
            if not cal.is_trading_day():
                logger.info("pre_market_check.non_trading_day_skip")
                print("Not a trading day — skipping pre-market checks.")
                return 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("pre_market_check.calendar_check_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Define checks
    # ------------------------------------------------------------------
    checks: list[tuple[str, object]] = [
        ("broker_connectivity", lambda: check_broker_connectivity()),
        ("clickhouse", lambda: check_clickhouse(args.clickhouse_url, args.clickhouse_password)),
        ("redis", lambda: check_redis()),
        ("disk_space", lambda: check_disk_space()),
        ("reconciliation", lambda: check_reconciliation(args.clickhouse_url, args.clickhouse_password)),
        ("system_resources", lambda: check_system_resources()),
    ]

    results: list[tuple[str, bool, str]] = []
    failed_names: list[str] = []

    # ------------------------------------------------------------------
    # Run checks
    # ------------------------------------------------------------------
    print("")
    print("=" * 72)
    print("  PRE-MARKET HEALTH CHECK")
    print("=" * 72)
    print(f"  {'Check':<28} {'Status':<8} {'Detail'}")
    print("-" * 72)

    for name, fn in checks:
        try:
            ok, detail = fn()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"unhandled error: {exc}"
            logger.exception("pre_market_check.check_error", check=name, error=str(exc))

        results.append((name, ok, detail))
        if not ok:
            failed_names.append(name)

        tag = "PASS" if ok else "FAIL"
        print(f"  {name:<28} {tag:<8} {detail}")
        logger.info(
            "pre_market_check.result",
            check=name,
            ok=ok,
            detail=detail,
        )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print("-" * 72)
    pass_count = sum(1 for _, ok, _ in results if ok)
    fail_count = len(failed_names)
    print(f"  Total: {len(results)} checks | pass={pass_count} fail={fail_count}")
    print("=" * 72)

    all_passed = fail_count == 0
    if all_passed:
        print("RESULT: PASS — all pre-market checks passed")
    else:
        print(f"RESULT: FAIL — {fail_count} check(s) failed: {', '.join(failed_names)}")

    print("")

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    asyncio.run(
        _send_notifications(
            passed=all_passed,
            failed_names=failed_names,
            dry_run=args.dry_run,
        )
    )

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
