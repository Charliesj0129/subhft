# Track C: Autonomy Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the autonomy foundation — alert tiered routing (C1), operations state machine (C2), and self-healing framework (C3) — enabling 24/7 unattended operation.

**Architecture:** Layered Event Bus pattern. All alerts flow through `AlertRouter` (severity/aggregation/silence/escalation). `OperationsStateMachine` orchestrates daily lifecycle above `SessionGovernor`. `HealingOrchestrator` consumes `FaultEvent` objects and executes YAML-driven repair playbooks. Feature flags gate each module independently.

**Tech Stack:** Python 3.12, asyncio, structlog, pydantic-free (dataclasses + `__slots__`), YAML configs, pytest + AsyncMock, aiohttp (Telegram), Prometheus metrics via `observability/metrics.py`.

**Spec:** `docs/superpowers/specs/2026-04-15-track-c-autonomy-foundation-design.md`

---

## File Map

### C1: Alert Tiered Router

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/hft_platform/notifications/alert.py` | `Alert`, `AlertSeverity`, `SilenceRule` data models |
| Create | `src/hft_platform/notifications/aggregator.py` | Dedup + time-window aggregation |
| Create | `src/hft_platform/notifications/escalation.py` | Escalation chain (resend unack'd alerts) |
| Create | `src/hft_platform/notifications/alert_router.py` | Core routing pipeline: aggregate → silence → route → escalate |
| Create | `config/base/alert_silence.yaml` | Default silence rules (empty list) |
| Modify | `src/hft_platform/notifications/dispatcher.py` | Rewire `notify_*` methods to emit `Alert` via `AlertRouter` |
| Modify | `src/hft_platform/notifications/__init__.py` | Re-export `AlertRouter`, `Alert`, `AlertSeverity` |
| Create | `tests/unit/test_alert_models.py` | Alert, SilenceRule model tests |
| Create | `tests/unit/test_alert_aggregator.py` | Aggregation window + dedup tests |
| Create | `tests/unit/test_alert_escalation.py` | Escalation chain tests |
| Create | `tests/unit/test_alert_router.py` | Full routing pipeline tests |
| Create | `tests/unit/test_dispatcher_alert_integration.py` | Dispatcher rewire backward compat tests |

### C2: Operations State Machine

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/hft_platform/ops/preflight_checker.py` | `PreflightChecker`, `PreflightCheck`, `CheckResult` |
| Create | `src/hft_platform/ops/contract_lifecycle.py` | `ContractLifecycleManager` — futures alias + option chain |
| Create | `src/hft_platform/ops/ops_state_machine.py` | `OpsState`, `OperationsStateMachine` |
| Create | `config/base/ops_state_machine.yaml` | Ops SM config (timings, cron, preflight) |
| Modify | `src/hft_platform/ops/session_governor.py` | Add `on_phase_change` hook for OpsStateMachine |
| Create | `tests/unit/test_preflight_checker.py` | Preflight check pass/fail/warn/timeout tests |
| Create | `tests/unit/test_contract_lifecycle.py` | Futures refresh + option chain tests |
| Create | `tests/unit/test_ops_state_machine.py` | State transitions + lifecycle tests |

### C3: Self-Healing Framework

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/hft_platform/healing/__init__.py` | Package exports |
| Create | `src/hft_platform/healing/fault.py` | `FaultEvent`, `FaultCategory`, `FaultSeverity`, `RiskLevel` |
| Create | `src/hft_platform/healing/playbook.py` | `HealingPlaybook` — YAML-driven repair step lookup |
| Create | `src/hft_platform/healing/actions.py` | `ActionRegistry` + concrete repair action callables |
| Create | `src/hft_platform/healing/orchestrator.py` | `HealingOrchestrator` — classify → lookup → execute → verify |
| Create | `config/base/healing_playbook.yaml` | Fault-repair mapping (13 playbooks) |
| Modify | `src/hft_platform/ops/autonomy_monitor.py` | Emit `FaultEvent` instead of direct actions (behind flag) |
| Create | `tests/unit/test_fault_models.py` | FaultEvent model tests |
| Create | `tests/unit/test_healing_playbook.py` | Playbook YAML loading + matching tests |
| Create | `tests/unit/test_healing_actions.py` | Action registry + individual action tests |
| Create | `tests/unit/test_healing_orchestrator.py` | Full orchestration flow tests |
| Create | `tests/unit/test_autonomy_monitor_fault_emit.py` | AutonomyMonitor FaultEvent emission tests |

---

## Module C1: Alert Tiered Router

### Task 1: Alert Data Models

**Files:**
- Create: `src/hft_platform/notifications/alert.py`
- Create: `tests/unit/test_alert_models.py`

- [ ] **Step 1: Write failing tests for Alert and SilenceRule models**

```python
# tests/unit/test_alert_models.py
"""Tests for alert data models."""
from __future__ import annotations

import pytest


def test_alert_severity_ordering():
    from hft_platform.notifications.alert import AlertSeverity

    assert AlertSeverity.INFO < AlertSeverity.WARN
    assert AlertSeverity.WARN < AlertSeverity.CRITICAL
    assert AlertSeverity.CRITICAL < AlertSeverity.FATAL


def test_alert_creation():
    from hft_platform.notifications.alert import Alert, AlertSeverity

    alert = Alert(
        alert_id="test-001",
        severity=AlertSeverity.WARN,
        category="feed",
        source="shioaji_client",
        title="Feed gap detected",
        detail="No ticks for 2.5 seconds on TMFD6",
        ts_ns=1_700_000_000_000_000_000,
        dedup_key="feed_gap:TMFD6",
        metadata={"symbol": "TMFD6", "gap_s": 2.5},
    )
    assert alert.severity == AlertSeverity.WARN
    assert alert.category == "feed"
    assert alert.dedup_key == "feed_gap:TMFD6"


def test_alert_is_frozen():
    from hft_platform.notifications.alert import Alert, AlertSeverity

    alert = Alert(
        alert_id="test-002",
        severity=AlertSeverity.INFO,
        category="ops",
        source="session_governor",
        title="Phase change",
        detail="futures_day: INIT -> OPEN",
        ts_ns=1_700_000_000_000_000_000,
        dedup_key=None,
        metadata=None,
    )
    with pytest.raises(AttributeError):
        alert.severity = AlertSeverity.FATAL  # type: ignore[misc]


def test_silence_rule_matches_category():
    from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule

    rule = SilenceRule(
        rule_id="s-001",
        category="feed",
        source=None,
        severity_max=AlertSeverity.WARN,
        start_ns=1_000_000_000_000_000_000,
        end_ns=2_000_000_000_000_000_000,
        reason="maintenance",
    )
    alert = Alert(
        alert_id="a-001",
        severity=AlertSeverity.WARN,
        category="feed",
        source="shioaji_client",
        title="Test",
        detail="Test",
        ts_ns=1_500_000_000_000_000_000,
        dedup_key=None,
        metadata=None,
    )
    assert rule.matches(alert)


def test_silence_rule_does_not_match_higher_severity():
    from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule

    rule = SilenceRule(
        rule_id="s-002",
        category="feed",
        source=None,
        severity_max=AlertSeverity.WARN,
        start_ns=1_000_000_000_000_000_000,
        end_ns=2_000_000_000_000_000_000,
        reason="maintenance",
    )
    alert = Alert(
        alert_id="a-002",
        severity=AlertSeverity.CRITICAL,
        category="feed",
        source="shioaji_client",
        title="Test",
        detail="Test",
        ts_ns=1_500_000_000_000_000_000,
        dedup_key=None,
        metadata=None,
    )
    assert not rule.matches(alert)


def test_silence_rule_does_not_match_outside_window():
    from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule

    rule = SilenceRule(
        rule_id="s-003",
        category="feed",
        source=None,
        severity_max=AlertSeverity.WARN,
        start_ns=1_000_000_000_000_000_000,
        end_ns=2_000_000_000_000_000_000,
        reason="maintenance",
    )
    alert = Alert(
        alert_id="a-003",
        severity=AlertSeverity.INFO,
        category="feed",
        source="shioaji_client",
        title="Test",
        detail="Test",
        ts_ns=3_000_000_000_000_000_000,  # after window
        dedup_key=None,
        metadata=None,
    )
    assert not rule.matches(alert)


def test_silence_rule_permanent_window():
    from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule

    rule = SilenceRule(
        rule_id="s-004",
        category=None,  # match all categories
        source=None,
        severity_max=AlertSeverity.INFO,
        start_ns=1_000_000_000_000_000_000,
        end_ns=0,  # permanent
        reason="suppress info noise",
    )
    alert = Alert(
        alert_id="a-004",
        severity=AlertSeverity.INFO,
        category="broker",
        source="fubon",
        title="Test",
        detail="Test",
        ts_ns=9_000_000_000_000_000_000,  # far future
        dedup_key=None,
        metadata=None,
    )
    assert rule.matches(alert)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_alert_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hft_platform.notifications.alert'`

- [ ] **Step 3: Implement alert data models**

```python
# src/hft_platform/notifications/alert.py
"""Alert data models for the tiered notification routing system."""
from __future__ import annotations

import enum
from dataclasses import dataclass


class AlertSeverity(enum.IntEnum):
    """Alert severity levels for routing decisions."""

    INFO = 0
    WARN = 1
    CRITICAL = 2
    FATAL = 3


@dataclass(slots=True, frozen=True)
class Alert:
    """Immutable alert message for the notification pipeline."""

    alert_id: str
    severity: AlertSeverity
    category: str
    source: str
    title: str
    detail: str
    ts_ns: int
    dedup_key: str | None
    metadata: dict | None


@dataclass(slots=True)
class SilenceRule:
    """Rule to suppress alerts matching specific criteria within a time window."""

    rule_id: str
    category: str | None
    source: str | None
    severity_max: AlertSeverity
    start_ns: int
    end_ns: int
    reason: str

    def matches(self, alert: Alert) -> bool:
        """Return True if this rule silences the given alert."""
        if self.category is not None and alert.category != self.category:
            return False
        if self.source is not None and alert.source != self.source:
            return False
        if alert.severity > self.severity_max:
            return False
        if alert.ts_ns < self.start_ns:
            return False
        if self.end_ns != 0 and alert.ts_ns > self.end_ns:
            return False
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_alert_models.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/notifications/alert.py tests/unit/test_alert_models.py
git commit -m "feat(notifications): add Alert and SilenceRule data models for tiered routing"
```

---

### Task 2: Alert Aggregator

**Files:**
- Create: `src/hft_platform/notifications/aggregator.py`
- Create: `tests/unit/test_alert_aggregator.py`

- [ ] **Step 1: Write failing tests for aggregation logic**

```python
# tests/unit/test_alert_aggregator.py
"""Tests for alert dedup and time-window aggregation."""
from __future__ import annotations

import pytest

from hft_platform.notifications.alert import Alert, AlertSeverity


def _make_alert(
    *,
    dedup_key: str | None = "test_key",
    severity: AlertSeverity = AlertSeverity.WARN,
    ts_ns: int = 1_000_000_000_000_000_000,
    alert_id: str = "a-001",
) -> Alert:
    return Alert(
        alert_id=alert_id,
        severity=severity,
        category="feed",
        source="test",
        title="Test alert",
        detail="Details here",
        ts_ns=ts_ns,
        dedup_key=dedup_key,
        metadata=None,
    )


class TestAlertAggregator:
    def test_first_alert_passes_through(self):
        from hft_platform.notifications.aggregator import AlertAggregator

        agg = AlertAggregator(window_ns=300_000_000_000)
        alert = _make_alert()
        result = agg.process(alert)
        assert result is not None
        assert result.alert_id == "a-001"

    def test_duplicate_within_window_is_suppressed(self):
        from hft_platform.notifications.aggregator import AlertAggregator

        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(ts_ns=1_000_000_000_000_000_000)
        a2 = _make_alert(ts_ns=1_001_000_000_000_000_000, alert_id="a-002")
        assert agg.process(a1) is not None
        assert agg.process(a2) is None  # suppressed

    def test_alert_after_window_passes_through(self):
        from hft_platform.notifications.aggregator import AlertAggregator

        agg = AlertAggregator(window_ns=300_000_000_000)  # 300s
        a1 = _make_alert(ts_ns=1_000_000_000_000_000_000)
        a2 = _make_alert(ts_ns=1_301_000_000_000_000_000, alert_id="a-002")  # 301s later
        assert agg.process(a1) is not None
        assert agg.process(a2) is not None

    def test_none_dedup_key_always_passes(self):
        from hft_platform.notifications.aggregator import AlertAggregator

        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(dedup_key=None, ts_ns=1_000_000_000_000_000_000)
        a2 = _make_alert(dedup_key=None, ts_ns=1_000_000_000_000_000_001, alert_id="a-002")
        assert agg.process(a1) is not None
        assert agg.process(a2) is not None

    def test_fatal_never_aggregated(self):
        from hft_platform.notifications.aggregator import AlertAggregator

        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(severity=AlertSeverity.FATAL, ts_ns=1_000_000_000_000_000_000)
        a2 = _make_alert(severity=AlertSeverity.FATAL, ts_ns=1_000_000_000_000_000_001, alert_id="a-002")
        assert agg.process(a1) is not None
        assert agg.process(a2) is not None

    def test_flush_pending_returns_summary(self):
        from hft_platform.notifications.aggregator import AlertAggregator

        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(ts_ns=1_000_000_000_000_000_000)
        a2 = _make_alert(ts_ns=1_001_000_000_000_000_000, alert_id="a-002")
        a3 = _make_alert(ts_ns=1_002_000_000_000_000_000, alert_id="a-003")
        agg.process(a1)
        agg.process(a2)
        agg.process(a3)

        summaries = agg.flush_expired(now_ns=1_301_000_000_000_000_000)
        assert len(summaries) == 1
        assert summaries[0].suppressed_count == 2

    def test_different_dedup_keys_independent(self):
        from hft_platform.notifications.aggregator import AlertAggregator

        agg = AlertAggregator(window_ns=300_000_000_000)
        a1 = _make_alert(dedup_key="key_a", ts_ns=1_000_000_000_000_000_000)
        a2 = _make_alert(dedup_key="key_b", ts_ns=1_000_000_000_000_000_001, alert_id="a-002")
        assert agg.process(a1) is not None
        assert agg.process(a2) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_alert_aggregator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hft_platform.notifications.aggregator'`

- [ ] **Step 3: Implement the aggregator**

```python
# src/hft_platform/notifications/aggregator.py
"""Alert deduplication and time-window aggregation."""
from __future__ import annotations

from dataclasses import dataclass, field

from hft_platform.notifications.alert import Alert, AlertSeverity


@dataclass(slots=True)
class AggregationSummary:
    """Summary emitted when an aggregation window expires with suppressed alerts."""

    dedup_key: str
    first_alert: Alert
    suppressed_count: int
    window_start_ns: int
    window_end_ns: int


@dataclass(slots=True)
class _AggWindow:
    """Tracks a single dedup_key's aggregation window."""

    first_alert: Alert
    window_start_ns: int
    window_end_ns: int
    count: int = 1


class AlertAggregator:
    """Deduplicates alerts by dedup_key within a configurable time window.

    - First alert per key passes through immediately.
    - Subsequent alerts with the same key within the window are suppressed.
    - FATAL alerts are never aggregated.
    - Alerts with dedup_key=None always pass through.
    """

    __slots__ = ("_window_ns", "_windows")

    def __init__(self, window_ns: int = 300_000_000_000) -> None:
        self._window_ns = window_ns
        self._windows: dict[str, _AggWindow] = {}

    def process(self, alert: Alert) -> Alert | None:
        """Process an incoming alert. Returns the alert if it should be sent, None if suppressed."""
        if alert.dedup_key is None:
            return alert
        if alert.severity == AlertSeverity.FATAL:
            return alert

        key = alert.dedup_key
        window = self._windows.get(key)

        if window is None or alert.ts_ns > window.window_end_ns:
            self._windows[key] = _AggWindow(
                first_alert=alert,
                window_start_ns=alert.ts_ns,
                window_end_ns=alert.ts_ns + self._window_ns,
            )
            return alert

        window.count += 1
        return None

    def flush_expired(self, now_ns: int) -> list[AggregationSummary]:
        """Flush windows that have expired, returning summaries for those with suppressed alerts."""
        expired: list[AggregationSummary] = []
        to_remove: list[str] = []

        for key, window in self._windows.items():
            if now_ns > window.window_end_ns and window.count > 1:
                expired.append(
                    AggregationSummary(
                        dedup_key=key,
                        first_alert=window.first_alert,
                        suppressed_count=window.count - 1,
                        window_start_ns=window.window_start_ns,
                        window_end_ns=window.window_end_ns,
                    )
                )
            if now_ns > window.window_end_ns:
                to_remove.append(key)

        for key in to_remove:
            del self._windows[key]

        return expired
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_alert_aggregator.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/notifications/aggregator.py tests/unit/test_alert_aggregator.py
git commit -m "feat(notifications): add AlertAggregator with dedup and time-window suppression"
```

---

### Task 3: Escalation Chain

**Files:**
- Create: `src/hft_platform/notifications/escalation.py`
- Create: `tests/unit/test_alert_escalation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_alert_escalation.py
"""Tests for alert escalation chain."""
from __future__ import annotations

import pytest

from hft_platform.notifications.alert import Alert, AlertSeverity


def _make_alert(
    *,
    alert_id: str = "a-001",
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    ts_ns: int = 1_000_000_000_000_000_000,
) -> Alert:
    return Alert(
        alert_id=alert_id,
        severity=severity,
        category="risk",
        source="storm_guard",
        title="Test alert",
        detail="Test detail",
        ts_ns=ts_ns,
        dedup_key=None,
        metadata=None,
    )


class TestEscalationTracker:
    def test_track_new_alert(self):
        from hft_platform.notifications.escalation import EscalationTracker

        tracker = EscalationTracker(
            intervals_ns=[300_000_000_000, 900_000_000_000],
            max_escalations=3,
        )
        alert = _make_alert()
        tracker.track(alert)
        assert tracker.is_tracked("a-001")

    def test_acknowledge_stops_tracking(self):
        from hft_platform.notifications.escalation import EscalationTracker

        tracker = EscalationTracker(intervals_ns=[300_000_000_000], max_escalations=2)
        tracker.track(_make_alert())
        assert tracker.is_tracked("a-001")
        tracker.acknowledge("a-001")
        assert not tracker.is_tracked("a-001")

    def test_due_escalations_returns_alerts_after_interval(self):
        from hft_platform.notifications.escalation import EscalationTracker

        tracker = EscalationTracker(
            intervals_ns=[300_000_000_000],  # 5 min
            max_escalations=3,
        )
        alert = _make_alert(ts_ns=1_000_000_000_000_000_000)
        tracker.track(alert)

        # Before interval: nothing due
        due = tracker.get_due(now_ns=1_100_000_000_000_000_000)  # 100s later
        assert len(due) == 0

        # After interval: escalation due
        due = tracker.get_due(now_ns=1_301_000_000_000_000_000)  # 301s later
        assert len(due) == 1
        assert due[0].alert_id == "a-001"

    def test_max_escalations_reached(self):
        from hft_platform.notifications.escalation import EscalationTracker

        tracker = EscalationTracker(
            intervals_ns=[100_000_000_000],  # 100s
            max_escalations=2,
        )
        alert = _make_alert(ts_ns=1_000_000_000_000_000_000)
        tracker.track(alert)

        # First escalation
        due = tracker.get_due(now_ns=1_101_000_000_000_000_000)
        assert len(due) == 1
        tracker.mark_escalated("a-001", now_ns=1_101_000_000_000_000_000)

        # Second escalation
        due = tracker.get_due(now_ns=1_202_000_000_000_000_000)
        assert len(due) == 1
        tracker.mark_escalated("a-001", now_ns=1_202_000_000_000_000_000)

        # Third: max reached, no more escalations
        due = tracker.get_due(now_ns=1_303_000_000_000_000_000)
        assert len(due) == 0

    def test_info_alerts_not_tracked(self):
        from hft_platform.notifications.escalation import EscalationTracker

        tracker = EscalationTracker(intervals_ns=[300_000_000_000], max_escalations=3)
        alert = _make_alert(severity=AlertSeverity.INFO)
        tracker.track(alert)
        assert not tracker.is_tracked("a-001")

    def test_warn_alerts_not_tracked(self):
        from hft_platform.notifications.escalation import EscalationTracker

        tracker = EscalationTracker(intervals_ns=[300_000_000_000], max_escalations=3)
        alert = _make_alert(severity=AlertSeverity.WARN)
        tracker.track(alert)
        assert not tracker.is_tracked("a-001")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_alert_escalation.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement escalation tracker**

```python
# src/hft_platform/notifications/escalation.py
"""Escalation chain for unacknowledged CRITICAL/FATAL alerts."""
from __future__ import annotations

from dataclasses import dataclass, field

from hft_platform.notifications.alert import Alert, AlertSeverity


@dataclass(slots=True)
class _EscalationEntry:
    """Tracks escalation state for a single alert."""

    alert: Alert
    tracked_ns: int
    escalation_count: int = 0
    last_escalated_ns: int = 0


class EscalationTracker:
    """Tracks CRITICAL/FATAL alerts and generates escalation events.

    Only CRITICAL and FATAL alerts are tracked. INFO/WARN are ignored.
    Escalation intervals define how long to wait before each resend.
    After max_escalations, the alert is no longer escalated (but stays tracked
    until acknowledged).
    """

    __slots__ = ("_intervals_ns", "_max_escalations", "_entries")

    def __init__(
        self,
        intervals_ns: list[int] | None = None,
        max_escalations: int = 3,
    ) -> None:
        self._intervals_ns = intervals_ns or [300_000_000_000, 900_000_000_000]
        self._max_escalations = max_escalations
        self._entries: dict[str, _EscalationEntry] = {}

    def track(self, alert: Alert) -> None:
        """Start tracking an alert for escalation. Only CRITICAL/FATAL are tracked."""
        if alert.severity < AlertSeverity.CRITICAL:
            return
        self._entries[alert.alert_id] = _EscalationEntry(
            alert=alert,
            tracked_ns=alert.ts_ns,
        )

    def acknowledge(self, alert_id: str) -> None:
        """Acknowledge an alert, stopping its escalation chain."""
        self._entries.pop(alert_id, None)

    def is_tracked(self, alert_id: str) -> bool:
        """Check if an alert is being tracked."""
        return alert_id in self._entries

    def get_due(self, now_ns: int) -> list[Alert]:
        """Return alerts whose next escalation is due."""
        due: list[Alert] = []
        for entry in self._entries.values():
            if entry.escalation_count >= self._max_escalations:
                continue
            interval_idx = min(entry.escalation_count, len(self._intervals_ns) - 1)
            interval = self._intervals_ns[interval_idx]
            reference_ns = entry.last_escalated_ns if entry.last_escalated_ns else entry.tracked_ns
            if now_ns - reference_ns >= interval:
                due.append(entry.alert)
        return due

    def mark_escalated(self, alert_id: str, now_ns: int) -> None:
        """Mark an alert as having been escalated at the given time."""
        entry = self._entries.get(alert_id)
        if entry is not None:
            entry.escalation_count += 1
            entry.last_escalated_ns = now_ns
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_alert_escalation.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/notifications/escalation.py tests/unit/test_alert_escalation.py
git commit -m "feat(notifications): add EscalationTracker for CRITICAL/FATAL alert resend"
```

---

### Task 4: AlertRouter Core

**Files:**
- Create: `src/hft_platform/notifications/alert_router.py`
- Create: `config/base/alert_silence.yaml`
- Create: `tests/unit/test_alert_router.py`

- [ ] **Step 1: Write failing tests for the router pipeline**

```python
# tests/unit/test_alert_router.py
"""Tests for the AlertRouter core routing pipeline."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule


def _make_alert(
    *,
    alert_id: str = "a-001",
    severity: AlertSeverity = AlertSeverity.WARN,
    category: str = "feed",
    dedup_key: str | None = None,
    ts_ns: int = 1_000_000_000_000_000_000,
) -> Alert:
    return Alert(
        alert_id=alert_id,
        severity=severity,
        category=category,
        source="test",
        title="Test alert",
        detail="Test detail",
        ts_ns=ts_ns,
        dedup_key=dedup_key,
        metadata=None,
    )


@pytest.fixture
def mock_telegram() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    return sender


@pytest.fixture
def mock_webhook() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    return sender


@pytest.fixture
def router(mock_telegram, mock_webhook):
    from hft_platform.notifications.alert_router import AlertRouter

    return AlertRouter(
        telegram_sender=mock_telegram,
        webhook_sender=mock_webhook,
    )


@pytest.mark.asyncio
async def test_warn_sends_telegram_only(router, mock_telegram, mock_webhook):
    alert = _make_alert(severity=AlertSeverity.WARN)
    await router.emit(alert)
    mock_telegram.send.assert_awaited_once()
    mock_webhook.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_critical_sends_telegram_and_webhook(router, mock_telegram, mock_webhook):
    alert = _make_alert(severity=AlertSeverity.CRITICAL)
    await router.emit(alert)
    mock_telegram.send.assert_awaited_once()
    mock_webhook.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_fatal_sends_telegram_and_webhook(router, mock_telegram, mock_webhook):
    alert = _make_alert(severity=AlertSeverity.FATAL)
    await router.emit(alert)
    mock_telegram.send.assert_awaited_once()
    mock_webhook.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_silenced_alert_not_sent(router, mock_telegram):
    rule = SilenceRule(
        rule_id="s-001",
        category="feed",
        source=None,
        severity_max=AlertSeverity.WARN,
        start_ns=0,
        end_ns=0,  # permanent
        reason="test silence",
    )
    router.add_silence(rule)
    alert = _make_alert(severity=AlertSeverity.WARN, category="feed")
    await router.emit(alert)
    mock_telegram.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_silence_allows_sending(router, mock_telegram):
    rule = SilenceRule(
        rule_id="s-001",
        category="feed",
        source=None,
        severity_max=AlertSeverity.WARN,
        start_ns=0,
        end_ns=0,
        reason="test silence",
    )
    router.add_silence(rule)
    router.remove_silence("s-001")
    alert = _make_alert(severity=AlertSeverity.WARN, category="feed")
    await router.emit(alert)
    mock_telegram.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_dedup_suppresses_duplicate(router, mock_telegram):
    a1 = _make_alert(dedup_key="dup_key", ts_ns=1_000_000_000_000_000_000)
    a2 = _make_alert(dedup_key="dup_key", ts_ns=1_001_000_000_000_000_000, alert_id="a-002")
    await router.emit(a1)
    await router.emit(a2)
    assert mock_telegram.send.await_count == 1


@pytest.mark.asyncio
async def test_info_batched_not_immediate(router, mock_telegram):
    alert = _make_alert(severity=AlertSeverity.INFO)
    await router.emit(alert)
    mock_telegram.send.assert_not_awaited()  # batched, not sent immediately


@pytest.mark.asyncio
async def test_flush_info_batch(router, mock_telegram):
    a1 = _make_alert(severity=AlertSeverity.INFO, alert_id="i-001")
    a2 = _make_alert(severity=AlertSeverity.INFO, alert_id="i-002")
    await router.emit(a1)
    await router.emit(a2)
    await router.flush_info_batch()
    mock_telegram.send.assert_awaited_once()
    msg = mock_telegram.send.call_args.args[0]
    assert "2" in msg  # batch count


def test_active_alerts_returns_unacked(router):
    a1 = _make_alert(severity=AlertSeverity.CRITICAL, alert_id="c-001")
    a2 = _make_alert(severity=AlertSeverity.FATAL, alert_id="f-001")
    router._escalation.track(a1)
    router._escalation.track(a2)
    active = router.active_alerts()
    assert len(active) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_alert_router.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create default silence config**

```yaml
# config/base/alert_silence.yaml
# Default silence rules (empty — no alerts silenced by default).
# Add rules via Telegram /silence command or here.
# Format:
#   - rule_id: "maintenance-001"
#     category: "feed"         # null = match all
#     source: null             # null = match all
#     severity_max: "WARN"     # silence INFO and WARN only
#     reason: "scheduled maintenance"
rules: []
```

- [ ] **Step 4: Implement AlertRouter**

```python
# src/hft_platform/notifications/alert_router.py
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

_DEFAULT_SILENCE_PATH = Path(__file__).resolve().parents[3] / "config" / "base" / "alert_silence.yaml"


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
        """Add a silence rule at runtime."""
        self._silence_rules[rule.rule_id] = rule
        logger.info("alert_router.silence_added", rule_id=rule.rule_id, reason=rule.reason)

    def remove_silence(self, rule_id: str) -> bool:
        """Remove a silence rule. Returns True if the rule existed."""
        removed = self._silence_rules.pop(rule_id, None)
        if removed:
            logger.info("alert_router.silence_removed", rule_id=rule_id)
        return removed is not None

    def _is_silenced(self, alert: Alert) -> bool:
        for rule in self._silence_rules.values():
            if rule.matches(alert):
                return True
        return False

    async def emit(self, alert: Alert) -> None:
        """Process and route an alert through the full pipeline."""
        # 1. Aggregation
        passed = self._aggregator.process(alert)
        if passed is None:
            return

        # 2. Silence check
        if self._is_silenced(alert):
            logger.debug("alert_router.silenced", alert_id=alert.alert_id, category=alert.category)
            return

        # 3. Route by severity
        if alert.severity == AlertSeverity.INFO:
            self._info_batch.append(alert)
            return

        if alert.severity == AlertSeverity.WARN:
            await self._send_telegram(alert)
        elif alert.severity >= AlertSeverity.CRITICAL:
            await self._send_critical(alert)
            self._escalation.track(alert)

    async def _send_telegram(self, alert: Alert) -> None:
        """Send a single alert via Telegram."""
        msg = self._format_alert(alert)
        await self._telegram.send(msg, critical=False)

    async def _send_critical(self, alert: Alert) -> None:
        """Send via both Telegram and webhook (CRITICAL/FATAL)."""
        msg = self._format_alert(alert)
        coros: list[Any] = [self._telegram.send(msg, critical=True)]
        if self._webhook is not None:
            coros.append(self._webhook.send(msg))
        await asyncio.gather(*coros, return_exceptions=True)

    @staticmethod
    def _format_alert(alert: Alert) -> str:
        """Format an alert for Telegram delivery."""
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
        """Flush accumulated INFO alerts as a single batched message."""
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
        """Acknowledge an alert, stopping escalation."""
        was_tracked = self._escalation.is_tracked(alert_id)
        self._escalation.acknowledge(alert_id)
        return was_tracked

    def active_alerts(self) -> list[Alert]:
        """Return all currently tracked (unacknowledged) alerts."""
        return [e.alert for e in self._escalation._entries.values()]

    async def tick(self, now_ns: int) -> None:
        """Called periodically to process escalations and flush batches.

        Should be called from a background task every ~30-60 seconds.
        """
        # Flush expired aggregation windows
        summaries = self._aggregator.flush_expired(now_ns)
        for s in summaries:
            msg = f"⚠️ {s.first_alert.title} — repeated {s.suppressed_count} times in past 5 minutes"
            await self._telegram.send(msg, critical=False)

        # Process escalations
        due = self._escalation.get_due(now_ns)
        for alert in due:
            logger.warning("alert_router.escalation", alert_id=alert.alert_id, title=alert.title)
            await self._send_critical(alert)
            self._escalation.mark_escalated(alert.alert_id, now_ns)

        # Flush INFO batch
        await self.flush_info_batch()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_alert_router.py -v`
Expected: All 9 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/notifications/alert_router.py config/base/alert_silence.yaml tests/unit/test_alert_router.py
git commit -m "feat(notifications): add AlertRouter with severity routing, silence, and escalation"
```

---

### Task 5: Rewire NotificationDispatcher to Use AlertRouter

**Files:**
- Modify: `src/hft_platform/notifications/dispatcher.py`
- Modify: `src/hft_platform/notifications/__init__.py`
- Create: `tests/unit/test_dispatcher_alert_integration.py`

- [ ] **Step 1: Write failing backward-compatibility tests**

```python
# tests/unit/test_dispatcher_alert_integration.py
"""Tests that NotificationDispatcher still works after AlertRouter rewire."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_sender() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    return sender


@pytest.fixture
def mock_router() -> AsyncMock:
    router = AsyncMock()
    router.emit = AsyncMock()
    return router


@pytest.fixture
def dispatcher_with_router(mock_sender, mock_router):
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    d = NotificationDispatcher(sender=mock_sender)
    d._alert_router = mock_router
    return d


@pytest.mark.asyncio
async def test_notify_halt_emits_fatal_alert(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_halt(reason="risk limit")
    mock_router.emit.assert_awaited_once()
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.FATAL
    assert alert.category == "risk"


@pytest.mark.asyncio
async def test_notify_daily_loss_emits_fatal_alert(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_daily_loss(pnl_ntd=-50000, limit_ntd=-40000)
    mock_router.emit.assert_awaited_once()
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.FATAL


@pytest.mark.asyncio
async def test_notify_stormguard_change_emits_warn_or_critical(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_stormguard_change(old="NORMAL", new="STORM", reason="vol")
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.CRITICAL


@pytest.mark.asyncio
async def test_notify_heartbeat_emits_info(dispatcher_with_router, mock_router):
    await dispatcher_with_router.notify_heartbeat(
        autonomy_state="NORMAL", pnl_scaled=0, strategies_active=1, feed_status="ok"
    )
    alert = mock_router.emit.call_args.args[0]
    from hft_platform.notifications.alert import AlertSeverity

    assert alert.severity == AlertSeverity.INFO


@pytest.mark.asyncio
async def test_fallback_to_legacy_when_no_router(mock_sender):
    """Without AlertRouter, the dispatcher falls back to direct TelegramSender."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    d = NotificationDispatcher(sender=mock_sender)
    await d.notify_halt(reason="test")
    mock_sender.send.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_dispatcher_alert_integration.py -v`
Expected: FAIL — `AttributeError: '_alert_router'` (attribute doesn't exist yet)

- [ ] **Step 3: Modify dispatcher.py to use AlertRouter**

Add `_alert_router` slot and modify each `notify_*` method to emit an `Alert` when a router is present, falling back to legacy behavior otherwise. The key changes to `src/hft_platform/notifications/dispatcher.py`:

In `__init__`, add:
```python
self._alert_router: AlertRouter | None = None
```

Add to `__slots__`:
```python
__slots__ = ("_sender", "_fallback_sender", "_alert_router")
```

Add a helper method:
```python
async def _emit_or_legacy(self, alert: Alert, legacy_msg: str, critical: bool) -> None:
    """Emit via AlertRouter if available, else fall back to direct send."""
    if self._alert_router is not None:
        await self._alert_router.emit(alert)
    elif critical:
        await self._send_critical(legacy_msg)
    else:
        await self._sender.send(legacy_msg, critical=False)
```

Then rewrite each `notify_*` method to construct an `Alert` and call `_emit_or_legacy`. For example, `notify_halt` becomes:

```python
async def notify_halt(self, reason: str) -> None:
    msg = templates.render_halt(reason=reason)
    logger.warning("dispatcher.notify_halt", reason=reason)
    alert = Alert(
        alert_id=_make_alert_id(),
        severity=AlertSeverity.FATAL,
        category="risk",
        source="risk_engine",
        title=f"HALT: {reason}",
        detail=msg,
        ts_ns=timebase.now_ns(),
        dedup_key="halt",
        metadata={"reason": reason},
    )
    await self._emit_or_legacy(alert, msg, critical=True)
```

Similarly update all 23+ methods with appropriate severity mappings:
- `notify_halt` → FATAL, category="risk"
- `notify_daily_loss` → FATAL, category="risk"
- `notify_margin_critical` → CRITICAL, category="risk"
- `notify_stormguard_change` → CRITICAL (HALT/STORM) or WARN (others), category="risk"
- `notify_autonomy_transition` → CRITICAL (HALT) or WARN, category="ops"
- `notify_flatten_result` → CRITICAL (failed>0) or INFO, category="execution"
- `notify_reconnect` → WARN, category="broker", dedup_key="reconnect"
- `notify_heartbeat` → INFO, category="ops"
- `notify_daily_report` → INFO, category="ops"
- `notify_weekly_summary` → INFO, category="ops"
- `notify_pre_market_pass` → INFO, category="ops"
- `notify_pre_market_fail` → CRITICAL, category="ops"
- `notify_reconciliation_mismatch` → WARN, category="position"
- `notify_backup_success` → INFO, category="infra"
- `notify_backup_failed` → WARN, category="infra"
- `notify_margin_warning` → WARN, category="risk"
- `notify_position_recovery` → INFO, category="position"
- `notify_position_recovery_failed` → FATAL, category="position"
- `notify_canary_action` → CRITICAL (rollback) or INFO (graduated), category="ops"
- Others → INFO or WARN as appropriate

Add the import at top:
```python
from hft_platform.core import timebase
from hft_platform.notifications.alert import Alert, AlertSeverity

def _make_alert_id() -> str:
    import uuid
    return str(uuid.uuid4())[:8]
```

- [ ] **Step 4: Update `__init__.py`**

```python
# src/hft_platform/notifications/__init__.py
"""Notification subsystem for solo-operator alerts."""

from hft_platform.notifications.alert import Alert, AlertSeverity, SilenceRule
from hft_platform.notifications.alert_router import AlertRouter
from hft_platform.notifications.dispatcher import NotificationDispatcher
from hft_platform.notifications.telegram import TelegramSender

__all__ = [
    "Alert",
    "AlertRouter",
    "AlertSeverity",
    "NotificationDispatcher",
    "SilenceRule",
    "TelegramSender",
]
```

- [ ] **Step 5: Run all notification tests**

Run: `uv run pytest tests/unit/test_dispatcher_alert_integration.py tests/unit/test_notification_dispatcher.py tests/unit/test_notification_templates.py -v`
Expected: All tests PASS (new tests + existing backward compat)

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/notifications/dispatcher.py src/hft_platform/notifications/__init__.py tests/unit/test_dispatcher_alert_integration.py
git commit -m "feat(notifications): rewire NotificationDispatcher to emit Alert via AlertRouter"
```

---

## Module C2: Operations State Machine

### Task 6: PreflightChecker

**Files:**
- Create: `src/hft_platform/ops/preflight_checker.py`
- Create: `tests/unit/test_preflight_checker.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_preflight_checker.py
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

        async def _pass():
            return CheckResult.PASS

        checker = PreflightChecker(
            checks=[
                PreflightCheck(name="broker_login", check_fn=_pass, required=True, timeout_s=5.0),
                PreflightCheck(name="redis_alive", check_fn=_pass, required=False, timeout_s=5.0),
            ]
        )
        report = await checker.run_all()
        assert report.passed is True
        assert len(report.results) == 2
        assert all(r.result == CheckResult.PASS for r in report.results)

    @pytest.mark.asyncio
    async def test_required_check_fails_blocks(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker

        async def _pass():
            return CheckResult.PASS

        async def _fail():
            return CheckResult.FAIL

        checker = PreflightChecker(
            checks=[
                PreflightCheck(name="broker_login", check_fn=_fail, required=True, timeout_s=5.0),
                PreflightCheck(name="redis_alive", check_fn=_pass, required=False, timeout_s=5.0),
            ]
        )
        report = await checker.run_all()
        assert report.passed is False
        assert report.failed_required == ["broker_login"]

    @pytest.mark.asyncio
    async def test_optional_check_fails_still_passes(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker

        async def _pass():
            return CheckResult.PASS

        async def _fail():
            return CheckResult.FAIL

        checker = PreflightChecker(
            checks=[
                PreflightCheck(name="broker_login", check_fn=_pass, required=True, timeout_s=5.0),
                PreflightCheck(name="redis_alive", check_fn=_fail, required=False, timeout_s=5.0),
            ]
        )
        report = await checker.run_all()
        assert report.passed is True

    @pytest.mark.asyncio
    async def test_warn_result_passes_but_recorded(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker

        async def _warn():
            return CheckResult.WARN

        checker = PreflightChecker(
            checks=[
                PreflightCheck(name="disk_space", check_fn=_warn, required=True, timeout_s=5.0),
            ]
        )
        report = await checker.run_all()
        assert report.passed is True
        assert report.warnings == ["disk_space"]

    @pytest.mark.asyncio
    async def test_timeout_treated_as_fail(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker

        async def _slow():
            await asyncio.sleep(10)
            return CheckResult.PASS

        checker = PreflightChecker(
            checks=[
                PreflightCheck(name="slow_check", check_fn=_slow, required=True, timeout_s=0.05),
            ]
        )
        report = await checker.run_all()
        assert report.passed is False
        assert report.failed_required == ["slow_check"]

    @pytest.mark.asyncio
    async def test_exception_treated_as_fail(self):
        from hft_platform.ops.preflight_checker import CheckResult, PreflightCheck, PreflightChecker

        async def _raise():
            raise ConnectionError("cannot connect")

        checker = PreflightChecker(
            checks=[
                PreflightCheck(name="broken_check", check_fn=_raise, required=True, timeout_s=5.0),
            ]
        )
        report = await checker.run_all()
        assert report.passed is False
        assert "broken_check" in report.failed_required
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_preflight_checker.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement PreflightChecker**

```python
# src/hft_platform/ops/preflight_checker.py
"""PreflightChecker — pre-market health check system."""
from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import structlog

logger = structlog.get_logger("ops.preflight_checker")


class CheckResult(enum.Enum):
    """Result of a single preflight check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(slots=True, frozen=True)
class PreflightCheck:
    """Definition of a single preflight check."""

    name: str
    check_fn: Callable[[], Awaitable[CheckResult]]
    required: bool
    timeout_s: float


@dataclass(slots=True)
class CheckOutcome:
    """Outcome of running a single preflight check."""

    name: str
    result: CheckResult
    required: bool
    error: str | None = None
    duration_ms: float = 0.0


@dataclass(slots=True)
class PreflightReport:
    """Aggregated result of all preflight checks."""

    passed: bool
    results: list[CheckOutcome] = field(default_factory=list)
    failed_required: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class PreflightChecker:
    """Runs pre-market health checks and produces a pass/fail report."""

    __slots__ = ("_checks",)

    def __init__(self, checks: list[PreflightCheck] | None = None) -> None:
        self._checks: list[PreflightCheck] = checks or []

    def add_check(self, check: PreflightCheck) -> None:
        """Register a new preflight check."""
        self._checks.append(check)

    async def run_all(self) -> PreflightReport:
        """Run all checks concurrently and return a PreflightReport."""
        outcomes: list[CheckOutcome] = []

        for check in self._checks:
            outcome = await self._run_single(check)
            outcomes.append(outcome)

        failed_required = [
            o.name for o in outcomes if o.required and o.result == CheckResult.FAIL
        ]
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
        """Run a single check with timeout and error handling."""
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
            logger.warning("preflight_check_timeout", name=check.name, timeout_s=check.timeout_s)
            return CheckOutcome(
                name=check.name,
                result=CheckResult.FAIL,
                required=check.required,
                error=f"timeout after {check.timeout_s}s",
                duration_ms=elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.monotonic() - start) * 1000
            logger.warning("preflight_check_error", name=check.name, error=str(exc))
            return CheckOutcome(
                name=check.name,
                result=CheckResult.FAIL,
                required=check.required,
                error=str(exc),
                duration_ms=elapsed,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_preflight_checker.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/ops/preflight_checker.py tests/unit/test_preflight_checker.py
git commit -m "feat(ops): add PreflightChecker for pre-market health checks"
```

---

### Task 7: ContractLifecycleManager

**Files:**
- Create: `src/hft_platform/ops/contract_lifecycle.py`
- Create: `tests/unit/test_contract_lifecycle.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_contract_lifecycle.py
"""Tests for ContractLifecycleManager — futures alias refresh and option chain updates."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestContractLifecycleManager:
    @pytest.mark.asyncio
    async def test_detect_expiry_warns_3_days_before(self):
        from hft_platform.ops.contract_lifecycle import ContractLifecycleManager

        alert_callback = AsyncMock()
        mgr = ContractLifecycleManager(
            contracts_runtime=MagicMock(),
            alert_callback=alert_callback,
            expiry_warn_days=[3, 1],
        )
        # Simulate a contract expiring in 3 days
        today = date(2026, 4, 16)
        expiry = today + timedelta(days=3)
        mgr._known_expiries = {"TXFE6": expiry}

        await mgr.check_expiries(today)
        alert_callback.assert_awaited_once()
        alert = alert_callback.call_args.args[0]
        assert "TXFE6" in alert.title
        from hft_platform.notifications.alert import AlertSeverity

        assert alert.severity == AlertSeverity.INFO

    @pytest.mark.asyncio
    async def test_detect_expiry_warns_1_day_before(self):
        from hft_platform.ops.contract_lifecycle import ContractLifecycleManager

        alert_callback = AsyncMock()
        mgr = ContractLifecycleManager(
            contracts_runtime=MagicMock(),
            alert_callback=alert_callback,
            expiry_warn_days=[3, 1],
        )
        today = date(2026, 4, 16)
        expiry = today + timedelta(days=1)
        mgr._known_expiries = {"TXFE6": expiry}

        await mgr.check_expiries(today)
        alert_callback.assert_awaited_once()
        alert = alert_callback.call_args.args[0]
        from hft_platform.notifications.alert import AlertSeverity

        assert alert.severity == AlertSeverity.WARN

    @pytest.mark.asyncio
    async def test_no_warning_for_distant_expiry(self):
        from hft_platform.ops.contract_lifecycle import ContractLifecycleManager

        alert_callback = AsyncMock()
        mgr = ContractLifecycleManager(
            contracts_runtime=MagicMock(),
            alert_callback=alert_callback,
            expiry_warn_days=[3, 1],
        )
        today = date(2026, 4, 16)
        expiry = today + timedelta(days=20)
        mgr._known_expiries = {"TXFE6": expiry}

        await mgr.check_expiries(today)
        alert_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refresh_futures_aliases_calls_runtime(self):
        from hft_platform.ops.contract_lifecycle import ContractLifecycleManager

        contracts_runtime = MagicMock()
        contracts_runtime.refresh_contract_cache = AsyncMock()
        contracts_runtime.resolve_symbol_aliases = MagicMock(return_value={"TMFR1": "TMFE6"})

        mgr = ContractLifecycleManager(
            contracts_runtime=contracts_runtime,
            alert_callback=AsyncMock(),
        )
        result = await mgr.refresh_futures_aliases()
        contracts_runtime.refresh_contract_cache.assert_awaited_once()
        assert result == {"TMFR1": "TMFE6"}

    @pytest.mark.asyncio
    async def test_refresh_option_chain_generates_symbols(self):
        from hft_platform.ops.contract_lifecycle import ContractLifecycleManager

        contracts_runtime = MagicMock()
        # Simulate broker returning option contracts
        mock_contract = MagicMock()
        mock_contract.code = "TXO22500C6"
        mock_contract.delivery_month = "202604"
        contracts_runtime.get_option_contracts = AsyncMock(return_value=[mock_contract])

        alert_callback = AsyncMock()
        mgr = ContractLifecycleManager(
            contracts_runtime=contracts_runtime,
            alert_callback=alert_callback,
            option_strike_range=10,
        )
        contracts = await mgr.refresh_option_chain(underlying_price=22500)
        assert isinstance(contracts, list)
        contracts_runtime.get_option_contracts.assert_awaited_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_contract_lifecycle.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement ContractLifecycleManager**

```python
# src/hft_platform/ops/contract_lifecycle.py
"""ContractLifecycleManager — automatic futures rollover and option chain updates."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Awaitable, Callable

import structlog

from hft_platform.notifications.alert import Alert, AlertSeverity

logger = structlog.get_logger("ops.contract_lifecycle")


def _make_id() -> str:
    import uuid

    return str(uuid.uuid4())[:8]


class ContractLifecycleManager:
    """Manages contract expiry detection, futures alias refresh, and option chain updates."""

    __slots__ = (
        "_contracts_runtime",
        "_alert_callback",
        "_expiry_warn_days",
        "_option_strike_range",
        "_known_expiries",
        "_warned_expiries",
    )

    def __init__(
        self,
        contracts_runtime: Any,
        alert_callback: Callable[[Alert], Awaitable[None]],
        expiry_warn_days: list[int] | None = None,
        option_strike_range: int = 10,
    ) -> None:
        self._contracts_runtime = contracts_runtime
        self._alert_callback = alert_callback
        self._expiry_warn_days = sorted(expiry_warn_days or [3, 1], reverse=True)
        self._option_strike_range = option_strike_range
        self._known_expiries: dict[str, date] = {}
        self._warned_expiries: set[tuple[str, int]] = set()

    async def check_expiries(self, today: date) -> None:
        """Check all known contracts for approaching expiry and emit alerts."""
        from hft_platform.core import timebase

        for symbol, expiry in self._known_expiries.items():
            days_until = (expiry - today).days
            for warn_days in self._expiry_warn_days:
                key = (symbol, warn_days)
                if days_until == warn_days and key not in self._warned_expiries:
                    severity = AlertSeverity.WARN if warn_days <= 1 else AlertSeverity.INFO
                    alert = Alert(
                        alert_id=_make_id(),
                        severity=severity,
                        category="contract",
                        source="contract_lifecycle",
                        title=f"Contract {symbol} expires in {days_until} days",
                        detail=f"Contract {symbol} expires on {expiry}. Days remaining: {days_until}.",
                        ts_ns=timebase.now_ns(),
                        dedup_key=f"expiry:{symbol}:{warn_days}",
                        metadata={"symbol": symbol, "expiry": str(expiry), "days_until": days_until},
                    )
                    await self._alert_callback(alert)
                    self._warned_expiries.add(key)
                    break

    async def refresh_futures_aliases(self) -> dict[str, str]:
        """Refresh futures C0/R1/R2 aliases from broker contract cache.

        Returns a mapping of alias -> actual contract code.
        """
        await self._contracts_runtime.refresh_contract_cache()
        alias_map = self._contracts_runtime.resolve_symbol_aliases()
        logger.info("contract_lifecycle.futures_aliases_refreshed", aliases=alias_map)
        return alias_map

    async def refresh_option_chain(self, underlying_price: int = 0) -> list[Any]:
        """Fetch latest option chain from broker API.

        Returns the list of option contracts retrieved.
        """
        contracts = await self._contracts_runtime.get_option_contracts()
        logger.info(
            "contract_lifecycle.option_chain_refreshed",
            count=len(contracts),
            underlying_price=underlying_price,
        )
        return contracts

    def register_expiry(self, symbol: str, expiry: date) -> None:
        """Register a known contract expiry for monitoring."""
        self._known_expiries[symbol] = expiry
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_contract_lifecycle.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/ops/contract_lifecycle.py tests/unit/test_contract_lifecycle.py
git commit -m "feat(ops): add ContractLifecycleManager for auto futures/options refresh"
```

---

### Task 8: OperationsStateMachine

**Files:**
- Create: `src/hft_platform/ops/ops_state_machine.py`
- Create: `config/base/ops_state_machine.yaml`
- Create: `tests/unit/test_ops_state_machine.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_ops_state_machine.py
"""Tests for OperationsStateMachine daily lifecycle."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


class TestOpsState:
    def test_enum_values(self):
        from hft_platform.ops.ops_state_machine import OpsState

        assert OpsState.MAINTENANCE.value == "maintenance"
        assert OpsState.PRE_MARKET.value == "pre_market"
        assert OpsState.TRADING.value == "trading"
        assert OpsState.POST_MARKET.value == "post_market"
        assert OpsState.SETTLEMENT.value == "settlement"
        assert OpsState.NIGHT_SESSION.value == "night_session"


class TestOperationsStateMachine:
    def test_initial_state_is_maintenance(self):
        from hft_platform.ops.ops_state_machine import OperationsStateMachine

        sm = OperationsStateMachine(
            session_governor=MagicMock(),
            preflight_checker=MagicMock(),
            alert_callback=AsyncMock(),
        )
        from hft_platform.ops.ops_state_machine import OpsState

        assert sm.state == OpsState.MAINTENANCE

    @pytest.mark.asyncio
    async def test_transition_to_pre_market(self):
        from hft_platform.ops.ops_state_machine import OpsState, OperationsStateMachine

        alert_cb = AsyncMock()
        sm = OperationsStateMachine(
            session_governor=MagicMock(),
            preflight_checker=MagicMock(),
            alert_callback=alert_cb,
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        assert sm.state == OpsState.PRE_MARKET
        alert_cb.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_transition_emits_info_alert(self):
        from hft_platform.ops.ops_state_machine import OpsState, OperationsStateMachine

        alert_cb = AsyncMock()
        sm = OperationsStateMachine(
            session_governor=MagicMock(),
            preflight_checker=MagicMock(),
            alert_callback=alert_cb,
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        alert = alert_cb.call_args.args[0]
        from hft_platform.notifications.alert import AlertSeverity

        assert alert.severity == AlertSeverity.INFO
        assert alert.category == "ops"

    @pytest.mark.asyncio
    async def test_pre_market_runs_preflight(self):
        from hft_platform.ops.ops_state_machine import OpsState, OperationsStateMachine
        from hft_platform.ops.preflight_checker import PreflightReport

        preflight = MagicMock()
        preflight.run_all = AsyncMock(return_value=PreflightReport(passed=True))
        sm = OperationsStateMachine(
            session_governor=MagicMock(),
            preflight_checker=preflight,
            alert_callback=AsyncMock(),
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        result = await sm.run_preflight()
        assert result.passed is True
        preflight.run_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_same_state_transition_is_noop(self):
        from hft_platform.ops.ops_state_machine import OpsState, OperationsStateMachine

        alert_cb = AsyncMock()
        sm = OperationsStateMachine(
            session_governor=MagicMock(),
            preflight_checker=MagicMock(),
            alert_callback=alert_cb,
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        alert_cb.reset_mock()
        await sm.transition_to(OpsState.PRE_MARKET)  # no-op
        alert_cb.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_state_history_recorded(self):
        from hft_platform.ops.ops_state_machine import OpsState, OperationsStateMachine

        sm = OperationsStateMachine(
            session_governor=MagicMock(),
            preflight_checker=MagicMock(),
            alert_callback=AsyncMock(),
        )
        await sm.transition_to(OpsState.PRE_MARKET)
        await sm.transition_to(OpsState.TRADING)
        assert len(sm.state_history) == 2
        assert sm.state_history[0][1] == OpsState.PRE_MARKET
        assert sm.state_history[1][1] == OpsState.TRADING
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_ops_state_machine.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create config file**

```yaml
# config/base/ops_state_machine.yaml
ops:
  pre_market_lead_minutes: 60
  post_market_delay_minutes: 15
  night_session_enabled: true

  contract_lifecycle:
    futures_refresh_cron: "0 7 * * 1-5"
    options_refresh_cron: "0 7 * * 1"
    expiry_warn_days: [3, 1]
    option_strike_range: 10

  preflight:
    timeout_s: 300
    retry_interval_s: 30
    max_retries: 5
```

- [ ] **Step 4: Implement OperationsStateMachine**

```python
# src/hft_platform/ops/ops_state_machine.py
"""OperationsStateMachine — daily lifecycle orchestrator above SessionGovernor."""
from __future__ import annotations

import enum
from typing import Any, Awaitable, Callable

import structlog

from hft_platform.core import timebase
from hft_platform.notifications.alert import Alert, AlertSeverity
from hft_platform.ops.preflight_checker import PreflightChecker, PreflightReport

logger = structlog.get_logger("ops.ops_state_machine")


def _make_id() -> str:
    import uuid

    return str(uuid.uuid4())[:8]


class OpsState(enum.StrEnum):
    """Operations state machine phases."""

    MAINTENANCE = "maintenance"
    PRE_MARKET = "pre_market"
    TRADING = "trading"
    POST_MARKET = "post_market"
    SETTLEMENT = "settlement"
    NIGHT_SESSION = "night_session"


class OperationsStateMachine:
    """Orchestrates daily lifecycle: MAINTENANCE -> PRE_MARKET -> TRADING -> POST_MARKET -> SETTLEMENT.

    Sits above SessionGovernor, managing pre-market checks, contract lifecycle,
    and post-market settlement at the daily/weekly granularity.
    """

    __slots__ = (
        "_state",
        "_session_governor",
        "_preflight_checker",
        "_alert_callback",
        "_state_history",
        "_callbacks",
    )

    def __init__(
        self,
        session_governor: Any,
        preflight_checker: PreflightChecker,
        alert_callback: Callable[[Alert], Awaitable[None]],
    ) -> None:
        self._state = OpsState.MAINTENANCE
        self._session_governor = session_governor
        self._preflight_checker = preflight_checker
        self._alert_callback = alert_callback
        self._state_history: list[tuple[int, OpsState]] = []
        self._callbacks: list[Callable[[OpsState, OpsState], Awaitable[None]]] = []

    @property
    def state(self) -> OpsState:
        """Current operations state."""
        return self._state

    @property
    def state_history(self) -> list[tuple[int, OpsState]]:
        """Read-only snapshot of state transition history: [(ts_ns, state), ...]."""
        return list(self._state_history)

    def register_callback(self, callback: Callable[[OpsState, OpsState], Awaitable[None]]) -> None:
        """Register a callback invoked on state transitions: (old, new)."""
        self._callbacks.append(callback)

    async def transition_to(self, new_state: OpsState) -> None:
        """Transition to a new state, emitting an alert and invoking callbacks."""
        if new_state == self._state:
            return
        old_state = self._state
        self._state = new_state
        now_ns = timebase.now_ns()
        self._state_history.append((now_ns, new_state))

        logger.info("ops_state_transition", old=old_state.value, new=new_state.value)

        alert = Alert(
            alert_id=_make_id(),
            severity=AlertSeverity.INFO,
            category="ops",
            source="ops_state_machine",
            title=f"Ops: {old_state.value} -> {new_state.value}",
            detail=f"Operations state changed from {old_state.value} to {new_state.value}",
            ts_ns=now_ns,
            dedup_key=None,
            metadata={"old_state": old_state.value, "new_state": new_state.value},
        )
        await self._alert_callback(alert)

        for cb in self._callbacks:
            try:
                await cb(old_state, new_state)
            except Exception as exc:  # noqa: BLE001
                logger.error("ops_state_callback_error", error=str(exc))

    async def run_preflight(self) -> PreflightReport:
        """Run all preflight checks. Returns the report."""
        return await self._preflight_checker.run_all()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_ops_state_machine.py -v`
Expected: All 6 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/ops/ops_state_machine.py config/base/ops_state_machine.yaml tests/unit/test_ops_state_machine.py
git commit -m "feat(ops): add OperationsStateMachine for daily lifecycle orchestration"
```

---

## Module C3: Self-Healing Framework

### Task 9: Fault Data Models

**Files:**
- Create: `src/hft_platform/healing/__init__.py`
- Create: `src/hft_platform/healing/fault.py`
- Create: `tests/unit/test_fault_models.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_fault_models.py
"""Tests for fault event data models."""
from __future__ import annotations

import pytest


def test_fault_category_values():
    from hft_platform.healing.fault import FaultCategory

    assert FaultCategory.FEED == "feed"
    assert FaultCategory.BROKER == "broker"
    assert FaultCategory.INFRA == "infra"
    assert FaultCategory.POSITION == "position"
    assert FaultCategory.CONTRACT == "contract"
    assert FaultCategory.EXECUTION == "execution"


def test_fault_severity_ordering():
    from hft_platform.healing.fault import FaultSeverity

    assert FaultSeverity.TRANSIENT < FaultSeverity.DEGRADED
    assert FaultSeverity.DEGRADED < FaultSeverity.IMPAIRED
    assert FaultSeverity.IMPAIRED < FaultSeverity.CRITICAL


def test_risk_level_values():
    from hft_platform.healing.fault import RiskLevel

    assert RiskLevel.AUTO == 0
    assert RiskLevel.CONFIRM == 1


def test_fault_event_creation():
    from hft_platform.healing.fault import FaultCategory, FaultEvent, FaultSeverity

    event = FaultEvent(
        fault_id="f-001",
        category=FaultCategory.FEED,
        severity=FaultSeverity.DEGRADED,
        source="shioaji_client",
        description="Feed gap 2.5s on TMFD6",
        ts_ns=1_700_000_000_000_000_000,
        context={"symbol": "TMFD6", "gap_s": 2.5},
    )
    assert event.category == FaultCategory.FEED
    assert event.severity == FaultSeverity.DEGRADED
    assert event.context["symbol"] == "TMFD6"


def test_fault_event_is_frozen():
    from hft_platform.healing.fault import FaultCategory, FaultEvent, FaultSeverity

    event = FaultEvent(
        fault_id="f-002",
        category=FaultCategory.BROKER,
        severity=FaultSeverity.IMPAIRED,
        source="broker_client",
        description="Broker disconnected",
        ts_ns=1_700_000_000_000_000_000,
        context=None,
    )
    with pytest.raises(AttributeError):
        event.severity = FaultSeverity.CRITICAL  # type: ignore[misc]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_fault_models.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement fault models**

```python
# src/hft_platform/healing/__init__.py
"""Self-healing framework for autonomous fault detection and repair."""

# src/hft_platform/healing/fault.py
"""Fault event data models for the self-healing framework."""
from __future__ import annotations

import enum
from dataclasses import dataclass


class FaultCategory(enum.StrEnum):
    """Categories of faults the healing framework can handle."""

    FEED = "feed"
    BROKER = "broker"
    INFRA = "infra"
    POSITION = "position"
    CONTRACT = "contract"
    EXECUTION = "execution"


class FaultSeverity(enum.IntEnum):
    """Severity levels for fault events."""

    TRANSIENT = 0
    DEGRADED = 1
    IMPAIRED = 2
    CRITICAL = 3


class RiskLevel(enum.IntEnum):
    """Risk level determining whether a repair action needs approval."""

    AUTO = 0
    CONFIRM = 1


@dataclass(slots=True, frozen=True)
class FaultEvent:
    """Immutable fault event emitted by detectors for the HealingOrchestrator."""

    fault_id: str
    category: FaultCategory
    severity: FaultSeverity
    source: str
    description: str
    ts_ns: int
    context: dict | None
```

Note: create two files — `__init__.py` (empty docstring) and `fault.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_fault_models.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/healing/__init__.py src/hft_platform/healing/fault.py tests/unit/test_fault_models.py
git commit -m "feat(healing): add FaultEvent, FaultCategory, FaultSeverity, RiskLevel models"
```

---

### Task 10: HealingPlaybook (YAML-Driven Repair Lookup)

**Files:**
- Create: `src/hft_platform/healing/playbook.py`
- Create: `config/base/healing_playbook.yaml`
- Create: `tests/unit/test_healing_playbook.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_healing_playbook.py
"""Tests for YAML-driven healing playbook."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hft_platform.healing.fault import FaultCategory, FaultEvent, FaultSeverity


def _make_fault(
    *,
    category: FaultCategory = FaultCategory.FEED,
    severity: FaultSeverity = FaultSeverity.DEGRADED,
    description: str = "feed_gap",
    context: dict | None = None,
) -> FaultEvent:
    return FaultEvent(
        fault_id="f-001",
        category=category,
        severity=severity,
        source="test",
        description=description,
        ts_ns=1_700_000_000_000_000_000,
        context=context or {},
    )


class TestHealingPlaybook:
    def test_load_from_yaml(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook

        config = {
            "playbooks": {
                "feed_gap_short": {
                    "match": {"category": "feed", "description_contains": "feed_gap"},
                    "actions": [
                        {"name": "unsubscribe_symbol", "risk": "auto", "timeout_s": 5},
                        {"name": "wait", "risk": "auto", "timeout_s": 3, "params": {"duration_s": 3}},
                        {"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10},
                    ],
                    "cooldown_s": 60,
                    "max_retries": 3,
                }
            }
        }
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)
        assert len(playbook._playbooks) == 1

    def test_match_by_category_and_description(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook

        config = {
            "playbooks": {
                "feed_gap_short": {
                    "match": {"category": "feed", "description_contains": "feed_gap"},
                    "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
                    "cooldown_s": 60,
                    "max_retries": 3,
                }
            }
        }
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)

        fault = _make_fault(category=FaultCategory.FEED, description="feed_gap detected on TMFD6")
        entry = playbook.find_match(fault)
        assert entry is not None
        assert entry.name == "feed_gap_short"
        assert len(entry.actions) == 1

    def test_no_match_returns_none(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook

        config = {
            "playbooks": {
                "feed_gap_short": {
                    "match": {"category": "feed", "description_contains": "feed_gap"},
                    "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
                    "cooldown_s": 60,
                    "max_retries": 3,
                }
            }
        }
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)

        fault = _make_fault(category=FaultCategory.BROKER, description="broker disconnected")
        entry = playbook.find_match(fault)
        assert entry is None

    def test_cooldown_prevents_rematch(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook

        config = {
            "playbooks": {
                "feed_gap_short": {
                    "match": {"category": "feed", "description_contains": "feed_gap"},
                    "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
                    "cooldown_s": 60,
                    "max_retries": 3,
                }
            }
        }
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)

        fault = _make_fault(ts_ns=1_000_000_000_000_000_000)
        entry = playbook.find_match(fault)
        assert entry is not None
        playbook.mark_used("feed_gap_short", ts_ns=1_000_000_000_000_000_000)

        # Within cooldown
        fault2 = FaultEvent(
            fault_id="f-002",
            category=FaultCategory.FEED,
            severity=FaultSeverity.DEGRADED,
            source="test",
            description="feed_gap again",
            ts_ns=1_030_000_000_000_000_000,  # 30s later (< 60s cooldown)
            context={},
        )
        entry2 = playbook.find_match(fault2)
        assert entry2 is None

    def test_multiple_playbooks_first_match_wins(self, tmp_path):
        from hft_platform.healing.playbook import HealingPlaybook

        config = {
            "playbooks": {
                "feed_gap_short": {
                    "match": {"category": "feed", "description_contains": "feed_gap"},
                    "actions": [{"name": "resubscribe_symbol", "risk": "auto", "timeout_s": 10}],
                    "cooldown_s": 60,
                    "max_retries": 3,
                },
                "feed_gap_long": {
                    "match": {"category": "feed", "description_contains": "feed_gap", "min_severity": "impaired"},
                    "actions": [{"name": "relogin_broker", "risk": "auto", "timeout_s": 30}],
                    "cooldown_s": 300,
                    "max_retries": 1,
                },
            }
        }
        path = tmp_path / "playbook.yaml"
        path.write_text(yaml.dump(config))
        playbook = HealingPlaybook(path)

        # Low severity matches first playbook
        fault = _make_fault(severity=FaultSeverity.DEGRADED)
        entry = playbook.find_match(fault)
        assert entry is not None
        assert entry.name == "feed_gap_short"

        # High severity matches second playbook (if first is on cooldown)
        playbook.mark_used("feed_gap_short", ts_ns=fault.ts_ns)
        fault_high = FaultEvent(
            fault_id="f-003",
            category=FaultCategory.FEED,
            severity=FaultSeverity.IMPAIRED,
            source="test",
            description="feed_gap long outage",
            ts_ns=fault.ts_ns + 10_000_000_000,  # 10s later
            context={},
        )
        entry2 = playbook.find_match(fault_high)
        assert entry2 is not None
        assert entry2.name == "feed_gap_long"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_healing_playbook.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Create the healing playbook YAML config**

```yaml
# config/base/healing_playbook.yaml
# Fault-repair mapping for the self-healing framework.
# Each playbook matches faults by category + description pattern and defines
# an ordered list of repair actions.

playbooks:
  feed_gap_short:
    match:
      category: feed
      description_contains: "feed_gap"
    actions:
      - name: unsubscribe_symbol
        risk: auto
        timeout_s: 5
      - name: wait
        risk: auto
        timeout_s: 5
        params: { duration_s: 3 }
      - name: resubscribe_symbol
        risk: auto
        timeout_s: 10
    cooldown_s: 60
    max_retries: 3

  feed_gap_long:
    match:
      category: feed
      description_contains: "feed_gap"
      min_severity: impaired
    actions:
      - name: logout_broker
        risk: auto
        timeout_s: 10
      - name: wait
        risk: auto
        timeout_s: 10
        params: { duration_s: 5 }
      - name: relogin_broker
        risk: auto
        timeout_s: 30
      - name: resubscribe_all
        risk: auto
        timeout_s: 30
    cooldown_s: 300
    max_retries: 2

  quote_flap:
    match:
      category: feed
      description_contains: "quote_flap"
    actions:
      - name: wait
        risk: auto
        timeout_s: 310
        params: { duration_s: 300 }
      - name: resubscribe_symbol
        risk: auto
        timeout_s: 10
    cooldown_s: 600
    max_retries: 2

  broker_disconnect_short:
    match:
      category: broker
      description_contains: "disconnect"
    actions:
      - name: relogin_broker
        risk: auto
        timeout_s: 30
      - name: reconcile_positions
        risk: auto
        timeout_s: 60
    cooldown_s: 120
    max_retries: 3

  broker_disconnect_long:
    match:
      category: broker
      description_contains: "disconnect"
      min_severity: impaired
    actions:
      - name: enter_reduce_only
        risk: auto
        timeout_s: 5
      - name: alert_and_wait_approval
        risk: confirm
        timeout_s: 900
      - name: relogin_broker
        risk: auto
        timeout_s: 30
      - name: reconcile_positions
        risk: auto
        timeout_s: 60
    cooldown_s: 300
    max_retries: 1

  clickhouse_unavailable:
    match:
      category: infra
      description_contains: "clickhouse"
    actions:
      - name: switch_to_wal_only
        risk: auto
        timeout_s: 5
      - name: retry_clickhouse_connect
        risk: auto
        timeout_s: 120
        params: { interval_s: 60 }
    cooldown_s: 300
    max_retries: 3

  redis_unavailable:
    match:
      category: infra
      description_contains: "redis"
    actions:
      - name: disable_live_monitor
        risk: auto
        timeout_s: 5
      - name: retry_redis_connect
        risk: auto
        timeout_s: 60
        params: { interval_s: 30 }
    cooldown_s: 120
    max_retries: 5

  disk_space_low:
    match:
      category: infra
      description_contains: "disk_space"
    actions:
      - name: archive_old_wal
        risk: auto
        timeout_s: 60
      - name: compress_logs
        risk: auto
        timeout_s: 120
    cooldown_s: 3600
    max_retries: 1

  disk_space_critical:
    match:
      category: infra
      description_contains: "disk_space_critical"
    actions:
      - name: emergency_wal_cleanup
        risk: confirm
        timeout_s: 60
      - name: stop_recording
        risk: confirm
        timeout_s: 5
    cooldown_s: 1800
    max_retries: 1

  position_drift_small:
    match:
      category: position
      description_contains: "position_drift"
    actions:
      - name: requery_broker_positions
        risk: auto
        timeout_s: 30
      - name: auto_correct_positions
        risk: auto
        timeout_s: 10
    cooldown_s: 120
    max_retries: 2

  position_drift_large:
    match:
      category: position
      description_contains: "position_drift"
      min_severity: impaired
    actions:
      - name: requery_broker_positions
        risk: auto
        timeout_s: 30
      - name: alert_and_wait_approval
        risk: confirm
        timeout_s: 900
    cooldown_s: 300
    max_retries: 1

  contract_alias_stale:
    match:
      category: contract
      description_contains: "alias_stale"
    actions:
      - name: refresh_contract_cache
        risk: auto
        timeout_s: 30
      - name: resolve_aliases
        risk: auto
        timeout_s: 10
      - name: resubscribe_all
        risk: auto
        timeout_s: 30
    cooldown_s: 3600
    max_retries: 1

  order_timeout:
    match:
      category: execution
      description_contains: "order_timeout"
    actions:
      - name: log_warn
        risk: auto
        timeout_s: 1
    cooldown_s: 60
    max_retries: 5
```

- [ ] **Step 4: Implement HealingPlaybook**

```python
# src/hft_platform/healing/playbook.py
"""HealingPlaybook — YAML-driven fault-to-repair-step lookup."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from hft_platform.healing.fault import FaultCategory, FaultEvent, FaultSeverity, RiskLevel

logger = structlog.get_logger("healing.playbook")

_SEVERITY_MAP: dict[str, FaultSeverity] = {
    "transient": FaultSeverity.TRANSIENT,
    "degraded": FaultSeverity.DEGRADED,
    "impaired": FaultSeverity.IMPAIRED,
    "critical": FaultSeverity.CRITICAL,
}

_RISK_MAP: dict[str, RiskLevel] = {
    "auto": RiskLevel.AUTO,
    "confirm": RiskLevel.CONFIRM,
}


@dataclass(slots=True, frozen=True)
class PlaybookAction:
    """A single repair action within a playbook entry."""

    name: str
    risk: RiskLevel
    timeout_s: float
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlaybookEntry:
    """A complete playbook entry: match criteria + ordered action list."""

    name: str
    match_category: str
    match_description_contains: str | None
    match_min_severity: FaultSeverity | None
    actions: list[PlaybookAction]
    cooldown_s: float
    max_retries: int


class HealingPlaybook:
    """Loads playbooks from YAML and matches FaultEvents to repair sequences."""

    __slots__ = ("_playbooks", "_last_used_ns")

    def __init__(self, config_path: Path | None = None) -> None:
        self._playbooks: list[PlaybookEntry] = []
        self._last_used_ns: dict[str, int] = {}
        if config_path is not None and config_path.exists():
            self._load(config_path)

    def _load(self, path: Path) -> None:
        try:
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            for name, cfg in raw.get("playbooks", {}).items():
                match = cfg.get("match", {})
                actions = []
                for a in cfg.get("actions", []):
                    actions.append(
                        PlaybookAction(
                            name=a["name"],
                            risk=_RISK_MAP.get(a.get("risk", "auto"), RiskLevel.AUTO),
                            timeout_s=float(a.get("timeout_s", 30)),
                            params=a.get("params", {}),
                        )
                    )
                min_sev_str = match.get("min_severity")
                self._playbooks.append(
                    PlaybookEntry(
                        name=name,
                        match_category=match.get("category", ""),
                        match_description_contains=match.get("description_contains"),
                        match_min_severity=_SEVERITY_MAP.get(min_sev_str) if min_sev_str else None,
                        actions=actions,
                        cooldown_s=float(cfg.get("cooldown_s", 60)),
                        max_retries=int(cfg.get("max_retries", 3)),
                    )
                )
            logger.info("healing_playbook_loaded", count=len(self._playbooks))
        except Exception as exc:  # noqa: BLE001
            logger.error("healing_playbook_load_failed", error=str(exc))

    def find_match(self, fault: FaultEvent) -> PlaybookEntry | None:
        """Find the first playbook entry matching the fault, respecting cooldowns."""
        for entry in self._playbooks:
            if not self._matches(entry, fault):
                continue
            # Check cooldown
            last = self._last_used_ns.get(entry.name, 0)
            cooldown_ns = int(entry.cooldown_s * 1_000_000_000)
            if last > 0 and (fault.ts_ns - last) < cooldown_ns:
                continue
            return entry
        return None

    @staticmethod
    def _matches(entry: PlaybookEntry, fault: FaultEvent) -> bool:
        if entry.match_category and fault.category.value != entry.match_category:
            return False
        if entry.match_description_contains and entry.match_description_contains not in fault.description:
            return False
        if entry.match_min_severity is not None and fault.severity < entry.match_min_severity:
            return False
        return True

    def mark_used(self, playbook_name: str, ts_ns: int) -> None:
        """Record that a playbook was used, starting its cooldown."""
        self._last_used_ns[playbook_name] = ts_ns
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_healing_playbook.py -v`
Expected: All 5 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/hft_platform/healing/playbook.py config/base/healing_playbook.yaml tests/unit/test_healing_playbook.py
git commit -m "feat(healing): add HealingPlaybook with YAML-driven fault-repair matching"
```

---

### Task 11: Action Registry

**Files:**
- Create: `src/hft_platform/healing/actions.py`
- Create: `tests/unit/test_healing_actions.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_healing_actions.py
"""Tests for healing action registry."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


class TestActionRegistry:
    def test_register_and_get(self):
        from hft_platform.healing.actions import ActionRegistry

        registry = ActionRegistry()
        action_fn = AsyncMock()
        registry.register("test_action", action_fn)
        assert registry.get("test_action") is action_fn

    def test_get_unknown_returns_none(self):
        from hft_platform.healing.actions import ActionRegistry

        registry = ActionRegistry()
        assert registry.get("unknown_action") is None

    def test_list_actions(self):
        from hft_platform.healing.actions import ActionRegistry

        registry = ActionRegistry()
        registry.register("action_a", AsyncMock())
        registry.register("action_b", AsyncMock())
        names = registry.list_actions()
        assert "action_a" in names
        assert "action_b" in names

    @pytest.mark.asyncio
    async def test_wait_action(self):
        from hft_platform.healing.actions import ActionRegistry

        registry = ActionRegistry()
        registry.register_builtins()
        wait_fn = registry.get("wait")
        assert wait_fn is not None
        # Should complete without error (sleep 0.01s)
        await wait_fn(duration_s=0.01)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_healing_actions.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement ActionRegistry**

```python
# src/hft_platform/healing/actions.py
"""ActionRegistry — concrete repair action callables for the healing framework."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import structlog

logger = structlog.get_logger("healing.actions")

# Action callable type: async function accepting **kwargs
ActionFn = Callable[..., Awaitable[None]]


class ActionRegistry:
    """Registry of named async repair actions.

    Actions are registered by name and looked up by the HealingOrchestrator
    when executing playbook steps. Platform-level actions (relogin, resubscribe,
    etc.) are registered during bootstrap via inject_platform_actions().
    """

    __slots__ = ("_actions",)

    def __init__(self) -> None:
        self._actions: dict[str, ActionFn] = {}

    def register(self, name: str, fn: ActionFn) -> None:
        """Register a named action."""
        self._actions[name] = fn

    def get(self, name: str) -> ActionFn | None:
        """Get a registered action by name."""
        return self._actions.get(name)

    def list_actions(self) -> list[str]:
        """List all registered action names."""
        return list(self._actions.keys())

    def register_builtins(self) -> None:
        """Register built-in actions (wait, log_warn)."""
        self.register("wait", _action_wait)
        self.register("log_warn", _action_log_warn)


async def _action_wait(*, duration_s: float = 1.0, **kwargs: Any) -> None:
    """Built-in wait action."""
    logger.info("healing_action.wait", duration_s=duration_s)
    await asyncio.sleep(duration_s)


async def _action_log_warn(*, message: str = "healing action triggered", **kwargs: Any) -> None:
    """Built-in log warning action."""
    logger.warning("healing_action.log_warn", message=message)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_healing_actions.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/healing/actions.py tests/unit/test_healing_actions.py
git commit -m "feat(healing): add ActionRegistry for named repair action callables"
```

---

### Task 12: HealingOrchestrator

**Files:**
- Create: `src/hft_platform/healing/orchestrator.py`
- Create: `tests/unit/test_healing_orchestrator.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_healing_orchestrator.py
"""Tests for HealingOrchestrator — full fault-to-repair flow."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from hft_platform.healing.fault import FaultCategory, FaultEvent, FaultSeverity


def _playbook_yaml(tmp_path: Path) -> Path:
    config = {
        "playbooks": {
            "feed_gap_short": {
                "match": {"category": "feed", "description_contains": "feed_gap"},
                "actions": [
                    {"name": "mock_fix", "risk": "auto", "timeout_s": 5},
                ],
                "cooldown_s": 60,
                "max_retries": 3,
            },
            "broker_disconnect_confirm": {
                "match": {"category": "broker", "description_contains": "disconnect"},
                "actions": [
                    {"name": "alert_and_wait_approval", "risk": "confirm", "timeout_s": 5},
                ],
                "cooldown_s": 300,
                "max_retries": 1,
            },
        }
    }
    path = tmp_path / "playbook.yaml"
    path.write_text(yaml.dump(config))
    return path


def _make_fault(
    *,
    category: FaultCategory = FaultCategory.FEED,
    description: str = "feed_gap on TMFD6",
) -> FaultEvent:
    return FaultEvent(
        fault_id="f-001",
        category=category,
        severity=FaultSeverity.DEGRADED,
        source="test",
        description=description,
        ts_ns=1_700_000_000_000_000_000,
        context={"symbol": "TMFD6"},
    )


class TestHealingOrchestrator:
    @pytest.mark.asyncio
    async def test_auto_action_executes(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook

        mock_action = AsyncMock()
        registry = ActionRegistry()
        registry.register("mock_fix", mock_action)

        playbook = HealingPlaybook(_playbook_yaml(tmp_path))
        alert_cb = AsyncMock()
        orch = HealingOrchestrator(
            playbook=playbook,
            action_registry=registry,
            alert_callback=alert_cb,
        )

        fault = _make_fault()
        result = await orch.handle_fault(fault)
        assert result is not None
        assert result.success is True
        mock_action.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_matching_playbook_emits_alert(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook

        playbook = HealingPlaybook(_playbook_yaml(tmp_path))
        alert_cb = AsyncMock()
        orch = HealingOrchestrator(
            playbook=playbook,
            action_registry=ActionRegistry(),
            alert_callback=alert_cb,
        )

        fault = FaultEvent(
            fault_id="f-999",
            category=FaultCategory.INFRA,
            severity=FaultSeverity.DEGRADED,
            source="test",
            description="unknown_infra_issue",
            ts_ns=1_700_000_000_000_000_000,
            context=None,
        )
        result = await orch.handle_fault(fault)
        assert result is None
        alert_cb.assert_awaited_once()  # "no playbook" alert

    @pytest.mark.asyncio
    async def test_action_failure_stops_sequence(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook

        registry = ActionRegistry()
        registry.register("mock_fix", AsyncMock(side_effect=RuntimeError("broken")))

        playbook = HealingPlaybook(_playbook_yaml(tmp_path))
        alert_cb = AsyncMock()
        orch = HealingOrchestrator(
            playbook=playbook,
            action_registry=registry,
            alert_callback=alert_cb,
        )

        fault = _make_fault()
        result = await orch.handle_fault(fault)
        assert result is not None
        assert result.success is False

    @pytest.mark.asyncio
    async def test_confirm_action_emits_critical_alert(self, tmp_path):
        from hft_platform.healing.actions import ActionRegistry
        from hft_platform.healing.orchestrator import HealingOrchestrator
        from hft_platform.healing.playbook import HealingPlaybook

        registry = ActionRegistry()
        # Don't register alert_and_wait_approval — orchestrator should handle confirm flow

        playbook = HealingPlaybook(_playbook_yaml(tmp_path))
        alert_cb = AsyncMock()
        orch = HealingOrchestrator(
            playbook=playbook,
            action_registry=registry,
            alert_callback=alert_cb,
        )

        fault = FaultEvent(
            fault_id="f-002",
            category=FaultCategory.BROKER,
            severity=FaultSeverity.IMPAIRED,
            source="test",
            description="broker disconnect extended",
            ts_ns=1_700_000_000_000_000_000,
            context=None,
        )
        result = await orch.handle_fault(fault)
        # Should pause at confirm step and emit a CRITICAL alert
        assert result is not None
        assert result.pending_approval is True
        # Check that a CRITICAL alert was emitted
        found_critical = False
        for call in alert_cb.call_args_list:
            alert = call.args[0]
            from hft_platform.notifications.alert import AlertSeverity

            if alert.severity == AlertSeverity.CRITICAL:
                found_critical = True
        assert found_critical
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_healing_orchestrator.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement HealingOrchestrator**

```python
# src/hft_platform/healing/orchestrator.py
"""HealingOrchestrator — core fault-to-repair execution engine."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import structlog

from hft_platform.core import timebase
from hft_platform.healing.actions import ActionRegistry
from hft_platform.healing.fault import FaultEvent, RiskLevel
from hft_platform.healing.playbook import HealingPlaybook, PlaybookAction
from hft_platform.notifications.alert import Alert, AlertSeverity

logger = structlog.get_logger("healing.orchestrator")


def _make_id() -> str:
    import uuid

    return str(uuid.uuid4())[:8]


@dataclass(slots=True)
class HealingResult:
    """Result of a healing attempt."""

    fault_id: str
    playbook_name: str
    success: bool
    actions_completed: int
    actions_total: int
    error: str | None = None
    pending_approval: bool = False
    duration_ms: float = 0.0


class HealingOrchestrator:
    """Processes FaultEvents: finds matching playbook, executes repair actions in sequence.

    AUTO-risk actions execute immediately.
    CONFIRM-risk actions emit a CRITICAL alert and pause, setting pending_approval=True.
    """

    __slots__ = (
        "_playbook",
        "_registry",
        "_alert_callback",
        "_pending_faults",
    )

    def __init__(
        self,
        playbook: HealingPlaybook,
        action_registry: ActionRegistry,
        alert_callback: Callable[[Alert], Awaitable[None]],
    ) -> None:
        self._playbook = playbook
        self._registry = action_registry
        self._alert_callback = alert_callback
        self._pending_faults: dict[str, tuple[FaultEvent, str, int]] = {}  # fault_id -> (fault, playbook, step_idx)

    async def handle_fault(self, fault: FaultEvent) -> HealingResult | None:
        """Process a fault event: find playbook, execute repair actions.

        Returns HealingResult on match, None if no playbook found (emits alert).
        """
        import time

        start = time.monotonic()

        entry = self._playbook.find_match(fault)
        if entry is None:
            await self._alert_callback(
                Alert(
                    alert_id=_make_id(),
                    severity=AlertSeverity.WARN,
                    category=fault.category.value,
                    source="healing_orchestrator",
                    title=f"No playbook for fault: {fault.description}",
                    detail=f"Fault {fault.fault_id} ({fault.category.value}) has no matching playbook. Manual intervention may be needed.",
                    ts_ns=timebase.now_ns(),
                    dedup_key=f"no_playbook:{fault.category.value}",
                    metadata={"fault_id": fault.fault_id},
                )
            )
            return None

        logger.info(
            "healing_orchestrator.executing",
            fault_id=fault.fault_id,
            playbook=entry.name,
            actions=len(entry.actions),
        )

        completed = 0
        for i, action_def in enumerate(entry.actions):
            if action_def.risk == RiskLevel.CONFIRM:
                await self._alert_callback(
                    Alert(
                        alert_id=_make_id(),
                        severity=AlertSeverity.CRITICAL,
                        category=fault.category.value,
                        source="healing_orchestrator",
                        title=f"Approval needed: {action_def.name}",
                        detail=f"Fault {fault.fault_id}: playbook '{entry.name}' step {i + 1} requires /approve {fault.fault_id}",
                        ts_ns=timebase.now_ns(),
                        dedup_key=f"confirm:{fault.fault_id}",
                        metadata={"fault_id": fault.fault_id, "action": action_def.name},
                    )
                )
                self._pending_faults[fault.fault_id] = (fault, entry.name, i)
                self._playbook.mark_used(entry.name, fault.ts_ns)
                elapsed = (time.monotonic() - start) * 1000
                return HealingResult(
                    fault_id=fault.fault_id,
                    playbook_name=entry.name,
                    success=False,
                    actions_completed=completed,
                    actions_total=len(entry.actions),
                    pending_approval=True,
                    duration_ms=elapsed,
                )

            action_fn = self._registry.get(action_def.name)
            if action_fn is None:
                logger.warning("healing_orchestrator.action_not_found", action=action_def.name)
                continue

            try:
                await asyncio.wait_for(
                    action_fn(**action_def.params),
                    timeout=action_def.timeout_s,
                )
                completed += 1
            except Exception as exc:  # noqa: BLE001
                elapsed = (time.monotonic() - start) * 1000
                logger.error(
                    "healing_orchestrator.action_failed",
                    action=action_def.name,
                    error=str(exc),
                )
                await self._alert_callback(
                    Alert(
                        alert_id=_make_id(),
                        severity=AlertSeverity.FATAL,
                        category=fault.category.value,
                        source="healing_orchestrator",
                        title=f"Healing failed: {action_def.name}",
                        detail=f"Fault {fault.fault_id}: action '{action_def.name}' failed: {exc}",
                        ts_ns=timebase.now_ns(),
                        dedup_key=f"healing_fail:{fault.fault_id}",
                        metadata={"fault_id": fault.fault_id, "action": action_def.name, "error": str(exc)},
                    )
                )
                self._playbook.mark_used(entry.name, fault.ts_ns)
                return HealingResult(
                    fault_id=fault.fault_id,
                    playbook_name=entry.name,
                    success=False,
                    actions_completed=completed,
                    actions_total=len(entry.actions),
                    error=str(exc),
                    duration_ms=elapsed,
                )

        self._playbook.mark_used(entry.name, fault.ts_ns)
        elapsed = (time.monotonic() - start) * 1000
        logger.info(
            "healing_orchestrator.completed",
            fault_id=fault.fault_id,
            playbook=entry.name,
            actions_completed=completed,
            duration_ms=round(elapsed, 1),
        )
        return HealingResult(
            fault_id=fault.fault_id,
            playbook_name=entry.name,
            success=True,
            actions_completed=completed,
            actions_total=len(entry.actions),
            duration_ms=elapsed,
        )

    def approve(self, fault_id: str) -> bool:
        """Approve a pending CONFIRM-level repair. Returns True if fault was pending."""
        return self._pending_faults.pop(fault_id, None) is not None

    def reject(self, fault_id: str) -> bool:
        """Reject a pending repair. Returns True if fault was pending."""
        return self._pending_faults.pop(fault_id, None) is not None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_healing_orchestrator.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/healing/orchestrator.py tests/unit/test_healing_orchestrator.py
git commit -m "feat(healing): add HealingOrchestrator with playbook-driven repair execution"
```

---

### Task 13: AutonomyMonitor FaultEvent Emission

**Files:**
- Modify: `src/hft_platform/ops/autonomy_monitor.py`
- Create: `tests/unit/test_autonomy_monitor_fault_emit.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_autonomy_monitor_fault_emit.py
"""Tests for AutonomyMonitor emitting FaultEvents when HFT_HEALING_ENABLED=1."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import StormGuardState


@pytest.fixture
def mock_deps():
    storm_guard = MagicMock()
    storm_guard.state = StormGuardState.NORMAL
    platform_degrade = MagicMock()
    platform_degrade.reduce_only_active = False
    platform_inputs = MagicMock()
    platform_inputs.reduce_only_reasons.return_value = []
    return storm_guard, platform_degrade, platform_inputs


class TestAutonomyMonitorFaultEmit:
    def test_broker_disconnect_emits_fault_event(self, mock_deps):
        """When healing is enabled, broker disconnect emits FaultEvent instead of direct action."""
        storm_guard, platform_degrade, platform_inputs = mock_deps

        from hft_platform.ops.autonomy_monitor import AutonomyMonitor

        fault_callback = MagicMock()
        broker = MagicMock()
        broker.is_connected.return_value = False

        monitor = AutonomyMonitor(
            storm_guard=storm_guard,
            platform_degrade=platform_degrade,
            platform_inputs=platform_inputs,
            broker_client=broker,
            fault_callback=fault_callback,
        )
        monitor._broker_was_connected = False
        monitor._broker_disconnect_since_ns = 1_000_000_000_000_000_000 - 301_000_000_000

        decisions = monitor._evaluate()

        if os.getenv("HFT_HEALING_ENABLED", "0") == "1":
            # Should have called fault_callback with a FaultEvent
            fault_callback.assert_called_once()
            from hft_platform.healing.fault import FaultCategory

            fault = fault_callback.call_args.args[0]
            assert fault.category == FaultCategory.BROKER
        else:
            # Legacy path: MonitorDecision
            if decisions:
                assert decisions[0].action == "enter_reduce_only"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_autonomy_monitor_fault_emit.py -v`
Expected: FAIL — `TypeError` (fault_callback not accepted yet)

- [ ] **Step 3: Add fault_callback to AutonomyMonitor**

Add `fault_callback` parameter to `AutonomyMonitor.__init__`, add to `__slots__`, and gate behavior on `HFT_HEALING_ENABLED`. In `_check_broker_disconnect`, when healing is enabled, emit `FaultEvent` instead of appending `MonitorDecision`:

```python
# In __init__, add parameter and slot:
self._fault_callback = fault_callback
self._healing_enabled = os.getenv("HFT_HEALING_ENABLED", "0") in ("1", "true", "yes")
```

In `_check_broker_disconnect`, add the healing branch:
```python
if self._healing_enabled and self._fault_callback is not None:
    from hft_platform.healing.fault import FaultCategory, FaultEvent, FaultSeverity
    fault = FaultEvent(
        fault_id=f"broker-disc-{now_ns}",
        category=FaultCategory.BROKER,
        severity=FaultSeverity.IMPAIRED,
        source="autonomy_monitor",
        description="broker disconnect > 5min",
        ts_ns=now_ns,
        context={"elapsed_ns": elapsed_ns},
    )
    self._fault_callback(fault)
else:
    # Legacy path
    decisions.append(MonitorDecision(...))
```

Apply the same pattern for infra health checks (`_INFRA_REASON_MAP` block) and reconciliation drift.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_autonomy_monitor_fault_emit.py tests/unit/test_notification_autonomy.py -v`
Expected: All tests PASS (new + existing)

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/ops/autonomy_monitor.py tests/unit/test_autonomy_monitor_fault_emit.py
git commit -m "feat(healing): AutonomyMonitor emits FaultEvent when HFT_HEALING_ENABLED=1"
```

---

### Task 14: Run Full Test Suite and Lint

- [ ] **Step 1: Run full test suite**

Run: `uv run pytest tests/unit/test_alert_models.py tests/unit/test_alert_aggregator.py tests/unit/test_alert_escalation.py tests/unit/test_alert_router.py tests/unit/test_dispatcher_alert_integration.py tests/unit/test_preflight_checker.py tests/unit/test_contract_lifecycle.py tests/unit/test_ops_state_machine.py tests/unit/test_fault_models.py tests/unit/test_healing_playbook.py tests/unit/test_healing_actions.py tests/unit/test_healing_orchestrator.py tests/unit/test_autonomy_monitor_fault_emit.py -v`
Expected: All tests PASS

- [ ] **Step 2: Run existing notification tests for regression**

Run: `uv run pytest tests/unit/test_notification_dispatcher.py tests/unit/test_notification_templates.py tests/unit/test_notification_autonomy.py tests/unit/test_session_governor.py -v`
Expected: All existing tests PASS (no regressions)

- [ ] **Step 3: Run lint**

Run: `uv run ruff check src/hft_platform/notifications/ src/hft_platform/ops/ src/hft_platform/healing/`
Expected: No errors

- [ ] **Step 4: Run type check**

Run: `uv run mypy src/hft_platform/notifications/alert.py src/hft_platform/notifications/aggregator.py src/hft_platform/notifications/escalation.py src/hft_platform/notifications/alert_router.py src/hft_platform/healing/fault.py src/hft_platform/healing/playbook.py src/hft_platform/healing/actions.py src/hft_platform/healing/orchestrator.py src/hft_platform/ops/preflight_checker.py src/hft_platform/ops/contract_lifecycle.py src/hft_platform/ops/ops_state_machine.py`
Expected: No type errors

- [ ] **Step 5: Fix any issues and commit**

```bash
git add -A
git commit -m "chore: fix lint and type issues in Track C modules"
```
