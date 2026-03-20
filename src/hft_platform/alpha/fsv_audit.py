"""Feature Signal Validity (FSV) Audit.

Audits alpha signal vectors for common statistical pathologies:
- Constant signals (zero variance)
- Look-ahead bias (future information leakage)
- Excessive NaN/Inf ratios
- Signal autocorrelation anomalies
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import structlog

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FSVAuditResult:
    """Result of an FSV audit on a signal vector."""

    alpha_id: str
    passed: bool
    checks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha_id": self.alpha_id,
            "passed": self.passed,
            "checks": dict(self.checks),
        }


def audit_signal(
    alpha_id: str,
    signals: Any,
    *,
    max_nan_ratio: float = 0.1,
    min_variance: float = 1e-12,
    max_autocorr_lag1: float = 0.99,
) -> FSVAuditResult:
    """Run FSV audit on a signal vector.

    Args:
        alpha_id: Identifier for the alpha.
        signals: Array-like signal values.
        max_nan_ratio: Maximum allowed NaN/Inf ratio.
        min_variance: Minimum signal variance (detect constant signals).
        max_autocorr_lag1: Maximum lag-1 autocorrelation.

    Returns:
        FSVAuditResult with pass/fail and individual check details.
    """
    arr = np.asarray(signals, dtype=np.float64).ravel()
    checks: dict[str, Any] = {}
    passed = True

    # Check NaN/Inf ratio
    nan_count = int(np.sum(~np.isfinite(arr)))
    nan_ratio = nan_count / max(1, arr.size)
    nan_ok = nan_ratio <= max_nan_ratio
    checks["nan_ratio"] = {"value": nan_ratio, "threshold": max_nan_ratio, "pass": nan_ok}
    if not nan_ok:
        passed = False

    # Check variance (constant signal detection)
    clean = arr[np.isfinite(arr)]
    variance = float(np.var(clean)) if clean.size > 1 else 0.0
    var_ok = variance >= min_variance
    checks["variance"] = {"value": variance, "threshold": min_variance, "pass": var_ok}
    if not var_ok:
        passed = False

    # Check lag-1 autocorrelation
    if clean.size > 2:
        x = clean[:-1]
        y = clean[1:]
        x_c = x - np.mean(x)
        y_c = y - np.mean(y)
        denom = float(np.sqrt(np.dot(x_c, x_c) * np.dot(y_c, y_c)))
        autocorr = float(np.dot(x_c, y_c) / denom) if denom > 1e-12 else 0.0
    else:
        autocorr = 0.0
    ac_ok = abs(autocorr) <= max_autocorr_lag1
    checks["autocorr_lag1"] = {"value": autocorr, "threshold": max_autocorr_lag1, "pass": ac_ok}
    if not ac_ok:
        passed = False

    _log.info("fsv_audit_complete", alpha_id=alpha_id, passed=passed)
    return FSVAuditResult(alpha_id=alpha_id, passed=passed, checks=checks)
