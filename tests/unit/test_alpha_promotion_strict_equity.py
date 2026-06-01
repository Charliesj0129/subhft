"""L6 — Strict-only validation + equity-source classifier tests.

Covers:
  * synthetic scorecard rejected at Gate D ``equity_source`` check.
  * ``real`` scorecard passes Gate D ``equity_source`` check.
  * ``real_no_trade`` scorecard passes (legitimate no-fill session).
  * ``screen_only=true`` scorecard rejected before any gate runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hft_platform.alpha._validation_profile import ValidationProfile
from hft_platform.alpha.promotion import (
    PromotionConfig,
    PromotionError,
    _evaluate_gate_d,
    promote_alpha,
)


def _strict_profile() -> ValidationProfile:
    # Bypass blocking-sub-gate registry validation by constructing directly.
    return ValidationProfile(
        name="strict_test",
        is_strict=True,
        thresholds={"maker": {}, "taker": {}},
        blocking_sub_gates=("sharpe_threshold",),
    )


def _gate_d_passing_scorecard(equity_source: str | None) -> dict[str, Any]:
    """A scorecard that passes every Gate D check except equity_source."""
    payload: dict[str, Any] = {
        "sharpe_oos": 1.5,
        "max_drawdown": -0.05,
        "turnover": 0.5,
        "correlation_pool_max": 0.3,
        # Slice B (merged 2026-05-29): strict latency_audit now expects submit
        # and cancel P95 fields when latency_profile is a dict under a strict
        # validation profile.
        "latency_profile": {
            "latency_profile_id": "sim_p95_v2026-02-26",
            "submit_ack_latency_ms": 36.0,
            "cancel_ack_latency_ms": 47.0,
        },
        "replay_parity": {"match_pct": 99.0},
    }
    if equity_source is not None:
        payload["equity_source"] = equity_source
    return payload


def _make_promotion_config(**overrides: Any) -> PromotionConfig:
    base: dict[str, Any] = {
        "alpha_id": "TEST_ALPHA",
        "owner": "tester",
        "min_sharpe_oos": 1.0,
        "max_abs_drawdown": 0.2,
        "max_turnover": 2.0,
        "max_correlation": 0.7,
        "validation_profile": _strict_profile(),
    }
    base.update(overrides)
    return PromotionConfig(**base)


def test_gate_d_rejects_synthetic_equity() -> None:
    cfg = _make_promotion_config()
    scorecard = _gate_d_passing_scorecard(equity_source="synthetic")
    passed, checks = _evaluate_gate_d(scorecard, cfg)
    assert passed is False
    assert checks["equity_source"]["pass"] is False
    assert checks["equity_source"]["value"] == "synthetic"
    assert "synthetic" in checks["equity_source"]["detail"]


def test_gate_d_warns_on_missing_equity_source() -> None:
    """Legacy scorecards predating L6 lack equity_source — warn, do not block."""
    cfg = _make_promotion_config()
    scorecard = _gate_d_passing_scorecard(equity_source=None)
    passed, checks = _evaluate_gate_d(scorecard, cfg)
    # Missing is warn-only for back-compat; Gate D as a whole still passes.
    assert checks["equity_source"]["pass"] is True
    assert checks["equity_source"]["value"] is None
    assert "WARN" in checks["equity_source"]["detail"]
    assert passed is True


def test_gate_d_accepts_real_equity() -> None:
    cfg = _make_promotion_config()
    scorecard = _gate_d_passing_scorecard(equity_source="real")
    passed, checks = _evaluate_gate_d(scorecard, cfg)
    assert checks["equity_source"]["pass"] is True
    assert checks["equity_source"]["value"] == "real"
    # Gate D as a whole passes (every other check is set up to pass).
    assert passed is True


def test_gate_d_accepts_real_no_trade_equity() -> None:
    """No-fill day must NOT be rejected — it's a legitimate observation."""
    cfg = _make_promotion_config()
    scorecard = _gate_d_passing_scorecard(equity_source="real_no_trade")
    passed, checks = _evaluate_gate_d(scorecard, cfg)
    assert checks["equity_source"]["pass"] is True
    assert checks["equity_source"]["value"] == "real_no_trade"
    assert passed is True


def test_gate_d_skips_equity_check_when_disabled() -> None:
    cfg = _make_promotion_config(require_real_equity=False)
    scorecard = _gate_d_passing_scorecard(equity_source="synthetic")
    passed, checks = _evaluate_gate_d(scorecard, cfg)
    # equity_source check is not added when require_real_equity=False
    assert "equity_source" not in checks
    assert passed is True


def test_promote_alpha_rejects_screen_only_scorecard(tmp_path: Path) -> None:
    """``promote_alpha`` must refuse ``screen_only=true`` scorecards
    *before* any gate runs (the artifact came from `hft alpha screen`)."""
    scorecard = _gate_d_passing_scorecard(equity_source="real")
    scorecard["screen_only"] = True
    scorecard_path = tmp_path / "scorecard.json"
    scorecard_path.write_text(json.dumps(scorecard))

    cfg = _make_promotion_config(
        scorecard_path=str(scorecard_path),
        project_root=str(tmp_path),
    )
    with pytest.raises(PromotionError, match="cannot_promote_screen_artifact"):
        promote_alpha(cfg)


def test_promote_alpha_screen_only_guard_can_be_disabled(tmp_path: Path) -> None:
    """``reject_screen_only=False`` lets a screen artifact through (escape
    hatch for research; never used in production CLI)."""
    scorecard = _gate_d_passing_scorecard(equity_source="real")
    scorecard["screen_only"] = True
    scorecard_path = tmp_path / "scorecard.json"
    scorecard_path.write_text(json.dumps(scorecard))

    cfg = _make_promotion_config(
        scorecard_path=str(scorecard_path),
        project_root=str(tmp_path),
        reject_screen_only=False,
    )
    # With the screen-only guard disabled, ``cannot_promote_screen_artifact``
    # must NOT be raised. The promotion may then succeed or fail downstream
    # depending on Gate-C/D/E state — we only care that the screen guard
    # was bypassed. PromotionError is the only structured raiser; any other
    # exception type is a real bug and should propagate.
    try:
        promote_alpha(cfg)
    except PromotionError as exc:
        assert "cannot_promote_screen_artifact" not in str(exc), (
            "screen-only guard must be bypassed when reject_screen_only=False"
        )
