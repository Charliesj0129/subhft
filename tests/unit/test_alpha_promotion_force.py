"""Tests for I-02: force=True requires a non-empty force_reason."""

import json
from pathlib import Path

import pytest

from hft_platform.alpha._validation_profile import ValidationProfile
from hft_platform.alpha.promotion import PromotionConfig, promote_alpha


def _strict_profile() -> ValidationProfile:
    return ValidationProfile(name="test", is_strict=True, thresholds={}, blocking_sub_gates=("sharpe_threshold",))


def _write_bad_scorecard(path: Path) -> None:
    """Write a scorecard that will fail all promotion gates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "sharpe_oos": 0.1,
                "max_drawdown": -0.9,
                "turnover": 10.0,
                "correlation_pool_max": 0.99,
                "latency_profile": "sim_p95_v2026-02-26",
            }
        )
    )


def _base_config(tmp_path: Path, **kwargs) -> PromotionConfig:
    scorecard = tmp_path / "research" / "alphas" / "test_alpha" / "scorecard.json"
    _write_bad_scorecard(scorecard)
    return PromotionConfig(
        alpha_id="test_alpha",
        owner="test",
        project_root=str(tmp_path),
        scorecard_path=str(scorecard),
        validation_profile=_strict_profile(),
        **kwargs,
    )


def test_force_true_empty_reason_raises(tmp_path: Path) -> None:
    config = _base_config(tmp_path, force=True, force_reason="")
    with pytest.raises(ValueError, match="force_reason"):
        promote_alpha(config)


def test_force_true_whitespace_only_reason_raises(tmp_path: Path) -> None:
    config = _base_config(tmp_path, force=True, force_reason="   ")
    with pytest.raises(ValueError, match="force_reason"):
        promote_alpha(config)


def test_force_true_valid_reason_succeeds(tmp_path: Path) -> None:
    config = _base_config(tmp_path, force=True, force_reason="emergency prod deploy approved by CTO")
    result = promote_alpha(config)
    assert result.approved
    assert result.forced


def test_force_true_reason_appears_in_result_reasons(tmp_path: Path) -> None:
    reason_text = "post-incident hotfix, reviewed by risk team"
    config = _base_config(tmp_path, force=True, force_reason=reason_text)
    result = promote_alpha(config)
    assert any(reason_text in r for r in result.reasons)


def test_force_false_does_not_require_force_reason(tmp_path: Path) -> None:
    """force=False should work fine with no force_reason (default)."""
    config = _base_config(tmp_path, force=False)
    result = promote_alpha(config)
    # Gates will fail, but no ValueError should be raised
    assert not result.approved
    assert not result.forced
