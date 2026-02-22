import json
from pathlib import Path

import yaml

from hft_platform.alpha.promotion import PromotionConfig, promote_alpha


def _write_scorecard(path: Path, sharpe: float, max_drawdown: float, turnover: float, corr: float | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sharpe_oos": sharpe,
        "max_drawdown": max_drawdown,
        "turnover": turnover,
        "correlation_pool_max": corr,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def test_promote_alpha_approved_writes_config(tmp_path: Path):
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.6, max_drawdown=-0.08, turnover=0.2, corr=0.3)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
            drift_alerts=0,
            execution_reject_rate=0.0,
        )
    )
    assert result.approved
    assert result.promotion_config_path is not None
    promo_path = Path(result.promotion_config_path)
    assert promo_path.exists()
    payload = yaml.safe_load(promo_path.read_text())
    assert payload["alpha_id"] == "ofi_mc"
    assert payload["enabled"] is True
    assert payload["weight"] > 0


def test_promote_alpha_reject_without_force(tmp_path: Path):
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=0.2, max_drawdown=-0.4, turnover=5.0, corr=0.95)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=1,
            drift_alerts=2,
            execution_reject_rate=0.05,
        )
    )
    assert not result.approved
    assert result.promotion_config_path is None


def test_promote_alpha_force_override(tmp_path: Path):
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=0.1, max_drawdown=-0.6, turnover=7.0, corr=0.99)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=0,
            drift_alerts=5,
            execution_reject_rate=0.2,
            force=True,
        )
    )
    assert result.approved
    assert result.forced
    assert result.promotion_config_path is not None
