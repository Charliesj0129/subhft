"""The order DLQ alert is a warning; an order that did not end cleanly pages.

THESHOW 2026-10-29..30 (SIM broker): 91 order dead-letters, all timed-out
``place_order`` calls (broker RTT 5-19 s against a 3 s timeout), paged critical
9 times. ``_bind_late_trade`` had cancelled 76 of those late Trades cleanly.
``OrderOutcomeUnresolved`` pages only the outcomes the platform no longer knows
the end of. Two of them never reached ``increase()`` that week -- the first
``cancel_timeout`` (15:10Z) and the only ``fill_claimed_after_expiry`` (15:38Z)
-- because a labelled child born at 1 has no increase. The registry now creates
them at 0. These tests tie the rule, the registry and the adapter together, so
renaming an outcome in one place cannot leave the page silently empty.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from hft_platform.observability.metrics import PAGED_PHANTOM_OUTCOMES, UNRESOLVED_PHANTOM_OUTCOMES, MetricsRegistry

_ROOT = Path(__file__).resolve().parents[2]
_RULES = _ROOT / "config" / "monitoring" / "alerts" / "rules.yaml"
_ADAPTER = _ROOT / "src" / "hft_platform" / "order" / "adapter.py"


def _alerts() -> dict[str, dict]:
    data = yaml.safe_load(_RULES.read_text())
    return {r["alert"]: r for g in data["groups"] for r in g["rules"] if "alert" in r}


def test_unresolved_alert_selects_exactly_the_paged_outcomes() -> None:
    expr = _alerts()["OrderOutcomeUnresolved"]["expr"]

    match = re.search(r'phantom_bound_total\{outcome=~?"([^"]+)"\}', expr)

    assert match, f"OrderOutcomeUnresolved no longer selects phantom_bound_total outcomes: {expr!r}"
    assert set(match.group(1).split("|")) == set(PAGED_PHANTOM_OUTCOMES)
    assert set(PAGED_PHANTOM_OUTCOMES) <= set(UNRESOLVED_PHANTOM_OUTCOMES)


def test_a_cancel_pages_only_once_its_watch_gave_up() -> None:
    """2026-11-02: cancel_timeout / cancel_failed paged at the slow call; the
    cancel watch that follows each one knows whether the order stayed live."""
    expr = _alerts()["OrderOutcomeUnresolved"]["expr"]

    assert 'cancel_unconfirmed_total{outcome="gave_up"}' in expr
    assert "cancel_timeout" not in expr and "cancel_failed" not in expr


def test_registry_creates_the_gave_up_child_at_zero() -> None:
    MetricsRegistry._instance = None
    try:
        counter = MetricsRegistry.get().cancel_unconfirmed_total
        samples = {
            s.labels["outcome"]: s.value
            for metric in counter.collect()
            for s in metric.samples
            if s.name.endswith("_total")
        }
    finally:
        MetricsRegistry._instance = None

    assert samples.get("gave_up") == 0.0


def test_registry_creates_each_unresolved_outcome_at_zero() -> None:
    MetricsRegistry._instance = None
    try:
        counter = MetricsRegistry.get().phantom_bound_total
        samples = {
            s.labels["outcome"]: s.value
            for metric in counter.collect()
            for s in metric.samples
            if s.name.endswith("_total")
        }
    finally:
        MetricsRegistry._instance = None

    assert {outcome: samples.get(outcome) for outcome in UNRESOLVED_PHANTOM_OUTCOMES} == dict.fromkeys(
        UNRESOLVED_PHANTOM_OUTCOMES, 0.0
    )


def test_each_unresolved_outcome_is_a_string_the_order_adapter_emits() -> None:
    source = _ADAPTER.read_text()

    missing = [outcome for outcome in UNRESOLVED_PHANTOM_OUTCOMES if f'"{outcome}"' not in source]

    assert not missing, f"OrderOutcomeUnresolved pages on outcomes the adapter never emits: {missing}"


def test_order_dead_letter_is_a_warning_and_an_unresolved_order_is_critical() -> None:
    alerts = _alerts()

    assert alerts["OrderDeadLetterQueueGrowing"]["labels"]["severity"] == "warning"
    assert alerts["OrderOutcomeUnresolved"]["labels"]["severity"] == "critical"
    # The recorder half of the DLQ split still pages: those rows are not in ClickHouse.
    assert alerts["RecorderDeadLetterQueueGrowing"]["labels"]["severity"] == "critical"
