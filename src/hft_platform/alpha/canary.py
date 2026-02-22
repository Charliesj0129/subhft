"""Canary monitor for promoted alpha strategies.

Reads promotion YAML configs and evaluates live metrics against guardrails.
Supports auto-rollback, escalation tiers, and graduation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from structlog import get_logger

logger = get_logger("canary_monitor")

# Escalation tier weights (progressive ramp-up)
_ESCALATION_TIERS: list[float] = [0.02, 0.05, 0.07, 0.10]

# Default config
_DEFAULT_ESCALATION_SESSIONS = 10
_DEFAULT_SHARPE_RATIO = 0.8


@dataclass(frozen=True)
class CanaryStatus:
    alpha_id: str
    current_weight: float
    state: str  # "canary", "escalated", "rolled_back", "graduated"
    reason: str
    checks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "alpha_id": self.alpha_id,
            "current_weight": self.current_weight,
            "state": self.state,
            "reason": self.reason,
            "checks": self.checks,
        }


class CanaryMonitor:
    """Monitor promoted canaries and enforce rollback/escalation rules."""

    def __init__(self, promotions_dir: str = "config/strategy_promotions"):
        self.promotions_dir = Path(promotions_dir)
        self.escalation_sessions = int(os.getenv("HFT_CANARY_ESCALATION_SESSIONS", str(_DEFAULT_ESCALATION_SESSIONS)))
        self.sharpe_ratio = float(os.getenv("HFT_CANARY_SHARPE_RATIO", str(_DEFAULT_SHARPE_RATIO)))

    def load_active_canaries(self) -> list[dict[str, Any]]:
        """Scan promotions dir for enabled canary YAMLs."""
        canaries: list[dict[str, Any]] = []
        if not self.promotions_dir.exists():
            return canaries

        for yaml_path in sorted(self.promotions_dir.rglob("*.yaml")):
            try:
                payload = yaml.safe_load(yaml_path.read_text())
            except Exception:
                logger.warning("canary: failed to parse YAML", path=str(yaml_path))
                continue

            if not isinstance(payload, dict):
                continue
            if not payload.get("enabled", False):
                continue

            payload["_path"] = str(yaml_path)
            canaries.append(payload)

        return canaries

    def evaluate(self, alpha_id: str, live_metrics: dict[str, Any]) -> CanaryStatus:
        """Evaluate live metrics against guardrails from promotion YAML.

        live_metrics keys:
          - slippage_bps: float
          - drawdown_contribution: float
          - execution_error_rate: float
          - sessions_live: int
          - sharpe_live: float (optional, for escalation)
        """
        canary = self._find_canary(alpha_id)
        if canary is None:
            return CanaryStatus(
                alpha_id=alpha_id,
                current_weight=0.0,
                state="not_found",
                reason=f"No active canary config found for {alpha_id}",
                checks={},
            )

        current_weight = float(canary.get("weight", 0.0))
        guardrails = canary.get("guardrails", {})
        rollback = canary.get("rollback", {}).get("trigger", {})
        scorecard = canary.get("scorecard_snapshot", {})

        # Extract thresholds
        max_slippage = float(rollback.get("live_slippage_bps_gt", guardrails.get("max_live_slippage_bps", 3.0)))
        max_dd_contrib = float(
            rollback.get(
                "live_drawdown_contribution_gt",
                guardrails.get("max_live_drawdown_contribution", 0.02),
            )
        )
        max_error_rate = float(
            rollback.get(
                "execution_error_rate_gt",
                guardrails.get("max_execution_error_rate", 0.01),
            )
        )

        # Extract live metrics
        slippage = float(live_metrics.get("slippage_bps", 0.0))
        dd_contrib = float(live_metrics.get("drawdown_contribution", 0.0))
        error_rate = float(live_metrics.get("execution_error_rate", 0.0))
        sessions_live = int(live_metrics.get("sessions_live", 0))
        sharpe_live = live_metrics.get("sharpe_live")

        checks: dict[str, Any] = {
            "slippage_bps": {"value": slippage, "max": max_slippage, "pass": slippage <= max_slippage},
            "drawdown_contribution": {"value": dd_contrib, "max": max_dd_contrib, "pass": dd_contrib <= max_dd_contrib},
            "execution_error_rate": {"value": error_rate, "max": max_error_rate, "pass": error_rate <= max_error_rate},
            "sessions_live": sessions_live,
        }

        # Check rollback triggers
        rollback_reasons: list[str] = []
        if slippage > max_slippage:
            rollback_reasons.append(f"slippage_bps {slippage} > {max_slippage}")
        if dd_contrib > max_dd_contrib:
            rollback_reasons.append(f"drawdown_contribution {dd_contrib} > {max_dd_contrib}")
        if error_rate > max_error_rate:
            rollback_reasons.append(f"execution_error_rate {error_rate} > {max_error_rate}")

        if rollback_reasons:
            return CanaryStatus(
                alpha_id=alpha_id,
                current_weight=current_weight,
                state="rolled_back",
                reason="; ".join(rollback_reasons),
                checks=checks,
            )

        # Check escalation eligibility
        if sessions_live >= self.escalation_sessions:
            sharpe_oos = scorecard.get("sharpe_oos")
            sharpe_threshold = float(sharpe_oos) * self.sharpe_ratio if sharpe_oos is not None else None

            escalation_eligible = (
                sharpe_live is not None and sharpe_threshold is not None and float(sharpe_live) >= sharpe_threshold
            )

            if escalation_eligible:
                checks["sharpe_live"] = float(sharpe_live)  # type: ignore[arg-type]
                checks["sharpe_threshold"] = sharpe_threshold

                # Find next tier
                next_weight = self._next_tier_weight(current_weight)
                if next_weight is None:
                    return CanaryStatus(
                        alpha_id=alpha_id,
                        current_weight=current_weight,
                        state="graduated",
                        reason=f"Weight {current_weight} at or above max tier; graduating",
                        checks=checks,
                    )

                return CanaryStatus(
                    alpha_id=alpha_id,
                    current_weight=current_weight,
                    state="escalated",
                    reason=f"Escalating weight from {current_weight} to {next_weight}",
                    checks=checks,
                )

        # Hold
        return CanaryStatus(
            alpha_id=alpha_id,
            current_weight=current_weight,
            state="canary",
            reason="All checks passed, holding current weight",
            checks=checks,
        )

    def apply_decision(self, status: CanaryStatus) -> None:
        """Execute the decision: modify YAML config and log to audit."""
        canary = self._find_canary(status.alpha_id)
        if canary is None:
            logger.warning("canary.apply: no config found", alpha_id=status.alpha_id)
            return

        yaml_path = Path(canary["_path"])
        old_weight = float(canary.get("weight", 0.0))

        if status.state == "rolled_back":
            canary["weight"] = 0.0
            canary["enabled"] = False
            new_weight = 0.0
        elif status.state == "escalated":
            new_weight = self._next_tier_weight(old_weight) or old_weight
            canary["weight"] = new_weight
        elif status.state == "graduated":
            max_tier = _ESCALATION_TIERS[-1]
            new_weight = max(old_weight, max_tier)
            canary["weight"] = new_weight
            # Remove canary-specific guardrails to indicate graduated
            canary.pop("rollback", None)
        else:
            # hold — no-op
            return

        # Remove internal path key before writing
        path_key = canary.pop("_path", None)
        yaml_path.write_text(yaml.safe_dump(canary, sort_keys=False))
        if path_key:
            canary["_path"] = path_key

        logger.info(
            "canary.apply",
            alpha_id=status.alpha_id,
            action=status.state,
            old_weight=old_weight,
            new_weight=canary["weight"],
        )

        # Best-effort audit log
        try:
            from hft_platform.alpha.audit import log_canary_action

            log_canary_action(
                alpha_id=status.alpha_id,
                action=status.state,
                old_weight=old_weight,
                new_weight=float(canary["weight"]),
                reason=status.reason,
                checks=status.checks,
            )
        except Exception:
            logger.debug("canary.apply: audit log failed", exc_info=True)

    def _find_canary(self, alpha_id: str) -> dict[str, Any] | None:
        for canary in self.load_active_canaries():
            if canary.get("alpha_id") == alpha_id:
                return canary
        return None

    def _next_tier_weight(self, current_weight: float) -> float | None:
        """Return the next escalation tier weight, or None if at/above max."""
        for tier in _ESCALATION_TIERS:
            if tier > current_weight:
                return tier
        return None
