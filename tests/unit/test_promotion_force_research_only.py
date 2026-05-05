"""Tests for L9 (loop_v1): forced promotions are research-only.

`promote_alpha(force=True, force_reason=...)` MUST write its YAML artifact
to ``research/forced_promotions/<date>/<id>.yaml`` rather than
``config/strategy_promotions/<date>/<id>.yaml``. The artifact must stamp
``live_promotion_eligible: false`` and ``enabled: false`` so a downstream
consumer cannot accidentally treat it as live-eligible.

CI separately guards ``config/live/**`` against any reference to
``research/forced_promotions/`` or ``forced: true``.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from hft_platform.alpha._validation_profile import ValidationProfile
from hft_platform.alpha.promotion import PromotionConfig, promote_alpha


def _strict_profile() -> ValidationProfile:
    return ValidationProfile(name="test", is_strict=True, thresholds={}, blocking_sub_gates=("sharpe_threshold",))


def _write_bad_scorecard(path: Path) -> None:
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


def _config(tmp_path: Path, **kwargs: object) -> PromotionConfig:
    scorecard = tmp_path / "research" / "alphas" / "test_alpha" / "scorecard.json"
    _write_bad_scorecard(scorecard)
    return PromotionConfig(
        alpha_id="test_alpha",
        owner="test",
        project_root=str(tmp_path),
        scorecard_path=str(scorecard),
        validation_profile=_strict_profile(),
        **kwargs,  # type: ignore[arg-type]
    )


class TestForcedPromotionResearchOnly:
    def test_forced_artifact_lives_under_research_forced_promotions(self, tmp_path: Path) -> None:
        config = _config(
            tmp_path,
            force=True,
            force_reason="post-incident hotfix approved by CTO",
        )
        result = promote_alpha(config)
        assert result.approved
        assert result.forced
        assert result.promotion_config_path is not None

        out = Path(result.promotion_config_path)
        assert "research/forced_promotions" in out.as_posix()
        assert "config/strategy_promotions" not in out.as_posix()
        assert out.exists()

    def test_forced_artifact_stamps_live_promotion_eligible_false(self, tmp_path: Path) -> None:
        config = _config(tmp_path, force=True, force_reason="emergency override, signed off by risk")
        result = promote_alpha(config)
        assert result.promotion_config_path is not None

        payload = yaml.safe_load(Path(result.promotion_config_path).read_text())
        assert payload["forced"] is True
        assert payload["live_promotion_eligible"] is False
        # Even though promote_alpha returned approved=True via override,
        # the on-disk artifact must NOT advertise itself as live-eligible.
        assert payload["enabled"] is False
        assert payload["weight"] == 0.0

    def test_non_forced_failed_promotion_writes_no_artifact(self, tmp_path: Path) -> None:
        # Sanity check: a NON-forced run with failing gates does not write a
        # promotion config at all (existing behavior preserved).
        config = _config(tmp_path, force=False)
        result = promote_alpha(config)
        assert not result.approved
        assert not result.forced
        assert result.promotion_config_path is None

    def test_forced_artifacts_are_isolated_per_alpha_id(self, tmp_path: Path) -> None:
        sc_a = tmp_path / "research" / "alphas" / "alpha_a" / "scorecard.json"
        _write_bad_scorecard(sc_a)
        cfg_a = PromotionConfig(
            alpha_id="alpha_a",
            owner="test",
            project_root=str(tmp_path),
            scorecard_path=str(sc_a),
            validation_profile=_strict_profile(),
            force=True,
            force_reason="reason A",
        )
        result_a = promote_alpha(cfg_a)
        assert result_a.promotion_config_path is not None
        path_a = Path(result_a.promotion_config_path)

        sc_b = tmp_path / "research" / "alphas" / "alpha_b" / "scorecard.json"
        _write_bad_scorecard(sc_b)
        cfg_b = PromotionConfig(
            alpha_id="alpha_b",
            owner="test",
            project_root=str(tmp_path),
            scorecard_path=str(sc_b),
            validation_profile=_strict_profile(),
            force=True,
            force_reason="reason B",
        )
        result_b = promote_alpha(cfg_b)
        assert result_b.promotion_config_path is not None
        path_b = Path(result_b.promotion_config_path)

        assert path_a != path_b
        assert path_a.parent == path_b.parent
        assert "research/forced_promotions" in path_a.as_posix()
        assert "research/forced_promotions" in path_b.as_posix()
