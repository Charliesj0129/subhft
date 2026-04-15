"""AlertRouter — core routing pipeline for tiered alert delivery."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from hft_platform.notifications.aggregator import AlertAggregator
from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule
from hft_platform.notifications.escalation import EscalationTracker

if TYPE_CHECKING:
    from hft_platform.notifications.telegram import TelegramSender
    from hft_platform.notifications.webhook import WebhookSender

logger = structlog.get_logger(__name__)

_DEFAULT_SILENCE_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "base" / "alert_silence.yaml"
)


class AlertRouter:
    """Routes alerts through aggregation, silence, severity-based delivery, and escalation.

    Pipeline: emit() -> aggregate -> silence check -> route by severity -> escalation track
    """

    __slots__ = (
        "_telegram",
        "_webhook",
        "_aggregator",
        "_escalation",
        "_silence_rules",
        "_info_batch",
    )

    def __init__(
        self,
        telegram_sender: TelegramSender,
        webhook_sender: WebhookSender | None = None,
        aggregation_window_ns: int = 300_000_000_000,
        escalation_intervals_ns: list[int] | None = None,
        max_escalations: int = 3,
        silence_config_path: Path | None = None,
    ) -> None:
        self._telegram = telegram_sender
        self._webhook = webhook_sender
        self._aggregator = AlertAggregator(window_ns=aggregation_window_ns)
        self._escalation = EscalationTracker(
            intervals_ns=escalation_intervals_ns or [300_000_000_000, 900_000_000_000],
            max_escalations=max_escalations,
        )
        self._silence_rules: dict[str, SilenceRule] = {}
        self._info_batch: list[Alert] = []
        self._load_silence_rules(silence_config_path or _DEFAULT_SILENCE_PATH)

    def _load_silence_rules(self, path: Path) -> None:
        """Load silence rules from YAML config. Silently skips missing files."""
        if not path.exists():
            return
        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            for entry in raw.get("rules", []):
                sev_str = entry.get("severity_max", "WARN")
                rule = SilenceRule(
                    rule_id=entry["rule_id"],
                    category=entry.get("category"),
                    source=entry.get("source"),
                    severity_max=AlertSeverity[sev_str.upper()],
                    start_ns=entry.get("start_ns", 0),
                    end_ns=entry.get("end_ns", 0),
                    reason=entry.get("reason", ""),
                )
                self._silence_rules[rule.rule_id] = rule
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert_router.silence_config_load_failed", error=str(exc))

    def add_silence(self, rule: SilenceRule) -> None:
        """Add or replace a silence rule at runtime."""
        self._silence_rules[rule.rule_id] = rule
        logger.info("alert_router.silence_added", rule_id=rule.rule_id, reason=rule.reason)

    def remove_silence(self, rule_id: str) -> bool:
        """Remove a silence rule by ID. Returns True if it existed."""
        removed = self._silence_rules.pop(rule_id, None)
        if removed is not None:
            logger.info("alert_router.silence_removed", rule_id=rule_id)
        return removed is not None

    def _is_silenced(self, alert: Alert) -> bool:
        """Return True if any active silence rule matches the alert."""
        return any(rule.matches(alert) for rule in self._silence_rules.values())

    async def emit(self, alert: Alert) -> None:
        """Process and route an alert through the full pipeline.

        Steps:
          1. Aggregation — dedup within time window.
          2. Silence check — suppress matching alerts.
          3. Route by severity: INFO → batch, WARN → Telegram, CRITICAL/FATAL → Telegram + Webhook + escalation.
        """
        # Step 1: aggregation / dedup
        passed = self._aggregator.process(alert)
        if passed is None:
            return

        # Step 2: silence check
        if self._is_silenced(alert):
            logger.debug(
                "alert_router.silenced",
                alert_id=alert.alert_id,
                category=alert.category,
            )
            return

        # Step 3: route by severity
        if alert.severity == AlertSeverity.INFO:
            self._info_batch.append(alert)
            return

        if alert.severity == AlertSeverity.WARN:
            await self._send_telegram(alert)
        elif alert.severity >= AlertSeverity.CRITICAL:
            await self._send_critical(alert)
            self._escalation.track(alert)

    async def _send_telegram(self, alert: Alert) -> None:
        """Send a non-critical alert to Telegram."""
        msg = self._format_alert(alert)
        await self._telegram.send(msg, critical=False)

    async def _send_critical(self, alert: Alert) -> None:
        """Fan-out a critical alert to Telegram (with retry) and optional webhook in parallel."""
        msg = self._format_alert(alert)
        coros: list[Any] = [self._telegram.send(msg, critical=True)]
        if self._webhook is not None:
            coros.append(self._webhook.send(msg))
        await asyncio.gather(*coros, return_exceptions=True)

    @staticmethod
    def _format_alert(alert: Alert) -> str:
        """Format an alert into a human-readable notification string."""
        severity_icons = {
            AlertSeverity.INFO: "ℹ️",
            AlertSeverity.WARN: "⚠️",
            AlertSeverity.CRITICAL: "🔴",
            AlertSeverity.FATAL: "🚨",
        }
        icon = severity_icons.get(alert.severity, "❓")
        lines = [
            f"{icon} [{alert.severity.name}] {alert.title}",
            f"Source: {alert.source} | Category: {alert.category}",
            alert.detail,
        ]
        return "\n".join(lines)

    async def flush_info_batch(self) -> None:
        """Send a batched summary of accumulated INFO alerts, then clear the batch."""
        if not self._info_batch:
            return
        count = len(self._info_batch)
        titles = [a.title for a in self._info_batch[:10]]
        summary = f"ℹ️ {count} INFO alerts:\n" + "\n".join(f"  • {t}" for t in titles)
        if count > 10:
            summary += f"\n  ... and {count - 10} more"
        self._info_batch.clear()
        await self._telegram.send(summary, critical=False)

    def acknowledge(self, alert_id: str) -> bool:
        """Acknowledge an escalating alert by ID. Returns True if it was being tracked."""
        was_tracked = self._escalation.is_tracked(alert_id)
        self._escalation.acknowledge(alert_id)
        return was_tracked

    def active_alerts(self) -> list[Alert]:
        """Return all currently escalating (unacknowledged) alerts."""
        return [e.alert for e in self._escalation._entries.values()]

    async def tick(self, now_ns: int) -> None:
        """Periodic maintenance: flush expired aggregation windows, fire due escalations, flush INFO batch.

        Should be called by a scheduler (e.g. every 30–60 seconds).
        """
        # Flush expired aggregation windows and send suppression summaries
        summaries = self._aggregator.flush_expired(now_ns)
        for s in summaries:
            msg = (
                f"⚠️ {s.first_alert.title} — repeated {s.suppressed_count} times "
                f"in past 5 minutes"
            )
            await self._telegram.send(msg, critical=False)

        # Re-send due escalations
        due = self._escalation.get_due(now_ns)
        for alert in due:
            logger.warning(
                "alert_router.escalation",
                alert_id=alert.alert_id,
                title=alert.title,
            )
            await self._send_critical(alert)
            self._escalation.mark_escalated(alert.alert_id, now_ns)

        # Flush INFO batch
        await self.flush_info_batch()
