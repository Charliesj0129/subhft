"""Steering brief: deterministic render, round-trip parse, approval gate."""

from __future__ import annotations

from research.candidate_loop.governor.brief import is_approved, parse_brief, render_brief
from research.candidate_loop.governor.signals import SteeringSignals

FIXED_TS = "2026-06-14T00:00:00+00:00"


def _signals() -> SteeringSignals:
    return SteeringSignals(
        family="trade_flow",
        candidates=20,
        survival_rate=0.10,
        ic_p10=0.02,
        ic_p50=0.114,
        ic_p90=0.20,
        cost_failure_rate=0.55,
        maker_cost_failure_rate=0.40,
        maker_rescuable_count=2,
        duplicate_rate=0.05,
        reduced_day_coverage_count=7,
        near_misses=[{"alpha_id": "a1", "failed_gate": "cost_proxy_taker", "margin": -0.03}],
        common_failure_patterns=[],
    )


def _render() -> str:
    return render_brief(
        _signals(),
        focus="amplify",
        n_target=30,
        source_run_id="smoke_001",
        generated_at=FIXED_TS,
    )


def test_render_brief_defaults_to_unapproved():
    text = _render()
    assert "approved: false" in text
    assert is_approved(text) is False


def test_render_brief_is_byte_stable_for_fixed_timestamp():
    assert _render() == _render()


def test_parse_brief_round_trips_frontmatter_and_body():
    brief = parse_brief(_render())
    assert brief.family == "trade_flow"
    assert brief.source_run_id == "smoke_001"
    assert brief.focus == "amplify"
    assert brief.n_target == 30
    assert brief.approved is False
    assert "## Why" in brief.body
    assert "## Focus" in brief.body
    assert "## Avoid" in brief.body


def test_flipping_approved_true_is_detected():
    text = _render().replace("approved: false", "approved: true")
    assert is_approved(text) is True
    assert parse_brief(text).approved is True


def test_brief_body_surfaces_maker_rescue_and_near_miss():
    body = parse_brief(_render()).body
    assert "maker" in body.lower()
    assert "a1" in body  # the near-miss alpha_id
