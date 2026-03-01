import json
from pathlib import Path

import pytest
import yaml

from hft_platform.alpha.promotion import (
    PromotionConfig,
    build_promotion_checklist,
    promote_alpha,
)


def _write_scorecard(
    path: Path,
    sharpe: float,
    max_drawdown: float,
    turnover: float,
    corr: float | None = 0.2,
    latency_profile: str | None = "sim_p95_v2026-02-26",
):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sharpe_oos": sharpe,
        "max_drawdown": max_drawdown,
        "turnover": turnover,
        "correlation_pool_max": corr,
        "latency_profile": latency_profile,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def test_promote_alpha_rejects_missing_correlation_metric(tmp_path: Path):
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.6, max_drawdown=-0.08, turnover=0.2, corr=None)

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
    assert not result.approved
    assert not result.gate_d_passed


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


# ---------------------------------------------------------------------------
# P3a: Config versioning tests
# ---------------------------------------------------------------------------


def test_promotion_config_config_version_default():
    """Default config_version is 'v1' and parent_config_version is None."""
    config = PromotionConfig(alpha_id="test_alpha", owner="bob")
    assert config.config_version == "v1"
    assert config.parent_config_version is None


def test_promotion_config_custom_version():
    """Custom config_version and parent_config_version can be set."""
    config = PromotionConfig(alpha_id="test_alpha", owner="bob", config_version="v2", parent_config_version="v1")
    assert config.config_version == "v2"
    assert config.parent_config_version == "v1"


def test_write_promotion_config_includes_versions(tmp_path: Path):
    """promote_alpha() writes config_version and parent_config_version into the YAML."""
    scorecard = tmp_path / "research" / "alphas" / "versioned_alpha" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.5, max_drawdown=-0.1, turnover=0.5)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="versioned_alpha",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
            config_version="v2",
            parent_config_version="v1",
        )
    )
    assert result.approved
    promo_path = Path(result.promotion_config_path)  # type: ignore[arg-type]
    payload = yaml.safe_load(promo_path.read_text())
    assert payload["config_version"] == "v2"
    assert payload["parent_config_version"] == "v1"


# ---------------------------------------------------------------------------
# P3b: PromotionChecklist tests
# ---------------------------------------------------------------------------


def _make_gate_d_checks(sharpe_pass: bool, dd_pass: bool, turnover_pass: bool, latency_pass: bool = True):
    return {
        "sharpe_oos": {"value": 1.5, "min": 1.0, "pass": sharpe_pass},
        "max_drawdown": {"value": -0.1, "min": -0.2, "pass": dd_pass},
        "turnover": {"value": 1.0, "max": 2.0, "pass": turnover_pass},
        "latency_profile": {"value": "sim_p95_v2026-02-26", "required": True, "pass": latency_pass, "detail": "OK"},
    }


def _make_gate_e_checks(shadow_pass: bool, drift_pass: bool, reject_pass: bool):
    return {
        "shadow_sessions": {"value": 6, "min": 5, "pass": shadow_pass},
        "drift_alerts": {"value": 0, "max": 0, "pass": drift_pass},
        "execution_reject_rate": {"value": 0.0, "max": 0.01, "pass": reject_pass},
    }


def test_build_checklist_all_pass():
    """When all gate checks pass, all_passed() is True and 7 items are returned."""
    config = PromotionConfig(alpha_id="test", owner="bob")
    gate_d = _make_gate_d_checks(True, True, True)
    gate_e = _make_gate_e_checks(True, True, True)
    checklist = build_promotion_checklist(config, gate_d, gate_e)
    assert len(checklist.items) == 7
    assert checklist.all_passed() is True


def test_build_checklist_partial_fail():
    """When one check fails, all_passed() is False."""
    config = PromotionConfig(alpha_id="test", owner="bob")
    gate_d = _make_gate_d_checks(True, True, True)
    gate_e = _make_gate_e_checks(False, True, True)  # shadow_sessions fails
    checklist = build_promotion_checklist(config, gate_d, gate_e)
    assert checklist.all_passed() is False


def test_promote_alpha_result_has_checklist(tmp_path: Path):
    """promote_alpha() result.checklist is not None and has 7 items."""
    scorecard = tmp_path / "research" / "alphas" / "chk_alpha" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.2, max_drawdown=-0.15, turnover=1.0)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="chk_alpha",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
        )
    )
    assert result.checklist is not None
    assert len(result.checklist.items) == 7


def test_promote_alpha_to_dict_includes_checklist(tmp_path: Path):
    """result.to_dict() contains a 'checklist' key."""
    scorecard = tmp_path / "research" / "alphas" / "dict_alpha" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.2, max_drawdown=-0.15, turnover=1.0)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="dict_alpha",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
        )
    )
    d = result.to_dict()
    assert "checklist" in d
    assert "items" in d["checklist"]
    assert len(d["checklist"]["items"]) == 7


def test_promote_alpha_requires_paper_trade_summary_when_governed(tmp_path: Path):
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.3, max_drawdown=-0.1, turnover=0.3, corr=0.2)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
            require_paper_trade_governance=True,
        )
    )
    assert not result.approved
    assert not result.gate_e_passed


def test_promote_alpha_paper_trade_summary_path_pass(tmp_path: Path):
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    summary_path = tmp_path / "paper_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "alpha_id": "ofi_mc",
                "session_count": 6,
                "distinct_trading_days": 5,
                "calendar_span_days": 7,
                "min_session_duration_seconds": 7200,
                "invalid_session_duration_count": 0,
                "drift_alerts_total": 0,
                "execution_reject_rate_mean": 0.001,
            }
        )
    )

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_path),
            min_paper_trade_session_minutes=30,
            enable_rust_readiness_gate=False,
        )
    )
    assert result.approved
    assert result.gate_e_passed


def test_promote_alpha_paper_trade_summary_fails_on_short_session(tmp_path: Path):
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    summary_path = tmp_path / "paper_summary_short.json"
    summary_path.write_text(
        json.dumps(
            {
                "alpha_id": "ofi_mc",
                "session_count": 6,
                "distinct_trading_days": 5,
                "calendar_span_days": 7,
                "min_session_duration_seconds": 1200,
                "invalid_session_duration_count": 0,
                "drift_alerts_total": 0,
                "execution_reject_rate_mean": 0.001,
            }
        )
    )

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_path),
            min_paper_trade_session_minutes=30,
            enable_rust_readiness_gate=False,
        )
    )
    assert not result.approved
    assert not result.gate_e_passed


def test_promote_alpha_rust_gate_fails_without_module(tmp_path: Path):
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
            enable_rust_readiness_gate=True,
        )
    )
    assert not result.approved
    assert not result.gate_f_passed


# ---------------------------------------------------------------------------
# P0-A: Gate E session duration default change (30 → 60 min)
# ---------------------------------------------------------------------------


def test_promotion_config_default_session_minutes_is_60():
    """Default min_paper_trade_session_minutes is 60 (not 30)."""
    config = PromotionConfig(alpha_id="test", owner="bob")
    assert config.min_paper_trade_session_minutes == 60


def test_gate_e_rejects_sub_60_minute_sessions_by_default(tmp_path: Path):
    """A 50-minute session fails Gate E under the default (60 min) threshold."""
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    summary_path = tmp_path / "short_session_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "alpha_id": "ofi_mc",
                "session_count": 6,
                "distinct_trading_days": 5,
                "calendar_span_days": 7,
                "min_session_duration_seconds": 3000,  # 50 minutes — below 60-min default
                "invalid_session_duration_count": 0,
                "drift_alerts_total": 0,
                "execution_reject_rate_mean": 0.001,
            }
        )
    )

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_path),
            enable_rust_readiness_gate=False,
            # min_paper_trade_session_minutes NOT specified → uses default=60
        )
    )
    assert not result.approved
    assert not result.gate_e_passed


def test_promote_alpha_rust_gate_passes_with_mocked_parity(monkeypatch, tmp_path: Path):
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    class _Proc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
            enable_rust_readiness_gate=True,
            rust_module_name="hft_platform.rust_core",
        )
    )
    assert result.approved
    assert result.gate_f_passed


# ---------------------------------------------------------------------------
# P1: Gate E prefers P95 reject rate over mean
# ---------------------------------------------------------------------------


def test_gate_e_uses_p95_reject_rate_from_summary(tmp_path: Path):
    """Gate E uses execution_reject_rate_p95 (not mean) when present in summary."""
    import json

    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    # Write paper trade summary with high P95 (0.05 > threshold 0.01) but low mean (0.001)
    summary_dir = tmp_path / "research" / "experiments" / "paper_trade" / "ofi_mc" / "sessions"
    summary_dir.mkdir(parents=True)
    session = {
        "alpha_id": "ofi_mc",
        "session_id": "abc",
        "started_at": "2026-02-01T09:00:00+00:00",
        "ended_at": "2026-02-08T16:00:00+00:00",
        "duration_seconds": 7 * 8 * 3600,
        "trading_day": "2026-02-01",
        "fills": 10,
        "pnl_bps": 1.5,
        "drift_alerts": 0,
        "execution_reject_rate": 0.001,   # mean is fine
        "reject_rate_p95": 0.05,          # P95 exceeds 0.01 threshold
        "notes": "",
    }
    (summary_dir / "2026-02-01_abc.json").write_text(json.dumps(session))

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            require_paper_trade_governance=True,
            paper_trade_summary_path=None,
            min_shadow_sessions=1,
            min_paper_trade_calendar_days=1,
            min_paper_trade_trading_days=1,
            max_execution_reject_rate=0.01,
        )
    )
    # P95 reject rate (0.05) > max_execution_reject_rate (0.01) → Gate E fails
    assert not result.gate_e_passed
    # Read gate_e_checks from the promotion decision report
    decision = json.loads(Path(result.promotion_decision_path).read_text())
    rej = decision["gate_e_checks"]["checks"]["execution_reject_rate"]
    assert rej.get("source") == "p95"
    assert rej.get("value") == pytest.approx(0.05)


def test_gate_e_falls_back_to_mean_when_p95_absent(tmp_path: Path):
    """Gate E uses mean reject rate when p95 is not in the summary."""
    import json

    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    summary_dir = tmp_path / "research" / "experiments" / "paper_trade" / "ofi_mc" / "sessions"
    summary_dir.mkdir(parents=True)
    session = {
        "alpha_id": "ofi_mc",
        "session_id": "def",
        "started_at": "2026-02-01T09:00:00+00:00",
        "ended_at": "2026-02-08T16:00:00+00:00",
        "duration_seconds": 7 * 8 * 3600,
        "trading_day": "2026-02-01",
        "fills": 10,
        "pnl_bps": 1.5,
        "drift_alerts": 0,
        "execution_reject_rate": 0.005,  # mean within threshold
        "reject_rate_p95": None,          # no P95 → fall back to mean
        "notes": "",
    }
    (summary_dir / "2026-02-01_def.json").write_text(json.dumps(session))

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            require_paper_trade_governance=True,
            paper_trade_summary_path=None,
            min_shadow_sessions=1,
            min_paper_trade_calendar_days=1,
            min_paper_trade_trading_days=1,
            max_execution_reject_rate=0.01,
        )
    )
    decision = json.loads(Path(result.promotion_decision_path).read_text())
    rej = decision["gate_e_checks"]["checks"]["execution_reject_rate"]
    assert rej.get("source") == "mean"
    assert rej.get("value") == pytest.approx(0.005)


# ---------------------------------------------------------------------------
# P4: feature_set_version mismatch warning in Gate D (warn-only)
# ---------------------------------------------------------------------------


def test_gate_d_feature_set_version_mismatch_blocks_gate_d(tmp_path: Path):
    """feature_set_version mismatch blocks Gate D (Q3: hardened from warn-only)."""
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
            manifest_feature_set_version="lob_shared_v0_old",  # mismatches live engine v1
        )
    )
    # Gate D fails with mismatch (blocking since Q3 hardening)
    assert not result.gate_d_passed
    assert not result.approved
    decision = json.loads(Path(result.promotion_decision_path).read_text())
    gate_d = decision.get("gate_d_passed", None)
    assert gate_d is False
    # The integration report should show the mismatch detail
    report = json.loads(Path(result.integration_report_path).read_text())
    fsv_check = report.get("checks", {}).get("feature_set_version", {})
    assert fsv_check.get("match") is False
    assert fsv_check.get("pass") is False


def test_gate_d_feature_set_version_match_passes(tmp_path: Path):
    """Matching feature_set_version produces no mismatch detail."""
    from hft_platform.feature.registry import FEATURE_SET_VERSION

    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
            manifest_feature_set_version=FEATURE_SET_VERSION,
        )
    )
    assert result.gate_d_passed
    decision = json.loads(Path(result.promotion_decision_path).read_text())
    # find feature_set_version check in integration report's gate_d checks
    report = json.loads(Path(result.integration_report_path).read_text())
    fsv_check = report.get("checks", {}).get("feature_set_version", {})
    assert fsv_check.get("match") is True
