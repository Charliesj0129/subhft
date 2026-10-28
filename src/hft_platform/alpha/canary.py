"""Canary monitor for promoted alpha strategies.

Reads promotion YAML configs and evaluates live metrics against guardrails.
Supports auto-rollback, escalation tiers, and graduation.
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from structlog import get_logger

from hft_platform.alpha import shadow_parity

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


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _unmet_obligations(
    obligations: Any,
    live_metrics: dict[str, Any],
    *,
    alpha_id: str = "",
) -> tuple[list[str], list[dict[str, Any]]]:
    """Deferred gates in a promotion YAML: those still unmet, and those the live metrics certify.

    Fail-closed: an obligations field that is not a list, an entry that is not
    a mapping, and a gate this monitor has no evidence rule for are all unmet.
    A replay_parity obligation (ADR-003) is met only by ``report.json`` files
    from ``hft run --mode replay`` (``live_metrics["replay_parity_reports"]``)
    that ``shadow_parity.judge_reports`` passes; a bare percentage is not evidence.
    """
    if obligations is None:
        return [], []
    if not isinstance(obligations, list):
        return ["obligations: malformed"], []
    unmet: list[str] = []
    certified: list[dict[str, Any]] = []
    for entry in obligations:
        if not isinstance(entry, dict):
            unmet.append("obligation: malformed")
            continue
        gate = str(entry.get("gate", ""))
        if gate == shadow_parity.PARITY_GATE:
            minimum = _as_float(entry.get("min_match_pct", 95.0))
            paths = live_metrics.get("replay_parity_reports")
            if minimum is None or not math.isfinite(minimum):
                unmet.append(f"{gate}: min_match_pct malformed")
            elif paths is None:
                unmet.append(f"{gate}: no replay parity report")
            elif not isinstance(paths, list | tuple) or not all(isinstance(p, str) for p in paths):
                unmet.append(f"{gate}: replay_parity_reports malformed")
            else:
                judged = shadow_parity.judge_reports(
                    paths,
                    Path("."),
                    strategy_id=str(entry.get("strategy_id") or alpha_id),
                    min_match_pct=minimum,
                )
                if judged["pass"]:
                    certified.append(shadow_parity.certified(entry, judged, certified_by="canary_evaluate"))
                else:
                    unmet.append(f"{gate}: {judged['detail']}")
            continue
        unmet.append(f"{gate or '<unnamed>'}: no evidence rule")
    return unmet, certified


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
            except Exception as _exc:  # noqa: BLE001
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
          - replay_parity_reports: list[str] (optional; ``report.json`` paths
            from ``hft run --mode replay``, evidence for a deferred
            replay_parity obligation)
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

            unmet, certified = _unmet_obligations(canary.get("obligations"), live_metrics, alpha_id=alpha_id)
            if escalation_eligible and unmet:
                # A gate deferred at promotion (ADR-003) is still owed: the
                # weight must not rise, and the canary must not graduate,
                # until it carries passing evidence.
                checks["unmet_obligations"] = unmet
                return CanaryStatus(
                    alpha_id=alpha_id,
                    current_weight=current_weight,
                    state="canary",
                    reason="Escalation blocked by unmet obligations: " + "; ".join(unmet),
                    checks=checks,
                )

            if escalation_eligible:
                checks["sharpe_live"] = float(sharpe_live)  # type: ignore[arg-type]
                checks["sharpe_threshold"] = sharpe_threshold
                if certified:
                    # apply_decision moves these out of ``obligations``.
                    checks["certified_obligations"] = certified

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

        # Build a new dict — never mutate the original (immutability rule)
        updated = {**canary}

        if status.state == "rolled_back":
            updated["weight"] = 0.0
            updated["enabled"] = False
            new_weight = 0.0
        elif status.state == "escalated":
            new_weight = self._next_tier_weight(old_weight) or old_weight
            updated["weight"] = new_weight
        elif status.state == "graduated":
            max_tier = _ESCALATION_TIERS[-1]
            new_weight = max(old_weight, max_tier)
            updated["weight"] = new_weight
            # Remove canary-specific guardrails to indicate graduated
            updated.pop("rollback", None)
        else:
            # hold — no-op
            return

        newly_certified = status.checks.get("certified_obligations")
        if isinstance(newly_certified, list) and newly_certified:
            # The obligations this decision's evidence certified are no longer
            # owed; keep the record of what certified them (ADR-003).
            done = {(c.get("gate"), c.get("stage")) for c in newly_certified}
            owed = canary.get("obligations")
            updated["obligations"] = [
                o
                for o in (owed if isinstance(owed, list) else [])
                if not (isinstance(o, dict) and (o.get("gate"), o.get("stage")) in done)
            ]
            prior = canary.get("certified_obligations")
            updated["certified_obligations"] = [*(prior if isinstance(prior, list) else []), *newly_certified]

        # Exclude internal path key from the persisted YAML
        updated_for_write = {k: v for k, v in updated.items() if k != "_path"}

        # Atomic write: write to temp file then rename (POSIX-atomic).
        # M2 (2026-04-25): switch to ``finally`` so orphan tmpfiles are removed
        # on SIGKILL / interpreter-shutdown paths that bypass exception
        # handlers.
        tmp_fd, tmp_path = tempfile.mkstemp(dir=yaml_path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(yaml.safe_dump(updated_for_write, sort_keys=False))
            Path(tmp_path).replace(yaml_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        logger.info(
            "canary.apply",
            alpha_id=status.alpha_id,
            action=status.state,
            old_weight=old_weight,
            new_weight=new_weight,
        )

        # Best-effort audit log
        try:
            from hft_platform.alpha.audit import log_canary_action

            log_canary_action(
                alpha_id=status.alpha_id,
                action=status.state,
                old_weight=old_weight,
                new_weight=float(new_weight),
                reason=status.reason,
                checks=status.checks,
            )
        except Exception as _exc:  # noqa: BLE001
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
