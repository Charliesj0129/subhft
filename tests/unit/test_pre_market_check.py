"""Unit tests for scripts/pre_market_check.py.

Each test function mocks external dependencies so no live services are needed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure scripts/ directory is importable
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pre_market_check as pmc  # noqa: E402, I001


# ---------------------------------------------------------------------------
# Helper: build a minimal fake psutil virtual_memory namedtuple
# ---------------------------------------------------------------------------


def _make_vm(available_gb: float, total_gb: float = 16.0) -> MagicMock:
    vm = MagicMock()
    vm.available = int(available_gb * 1024**3)
    vm.total = int(total_gb * 1024**3)
    vm.used = vm.total - vm.available
    return vm


# ---------------------------------------------------------------------------
# check_broker_connectivity
# ---------------------------------------------------------------------------


class TestBrokerCheck:
    """Tests for check_broker_connectivity()."""

    def _make_sj(self, margin: int) -> MagicMock:
        """Build a minimal shioaji mock with the given margin value."""
        api_instance = MagicMock()
        api_instance.login.return_value = None
        api_instance.logout.return_value = None
        # list_accounts returns a list of account objects with .margin attr
        acct = MagicMock()
        acct.margin = margin
        api_instance.list_accounts.return_value = [acct]
        api_instance.Contracts = MagicMock()
        api_instance.Contracts.update.return_value = None

        sj_mock = MagicMock()
        sj_mock.Shioaji.return_value = api_instance
        return sj_mock

    def test_broker_check_passes_with_sufficient_margin(self) -> None:
        """Login succeeds and margin ≥ 15000 → PASS."""
        sj_mock = self._make_sj(margin=50000)
        env = {
            "SHIOAJI_API_KEY": "test_key",
            "SHIOAJI_SECRET_KEY": "test_secret",
        }
        with patch.dict("os.environ", env), patch.object(pmc, "sj", sj_mock):
            # SIGALRM not available on some platforms — patch signal to no-op
            with patch("signal.alarm"), patch("signal.signal"):
                ok, detail = pmc.check_broker_connectivity()
        assert ok is True
        assert "OK" in detail

    def test_broker_check_fails_insufficient_margin(self) -> None:
        """Login succeeds but margin < 15000 → FAIL."""
        sj_mock = self._make_sj(margin=5000)
        env = {
            "SHIOAJI_API_KEY": "test_key",
            "SHIOAJI_SECRET_KEY": "test_secret",
        }
        with patch.dict("os.environ", env), patch.object(pmc, "sj", sj_mock):
            with patch("signal.alarm"), patch("signal.signal"):
                ok, detail = pmc.check_broker_connectivity()
        assert ok is False
        assert "margin" in detail.lower()

    def test_broker_check_fails_missing_credentials(self) -> None:
        """Missing env vars → FAIL immediately without touching SDK."""
        env = {"SHIOAJI_API_KEY": "", "SHIOAJI_SECRET_KEY": ""}
        with patch.dict("os.environ", env, clear=False):
            ok, detail = pmc.check_broker_connectivity()
        assert ok is False
        assert "not set" in detail

    def test_broker_check_fails_when_sdk_absent(self) -> None:
        """No shioaji SDK → FAIL."""
        with patch.object(pmc, "sj", None):
            ok, detail = pmc.check_broker_connectivity()
        assert ok is False
        assert "not installed" in detail


# ---------------------------------------------------------------------------
# check_clickhouse
# ---------------------------------------------------------------------------


class TestClickhouseCheck:
    """Tests for check_clickhouse()."""

    def test_clickhouse_check_passes(self) -> None:
        """SELECT 1 returns '1' and table exists → PASS."""
        responses = iter(["1", "1"])

        def _fake_urlopen(req: object, timeout: int = 10) -> object:
            body = next(responses).encode()
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.read.return_value = body
            return ctx

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            ok, detail = pmc.check_clickhouse()
        assert ok is True
        assert "market_data" in detail

    def test_clickhouse_check_fails_on_network_error(self) -> None:
        """Network error → FAIL."""
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            ok, detail = pmc.check_clickhouse()
        assert ok is False
        assert "unreachable" in detail.lower()

    def test_clickhouse_check_fails_missing_table(self) -> None:
        """SELECT 1 OK but table count is 0 → FAIL."""
        responses = iter(["1", "0"])

        def _fake_urlopen(req: object, timeout: int = 10) -> object:
            body = next(responses).encode()
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.read.return_value = body
            return ctx

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            ok, detail = pmc.check_clickhouse()
        assert ok is False
        assert "does not exist" in detail


# ---------------------------------------------------------------------------
# check_redis
# ---------------------------------------------------------------------------


class TestRedisCheck:
    """Tests for check_redis()."""

    def test_redis_check_passes(self) -> None:
        """redis.ping() returns True → PASS."""
        redis_module = types.ModuleType("redis")
        fake_client = MagicMock()
        fake_client.ping.return_value = True
        fake_client.close.return_value = None
        redis_module.Redis = MagicMock(return_value=fake_client)

        with patch.dict("sys.modules", {"redis": redis_module}):
            ok, detail = pmc.check_redis(host="localhost", port=6379)
        assert ok is True
        assert "OK" in detail

    def test_redis_check_fails_on_connection_error(self) -> None:
        """Connection error → FAIL."""
        redis_module = types.ModuleType("redis")
        fake_client = MagicMock()
        fake_client.ping.side_effect = ConnectionRefusedError("refused")
        redis_module.Redis = MagicMock(return_value=fake_client)

        with patch.dict("sys.modules", {"redis": redis_module}):
            ok, detail = pmc.check_redis(host="localhost", port=6379)
        assert ok is False
        assert "unreachable" in detail.lower()


# ---------------------------------------------------------------------------
# check_disk_space
# ---------------------------------------------------------------------------


class TestDiskSpaceCheck:
    """Tests for check_disk_space()."""

    def _make_usage(self, used_pct: float, total_gb: float = 100.0) -> object:
        total = int(total_gb * 1024**3)
        used = int(total * used_pct / 100)
        free = total - used
        return types.SimpleNamespace(total=total, used=used, free=free)

    def test_disk_check_passes_under_threshold(self) -> None:
        """50% usage → PASS."""
        usage = self._make_usage(50.0)
        with patch("shutil.disk_usage", return_value=usage):
            ok, detail = pmc.check_disk_space(directories=["/tmp"])
        assert ok is True
        assert "50.0%" in detail

    def test_disk_check_fails_over_threshold(self) -> None:
        """90% usage → FAIL."""
        usage = self._make_usage(90.0)
        with patch("shutil.disk_usage", return_value=usage):
            ok, detail = pmc.check_disk_space(directories=["/tmp"])
        assert ok is False
        assert "90.0%" in detail
        assert ">=" in detail or "critical" in detail.lower()

    def test_disk_check_at_exactly_threshold_fails(self) -> None:
        """Exactly 80% usage → FAIL (>= threshold)."""
        usage = self._make_usage(80.0)
        with patch("shutil.disk_usage", return_value=usage):
            ok, detail = pmc.check_disk_space(directories=["/tmp"])
        assert ok is False

    def test_disk_check_just_below_threshold_passes(self) -> None:
        """79.9% usage → PASS."""
        usage = self._make_usage(79.9)
        with patch("shutil.disk_usage", return_value=usage):
            ok, detail = pmc.check_disk_space(directories=["/tmp"])
        assert ok is True


# ---------------------------------------------------------------------------
# check_reconciliation
# ---------------------------------------------------------------------------


class TestReconciliationCheck:
    """Tests for check_reconciliation()."""

    def _make_responses(self, *responses: str):
        """Return a side_effect iterator for urlopen producing sequential responses."""
        resp_iter = iter(responses)

        def _fake_urlopen(req: object, timeout: int = 10) -> object:
            body = next(resp_iter).encode()
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.read.return_value = body
            return ctx

        return _fake_urlopen

    def test_reconciliation_no_table_passes(self) -> None:
        """Table doesn't exist (first run) → PASS."""
        # system.tables returns count=0 (table absent)
        with patch("urllib.request.urlopen", side_effect=self._make_responses("0")):
            ok, detail = pmc.check_reconciliation()
        assert ok is True
        assert "first run" in detail.lower() or "does not exist" in detail.lower()

    def test_reconciliation_match_passes(self) -> None:
        """Table exists, yesterday status = MATCH → PASS."""
        # First query: table count=1; second query: status=MATCH
        with patch("urllib.request.urlopen", side_effect=self._make_responses("1", "MATCH")):
            ok, detail = pmc.check_reconciliation()
        assert ok is True
        assert "MATCH" in detail

    def test_reconciliation_mismatch_fails(self) -> None:
        """Table exists, yesterday status = MISMATCH → FAIL."""
        with patch("urllib.request.urlopen", side_effect=self._make_responses("1", "MISMATCH")):
            ok, detail = pmc.check_reconciliation()
        assert ok is False
        assert "MISMATCH" in detail

    def test_reconciliation_ch_unreachable_passes(self) -> None:
        """ClickHouse unreachable → PASS (check_clickhouse handles outage)."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            ok, detail = pmc.check_reconciliation()
        assert ok is True


# ---------------------------------------------------------------------------
# check_system_resources
# ---------------------------------------------------------------------------


class TestSystemResourcesCheck:
    """Tests for check_system_resources()."""

    def test_system_resources_passes(self) -> None:
        """Sufficient RAM and low CPU → PASS."""
        vm = _make_vm(available_gb=8.0)
        with patch.object(pmc, "psutil", MagicMock()) as mock_psutil:
            mock_psutil.virtual_memory.return_value = vm
            mock_psutil.cpu_percent.return_value = 10.0
            ok, detail = pmc.check_system_resources()
        assert ok is True
        assert "RAM" in detail or "available" in detail.lower() or "8.0" in detail

    def test_system_resources_fails_low_ram(self) -> None:
        """Low RAM → FAIL."""
        vm = _make_vm(available_gb=0.5)
        with patch.object(pmc, "psutil", MagicMock()) as mock_psutil:
            mock_psutil.virtual_memory.return_value = vm
            mock_psutil.cpu_percent.return_value = 10.0
            ok, detail = pmc.check_system_resources()
        assert ok is False
        assert "RAM" in detail or "low" in detail.lower()

    def test_system_resources_fails_high_cpu(self) -> None:
        """High CPU → FAIL."""
        vm = _make_vm(available_gb=8.0)
        with patch.object(pmc, "psutil", MagicMock()) as mock_psutil:
            mock_psutil.virtual_memory.return_value = vm
            mock_psutil.cpu_percent.return_value = 95.0
            ok, detail = pmc.check_system_resources()
        assert ok is False
        assert "CPU" in detail

    def test_system_resources_fails_no_psutil(self) -> None:
        """psutil not installed → FAIL."""
        with patch.object(pmc, "psutil", None):
            ok, detail = pmc.check_system_resources()
        assert ok is False
        assert "psutil" in detail


# ---------------------------------------------------------------------------
# main() integration — minimal smoke tests
# ---------------------------------------------------------------------------


class TestMain:
    """Smoke tests for the main() entry point."""

    def _patch_all_pass(self):
        """Return a context manager patching all 6 checks to PASS."""
        return patch.multiple(
            "pre_market_check",
            check_broker_connectivity=MagicMock(return_value=(True, "login OK")),
            check_clickhouse=MagicMock(return_value=(True, "CH OK")),
            check_redis=MagicMock(return_value=(True, "PING OK")),
            check_disk_space=MagicMock(return_value=(True, "disk OK")),
            check_reconciliation=MagicMock(return_value=(True, "MATCH")),
            check_system_resources=MagicMock(return_value=(True, "resources OK")),
        )

    def test_main_all_pass_exits_0(self) -> None:
        """All checks pass → exit code 0."""
        with self._patch_all_pass():
            with patch("pre_market_check.get_calendar") as mock_cal:
                mock_cal.return_value.is_trading_day.return_value = True
                rc = pmc.main(["--dry-run"])
        assert rc == 0

    def test_main_one_fail_exits_1(self) -> None:
        """One check fails → exit code 1."""
        with patch.multiple(
            "pre_market_check",
            check_broker_connectivity=MagicMock(return_value=(False, "login failed")),
            check_clickhouse=MagicMock(return_value=(True, "CH OK")),
            check_redis=MagicMock(return_value=(True, "PING OK")),
            check_disk_space=MagicMock(return_value=(True, "disk OK")),
            check_reconciliation=MagicMock(return_value=(True, "MATCH")),
            check_system_resources=MagicMock(return_value=(True, "resources OK")),
        ):
            with patch("pre_market_check.get_calendar") as mock_cal:
                mock_cal.return_value.is_trading_day.return_value = True
                rc = pmc.main(["--dry-run"])
        assert rc == 1

    def test_main_non_trading_day_exits_0(self) -> None:
        """Non-trading day → exit 0 without running checks."""
        with patch("pre_market_check.get_calendar") as mock_cal:
            mock_cal.return_value.is_trading_day.return_value = False
            rc = pmc.main(["--dry-run"])
        assert rc == 0
