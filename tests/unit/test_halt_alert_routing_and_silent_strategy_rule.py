"""A latched HALT must not re-page every 15 minutes; a silent strategy must page once."""

from __future__ import annotations

from pathlib import Path

import yaml

ALERTS = Path(__file__).resolve().parents[2] / "config" / "monitoring" / "alerts"


def _routes() -> list[dict]:
    return yaml.safe_load((ALERTS / "alertmanager.yml").read_text())["route"]["routes"]


def _rules() -> dict[str, dict]:
    doc = yaml.safe_load((ALERTS / "rules.yaml").read_text())
    return {r["alert"]: r for g in doc["groups"] for r in g["rules"] if "alert" in r}


def test_storm_guard_halt_route_repeats_slower_than_the_critical_default():
    routes = _routes()
    halt_idx = next(i for i, r in enumerate(routes) if r.get("match", {}).get("alertname") == "StormGuardHalt")
    critical_idx = next(i for i, r in enumerate(routes) if r.get("match", {}).get("severity") == "critical")

    assert halt_idx < critical_idx  # first match wins; after the critical route it is dead config
    assert routes[halt_idx]["repeat_interval"] == "4h"
    assert routes[critical_idx]["repeat_interval"] == "15m"
    assert routes[halt_idx]["continue"] is False


def test_silent_strategy_rule_requires_position_silence_and_open_session():
    rule = _rules()["StrategyHoldsPositionButEmitsNoIntents"]

    assert "position_age_seconds" in rule["expr"]
    assert "strategy_intents_total" in rule["expr"]
    assert "market_trading_hours_active == 1" in rule["expr"]
    assert rule["for"] == "15m"
    assert rule["labels"]["severity"] == "warning"
