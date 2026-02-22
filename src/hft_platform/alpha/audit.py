"""Best-effort ClickHouse audit logging for alpha pipeline events.

Controlled by environment variables:
- HFT_ALPHA_AUDIT_ENABLED=0|1  (default 0, opt-in)
- HFT_CLICKHOUSE_HOST           (default localhost)
- HFT_CLICKHOUSE_PORT           (default 8123)
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from structlog import get_logger

if TYPE_CHECKING:
    from hft_platform.alpha.promotion import PromotionResult
    from hft_platform.alpha.validation import GateReport

logger = get_logger("alpha_audit")

_ENABLED: bool | None = None


def _is_enabled() -> bool:
    global _ENABLED  # noqa: PLW0603
    if _ENABLED is None:
        _ENABLED = os.getenv("HFT_ALPHA_AUDIT_ENABLED", "0") == "1"
    return _ENABLED


def _get_client() -> Any:
    import clickhouse_connect

    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    return clickhouse_connect.get_client(host=host, port=port)


def log_gate_result(
    alpha_id: str,
    run_id: str | None,
    gate_report: GateReport,
    config_hash: str | None,
) -> None:
    """Insert one row into audit.alpha_gate_log. Fails silently on error."""
    if not _is_enabled():
        return
    try:
        client = _get_client()
        gate_letter = gate_report.gate.replace("Gate ", "")
        now = datetime.now(UTC)
        client.insert(
            "audit.alpha_gate_log",
            [
                [
                    now,
                    alpha_id,
                    run_id or "",
                    gate_letter,
                    int(gate_report.passed),
                    config_hash or "",
                    json.dumps(gate_report.details, default=str),
                ]
            ],
            column_names=[
                "ts",
                "alpha_id",
                "run_id",
                "gate",
                "passed",
                "config_hash",
                "details",
            ],
        )
    except Exception:
        logger.warning("alpha_audit.log_gate_result failed", alpha_id=alpha_id, exc_info=True)


def log_promotion_result(
    promotion_result: PromotionResult,
    config_hash: str | None,
    scorecard: dict[str, Any] | None = None,
) -> None:
    """Insert one row into audit.alpha_promotion_log. Fails silently on error."""
    if not _is_enabled():
        return
    try:
        client = _get_client()
        now = datetime.now(UTC)
        client.insert(
            "audit.alpha_promotion_log",
            [
                [
                    now,
                    promotion_result.alpha_id,
                    "",
                    int(promotion_result.approved),
                    int(promotion_result.forced),
                    int(promotion_result.gate_d_passed),
                    int(promotion_result.gate_e_passed),
                    float(promotion_result.canary_weight),
                    config_hash or "",
                    json.dumps(promotion_result.reasons, default=str),
                    json.dumps(scorecard or {}, default=str),
                ]
            ],
            column_names=[
                "ts",
                "alpha_id",
                "run_id",
                "approved",
                "forced",
                "gate_d_passed",
                "gate_e_passed",
                "canary_weight",
                "config_hash",
                "reasons",
                "scorecard",
            ],
        )
    except Exception:
        logger.warning(
            "alpha_audit.log_promotion_result failed",
            alpha_id=promotion_result.alpha_id,
            exc_info=True,
        )


def log_canary_action(
    alpha_id: str,
    action: str,
    old_weight: float,
    new_weight: float,
    reason: str,
    checks: dict[str, Any] | None = None,
) -> None:
    """Insert one row into audit.alpha_canary_log. Fails silently on error."""
    if not _is_enabled():
        return
    try:
        client = _get_client()
        now = datetime.now(UTC)
        client.insert(
            "audit.alpha_canary_log",
            [
                [
                    now,
                    alpha_id,
                    action,
                    float(old_weight),
                    float(new_weight),
                    reason,
                    json.dumps(checks or {}, default=str),
                ]
            ],
            column_names=[
                "ts",
                "alpha_id",
                "action",
                "old_weight",
                "new_weight",
                "reason",
                "checks",
            ],
        )
    except Exception:
        logger.warning("alpha_audit.log_canary_action failed", alpha_id=alpha_id, exc_info=True)
