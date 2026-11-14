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


_STALL = re.compile(r"max_over_time\(order_broker_silence_seconds\[(\d+)m\]\) >= 10")


def test_critical_alert_holds_cancel_and_live_ttl_outside_the_stall_mask() -> None:
    """2026-11-13: only the outcomes a broker stall explains are masked; cancel_gave_up and
    live_order_ttl_release had stall-free runs with no confirmed broker terminal and stay critical."""
    expr = _alerts()["OrderOutcomeUnresolved"]["expr"]

    assert expr.count("unless on()") == 1
    unmasked, _, rest = expr.partition("or (")
    masked, _, mask = rest.partition("unless on()")

    assert "cancel_unconfirmed_total" in unmasked and "live_order_ttl_releases_total" in unmasked
    assert "phantom_recovery_releases_total" not in unmasked and "phantom_bound_total" not in unmasked
    assert "phantom_recovery_releases_total" in masked and "phantom_bound_total" in masked
    assert _STALL.search(mask)


def test_stall_warning_selects_the_same_outcomes_with_the_complementary_condition() -> None:
    alerts = _alerts()
    critical, warning = alerts["OrderOutcomeUnresolved"], alerts["OrderOutcomeUnresolvedDuringBrokerStall"]

    assert warning["labels"]["severity"] == "warning"
    assert "{{ $labels.outcome }}" in warning["annotations"]["summary"]
    for term in ('phantom_bound_total{outcome="fill_claimed_after_expiry"}', "phantom_recovery_releases_total"):
        assert term in warning["expr"] and term in critical["expr"]
    assert "cancel_unconfirmed_total" not in warning["expr"] and "live_order_ttl_releases_total" not in warning["expr"]
    assert "and on()" in warning["expr"] and "unless on()" in critical["expr"]
    # One stall look-back for both, so each run is exactly one of the two severities.
    assert _STALL.search(warning["expr"]) and _STALL.search(critical["expr"])
    assert _STALL.search(warning["expr"]).group(1) == _STALL.search(critical["expr"]).group(1)


def test_stall_look_back_outlasts_the_increase_window_by_the_phantom_ttl() -> None:
    """A phantom releases up to 300 s after its stall, so a look-back equal to the 30m increase() window
    stops masking in the last minutes of a run and pages critical there."""
    expr = _alerts()["OrderOutcomeUnresolved"]["expr"]
    increase_window = int(re.search(r"phantom_recovery_releases_total\[(\d+)m\]", expr).group(1))
    look_back = int(_STALL.search(expr).group(1))

    assert look_back >= increase_window + 5


def test_both_unresolved_alerts_merge_flapping_runs_for_an_hour() -> None:
    alerts = _alerts()

    assert alerts["OrderOutcomeUnresolved"]["keep_firing_for"] == "1h"
    assert alerts["OrderOutcomeUnresolvedDuringBrokerStall"]["keep_firing_for"] == "1h"


def test_critical_alert_pages_a_misattributed_phantom_claim_regardless_of_stall() -> None:
    """A misattributed claim means a fill was booked to the wrong order and that
    order's working cancel was sent: a position question no broker stall explains.
    It pages critical outside the stall mask, with its own outcome label."""
    expr = _alerts()["OrderOutcomeUnresolved"]["expr"]
    unmasked, _, _ = expr.partition("or (")

    assert 'phantom_claim_total{outcome="misattributed"}' in unmasked
    assert '"outcome", "claim_misattributed"' in unmasked
