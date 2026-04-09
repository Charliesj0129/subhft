"""Best-effort ClickHouse audit logging for alpha pipeline events.

Controlled by environment variables:
- HFT_ALPHA_AUDIT_ENABLED=0|1  (default 0, opt-in)
- HFT_CLICKHOUSE_HOST           (default localhost)
- HFT_CLICKHOUSE_PORT           (default 8123)
- HFT_ALPHA_AUDIT_FALLBACK_DIR  (default research/experiments/.audit_fallback)
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from structlog import get_logger

from hft_platform.core import timebase


def _now_utc() -> _dt.datetime:
    """UTC-aware datetime via timebase (no direct datetime.now)."""  # noqa: E501
    return _dt.datetime.fromtimestamp(timebase.now_s(), tz=_dt.timezone.utc)


def _now_ns() -> int:
    """Current time as nanosecond epoch integer (matches ClickHouse Int64 ts columns)."""
    return timebase.now_ns()


if TYPE_CHECKING:
    from hft_platform.alpha.promotion import PromotionResult
    from hft_platform.alpha.validation import GateReport

logger = get_logger("alpha_audit")

_FALLBACK_DIR: Path = Path(os.getenv("HFT_ALPHA_AUDIT_FALLBACK_DIR", "research/experiments/.audit_fallback"))


def _write_fallback(table: str, row: dict[str, Any]) -> None:
    """Append one JSON line to the local fallback file for the given table.

    This function must not raise — any error is logged at ERROR level and swallowed.
    """
    try:
        fallback_dir = _FALLBACK_DIR
        fallback_dir.mkdir(parents=True, exist_ok=True)
        payload = {**row, "_failed_at": _now_utc().isoformat()}
        fallback_file = fallback_dir / f"{table}.jsonl"
        with fallback_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
    except Exception:  # noqa: BLE001
        logger.error("alpha_audit._write_fallback failed", table=table, exc_info=True)


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
    try:
        from hft_platform.observability.metrics import get_metrics

        m = get_metrics()
        if m is not None:
            gate_letter = gate_report.gate.replace("Gate ", "")
            result = "pass" if gate_report.passed else "fail"
            m.alpha_gate_results_total.labels(
                alpha_id=alpha_id,
                gate=gate_letter,
                result=result,
            ).inc()
    except Exception:  # noqa: BLE001
        pass  # metrics are best-effort

    if not _is_enabled():
        return
    try:
        client = _get_client()
        gate_letter = gate_report.gate.replace("Gate ", "")
        now = _now_ns()
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
    except Exception as _exc:  # noqa: BLE001
        logger.warning("alpha_audit.log_gate_result failed", alpha_id=alpha_id, exc_info=True)
        gate_letter = gate_report.gate.replace("Gate ", "")
        _write_fallback(
            "alpha_gate_log",
            {
                "ts": _now_utc().isoformat(),
                "alpha_id": alpha_id,
                "run_id": run_id or "",
                "gate": gate_letter,
                "passed": int(gate_report.passed),
                "config_hash": config_hash or "",
                "details": json.dumps(gate_report.details, default=str),
            },
        )


def log_promotion_result(
    promotion_result: PromotionResult,
    config_hash: str | None,
    scorecard: dict[str, Any] | None = None,
) -> None:
    """Insert one row into audit.alpha_promotion_log. Fails silently on error."""
    try:
        from hft_platform.observability.metrics import get_metrics

        m = get_metrics()
        if m is not None:
            if promotion_result.forced:
                result = "forced"
            elif promotion_result.approved:
                result = "approved"
            else:
                result = "rejected"
            m.alpha_promotion_results_total.labels(
                alpha_id=promotion_result.alpha_id,
                result=result,
            ).inc()
    except Exception:  # noqa: BLE001
        pass  # metrics are best-effort

    if not _is_enabled():
        return
    try:
        client = _get_client()
        now = _now_ns()
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
    except Exception as _exc:  # noqa: BLE001
        logger.warning(
            "alpha_audit.log_promotion_result failed",
            alpha_id=promotion_result.alpha_id,
            exc_info=True,
        )
        _write_fallback(
            "alpha_promotion_log",
            {
                "ts": _now_utc().isoformat(),
                "alpha_id": promotion_result.alpha_id,
                "run_id": "",
                "approved": int(promotion_result.approved),
                "forced": int(promotion_result.forced),
                "gate_d_passed": int(promotion_result.gate_d_passed),
                "gate_e_passed": int(promotion_result.gate_e_passed),
                "canary_weight": float(promotion_result.canary_weight),
                "config_hash": config_hash or "",
                "reasons": json.dumps(promotion_result.reasons, default=str),
                "scorecard": json.dumps(scorecard or {}, default=str),
            },
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
    try:
        from hft_platform.observability.metrics import get_metrics

        m = get_metrics()
        if m is not None:
            m.alpha_canary_actions_total.labels(
                alpha_id=alpha_id,
                action=action,
            ).inc()
    except Exception:  # noqa: BLE001
        pass  # metrics are best-effort

    if not _is_enabled():
        return
    try:
        client = _get_client()
        now = _now_ns()
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
    except Exception as _exc:  # noqa: BLE001
        logger.warning("alpha_audit.log_canary_action failed", alpha_id=alpha_id, exc_info=True)
        _write_fallback(
            "alpha_canary_log",
            {
                "ts": _now_utc().isoformat(),
                "alpha_id": alpha_id,
                "action": action,
                "old_weight": float(old_weight),
                "new_weight": float(new_weight),
                "reason": reason,
                "checks": json.dumps(checks or {}, default=str),
            },
        )
