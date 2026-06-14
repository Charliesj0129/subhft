"""Governor signals: config loading, failure_summary extraction, focus labels."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.candidate_loop.governor.signals import (
    classify_focus,
    extract_signals,
    load_governor_config,
    n_target_for,
)

CFG_PATH = (
    Path(__file__).resolve().parents[4]
    / "config" / "research" / "candidate_loop" / "governor_v1.yaml"
)


def _summary() -> dict:
    return {
        "run_id": "smoke_001",
        "per_family": {
            "trade_flow": {  # alive → amplify
                "candidates": 20,
                "survival_rate": 0.10,
                "ic_distribution_survivors": {"p10": 0.02, "p50": 0.114, "p90": 0.20},
                "cost_failure_rate": 0.55,
                "maker_cost_failure_rate": 0.40,
                "maker_rescuable_count": 2,
                "duplicate_rate": 0.05,
                "reduced_day_coverage_count": 7,
                "near_misses": [
                    {"alpha_id": "a1", "failed_gate": "cost_proxy_taker", "margin": -0.03}
                ],
                "common_failure_patterns": [],
            },
            "microprice": {  # dead, no rescue → retire
                "candidates": 20,
                "survival_rate": 0.0,
                "ic_distribution_survivors": {"p10": 0.0, "p50": 0.0, "p90": 0.0},
                "cost_failure_rate": 1.0,
                "maker_cost_failure_rate": 1.0,
                "maker_rescuable_count": 0,
                "duplicate_rate": 0.0,
                "reduced_day_coverage_count": 0,
                "near_misses": [],
                "common_failure_patterns": [],
            },
            "depth_delta": {  # weak, no rescue → deprioritize
                "candidates": 20,
                "survival_rate": 0.03,
                "ic_distribution_survivors": {"p10": 0.0, "p50": 0.01, "p90": 0.02},
                "cost_failure_rate": 0.3,
                "maker_cost_failure_rate": 0.2,
                "maker_rescuable_count": 0,
                "duplicate_rate": 0.0,
                "reduced_day_coverage_count": 0,
                "near_misses": [],
                "common_failure_patterns": [],
            },
            "spread_regime": {  # middling, no rescue → maintain
                "candidates": 20,
                "survival_rate": 0.08,
                "ic_distribution_survivors": {"p10": 0.0, "p50": 0.02, "p90": 0.03},
                "cost_failure_rate": 0.2,
                "maker_cost_failure_rate": 0.1,
                "maker_rescuable_count": 0,
                "duplicate_rate": 0.0,
                "reduced_day_coverage_count": 0,
                "near_misses": [],
                "common_failure_patterns": [],
            },
        },
    }


def test_load_governor_config_reads_thresholds_and_model():
    cfg = load_governor_config(CFG_PATH)
    assert cfg.governor_version == "gov_v1"
    assert cfg.model_name == "deepseek-chat"
    assert cfg.base_url.startswith("https://")
    assert cfg.n_target["amplify"] == 30
    assert cfg.amplify_ic_p50_min == pytest.approx(0.05)


def test_extract_signals_maps_failure_summary_fields():
    sig = extract_signals(_summary(), "trade_flow")
    assert sig.family == "trade_flow"
    assert sig.survival_rate == pytest.approx(0.10)
    assert sig.ic_p50 == pytest.approx(0.114)
    assert sig.maker_rescuable_count == 2
    assert sig.reduced_day_coverage_count == 7
    assert sig.near_misses[0]["failed_gate"] == "cost_proxy_taker"


@pytest.mark.parametrize(
    "family,expected",
    [
        ("trade_flow", "amplify"),
        ("microprice", "retire"),
        ("depth_delta", "deprioritize"),
        ("spread_regime", "maintain"),
    ],
)
def test_classify_focus_is_deterministic_per_family(family, expected):
    cfg = load_governor_config(CFG_PATH)
    sig = extract_signals(_summary(), family)
    assert classify_focus(sig, cfg) == expected


def test_n_target_for_maps_focus_to_count():
    cfg = load_governor_config(CFG_PATH)
    assert n_target_for("amplify", cfg) == 30
    assert n_target_for("retire", cfg) == 5
