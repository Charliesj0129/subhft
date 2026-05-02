"""PreflightChecker — pre-market health check system."""

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog

logger = structlog.get_logger("ops.preflight_checker")


class CheckResult(enum.Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(slots=True, frozen=True)
class PreflightCheck:
    name: str
    check_fn: Callable[[], Awaitable[CheckResult]]
    required: bool
    timeout_s: float


@dataclass(slots=True)
class CheckOutcome:
    name: str
    result: CheckResult
    required: bool
    error: str | None = None
    duration_ms: float = 0.0


@dataclass(slots=True)
class PreflightReport:
    passed: bool
    results: list[CheckOutcome] = field(default_factory=list)
    failed_required: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PreflightChecker:
    __slots__ = ("_checks",)

    def __init__(self, checks: list[PreflightCheck] | None = None) -> None:
        self._checks: list[PreflightCheck] = checks or []

    def add_check(self, check: PreflightCheck) -> None:
        self._checks.append(check)

    async def run_all(self) -> PreflightReport:
        outcomes: list[CheckOutcome] = []
        for check in self._checks:
            outcome = await self._run_single(check)
            outcomes.append(outcome)

        failed_required = [o.name for o in outcomes if o.required and o.result == CheckResult.FAIL]
        warnings = [o.name for o in outcomes if o.result == CheckResult.WARN]
        passed = len(failed_required) == 0

        report = PreflightReport(
            passed=passed,
            results=outcomes,
            failed_required=failed_required,
            warnings=warnings,
        )
        logger.info(
            "preflight_complete",
            passed=passed,
            total=len(outcomes),
            failed_required=failed_required,
            warnings=warnings,
        )
        return report

    @staticmethod
    async def _run_single(check: PreflightCheck) -> CheckOutcome:
        import time

        start = time.monotonic()
        try:
            result = await asyncio.wait_for(check.check_fn(), timeout=check.timeout_s)
            elapsed = (time.monotonic() - start) * 1000
            return CheckOutcome(
                name=check.name,
                result=result,
                required=check.required,
                duration_ms=elapsed,
            )
        except asyncio.TimeoutError:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning(
                "preflight_check_timeout",
                name=check.name,
                timeout_s=check.timeout_s,
            )
            return CheckOutcome(
                name=check.name,
                result=CheckResult.FAIL,
                required=check.required,
                error=f"timeout after {check.timeout_s}s",
                duration_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("preflight_check_error", name=check.name, error=str(exc))
            return CheckOutcome(
                name=check.name,
                result=CheckResult.FAIL,
                required=check.required,
                error=str(exc),
                duration_ms=elapsed,
            )
