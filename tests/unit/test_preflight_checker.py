"""Tests for pre-market health check system."""
from __future__ import annotations

import asyncio

import pytest


class TestCheckResult:
    def test_enum_values(self):
        from hft_platform.ops.preflight_checker import CheckResult
        assert CheckResult.PASS.value == "pass"
        assert CheckResult.WARN.value == "warn"
        assert CheckResult.FAIL.value == "fail"


class TestPreflightChecker:
    @pytest.mark.asyncio
    async def test_all_checks_pass(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker
        async def _pass(): return CheckResult.PASS
        checker = PreflightChecker(checks=[
            PreflightCheck(name="broker_login", check_fn=_pass, required=True, timeout_s=5.0),
            PreflightCheck(name="redis_alive", check_fn=_pass, required=False, timeout_s=5.0),
        ])
        report = await checker.run_all()
        assert report.passed is True
        assert len(report.results) == 2

    @pytest.mark.asyncio
    async def test_required_check_fails_blocks(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker
        async def _pass(): return CheckResult.PASS
        async def _fail(): return CheckResult.FAIL
        checker = PreflightChecker(checks=[
            PreflightCheck(name="broker_login", check_fn=_fail, required=True, timeout_s=5.0),
            PreflightCheck(name="redis_alive", check_fn=_pass, required=False, timeout_s=5.0),
        ])
        report = await checker.run_all()
        assert report.passed is False
        assert report.failed_required == ["broker_login"]

    @pytest.mark.asyncio
    async def test_optional_check_fails_still_passes(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker
        async def _pass(): return CheckResult.PASS
        async def _fail(): return CheckResult.FAIL
        checker = PreflightChecker(checks=[
            PreflightCheck(name="broker_login", check_fn=_pass, required=True, timeout_s=5.0),
            PreflightCheck(name="redis_alive", check_fn=_fail, required=False, timeout_s=5.0),
        ])
        report = await checker.run_all()
        assert report.passed is True

    @pytest.mark.asyncio
    async def test_warn_result_passes_but_recorded(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker
        async def _warn(): return CheckResult.WARN
        checker = PreflightChecker(checks=[
            PreflightCheck(name="disk_space", check_fn=_warn, required=True, timeout_s=5.0),
        ])
        report = await checker.run_all()
        assert report.passed is True
        assert report.warnings == ["disk_space"]

    @pytest.mark.asyncio
    async def test_timeout_treated_as_fail(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker
        async def _slow():
            await asyncio.sleep(10)
            return CheckResult.PASS
        checker = PreflightChecker(checks=[
            PreflightCheck(name="slow_check", check_fn=_slow, required=True, timeout_s=0.05),
        ])
        report = await checker.run_all()
        assert report.passed is False
        assert report.failed_required == ["slow_check"]

    @pytest.mark.asyncio
    async def test_exception_treated_as_fail(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker
        async def _raise(): raise ConnectionError("cannot connect")
        checker = PreflightChecker(checks=[
            PreflightCheck(name="broken_check", check_fn=_raise, required=True, timeout_s=5.0),
        ])
        report = await checker.run_all()
        assert report.passed is False
        assert "broken_check" in report.failed_required
