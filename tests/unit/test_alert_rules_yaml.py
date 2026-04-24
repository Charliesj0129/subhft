"""Regression tests for config/monitoring/alerts/rules.yaml alert expressions.

These tests catch structural mistakes in the Prometheus alert YAML that would
silently produce false positives or false negatives in production.

Scope: YAML structure and PromQL expression invariants only. Full PromQL
semantic correctness requires `promtool test rules` and is not covered here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

RULES_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "monitoring" / "alerts" / "rules.yaml"
)


def _load_alerts_by_name() -> dict[str, dict]:
    with RULES_PATH.open() as f:
        data = yaml.safe_load(f)
    alerts: dict[str, dict] = {}
    for group in data.get("groups", []):
        for rule in group.get("rules", []):
            name = rule.get("alert")
            if name:
                alerts[name] = rule
    return alerts


def test_alpha_signal_silent_gates_on_nonzero_timestamp():
    """AlphaSignalSilent must not fire when `alpha_last_signal_ts` is 0.

    The gauge defaults to 0 for any registered strategy that has not yet emitted
    an intent (e.g. config-disabled strategies that still have executor entries
    built in StrategyRunner). Without a `> 0` gate, `time() - 0` always exceeds
    300 and the alert fires from engine startup forever.
    """
    alerts = _load_alerts_by_name()
    assert "AlphaSignalSilent" in alerts, "AlphaSignalSilent alert missing from rules.yaml"
    expr = alerts["AlphaSignalSilent"]["expr"]
    assert "alpha_last_signal_ts > 0" in expr, (
        "AlphaSignalSilent expression must gate on a non-zero gauge value to prevent "
        "false positives for strategies that have never emitted an intent. "
        f"Current expression: {expr!r}"
    )


def test_redis_connection_down_alert_removed():
    """RedisConnectionDown must not exist — engine does not probe Redis.

    The `redis_connection_health` gauge has no setter anywhere in the codebase
    (engine container lacks the `redis` Python module; only the separate
    monitor container uses Redis via `hft_platform.monitor._redis_*`). The
    alert therefore always fires on a gauge that is default-initialised to 0.
    """
    alerts = _load_alerts_by_name()
    assert "RedisConnectionDown" not in alerts, (
        "RedisConnectionDown alert references a gauge that no engine-side code "
        "ever sets. Remove the alert (and the dead gauge) until Redis health is "
        "probed from the component that actually uses Redis."
    )
