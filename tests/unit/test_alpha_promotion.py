import json
from pathlib import Path

import pytest
import yaml

from hft_platform.alpha.promotion import (
    PromotionConfig,
    _evaluate_gate_d,
    build_promotion_checklist,
    promote_alpha,
)


def _strict_profile():
    from hft_platform.alpha._validation_profile import ValidationProfile

    return ValidationProfile(name="test", is_strict=True, thresholds={}, blocking_sub_gates=("sharpe_threshold",))


def _write_scorecard(
    path: Path,
    sharpe: float,
    max_drawdown: float,
    turnover: float,
    corr: float | None = 0.2,
    latency_profile: str | None = "sim_p95_v2026-02-26",
    replay_parity_match_pct: float | None = 100.0,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "sharpe_oos": sharpe,
        "max_drawdown": max_drawdown,
        "turnover": turnover,
        "correlation_pool_max": corr,
        "latency_profile": latency_profile,
    }
    if replay_parity_match_pct is not None:
        payload["replay_parity"] = {"match_pct": replay_parity_match_pct}
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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
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
            force_reason="test: override for unit test",
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
        )
    )
    d = result.to_dict()
    assert "checklist" in d
    assert "items" in d["checklist"]
    assert len(d["checklist"]["items"]) == 7


def test_promote_alpha_writes_paper_governance_artifact(tmp_path: Path):
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
                "min_session_duration_seconds": 3600,
                "invalid_session_duration_count": 0,
                "drift_alerts_total": 0,
                "execution_reject_rate_mean": 0.001,
                "execution_reject_rate_p95": 0.003,
                "regimes_covered": ["trending", "mean_reverting"],
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
            min_shadow_sessions=5,
            min_paper_trade_calendar_days=7,
            min_paper_trade_trading_days=5,
            validation_profile=_strict_profile(),
        )
    )
    assert result.paper_governance_report_path is not None
    governance_path = Path(result.paper_governance_report_path)
    assert governance_path.exists()
    governance = json.loads(governance_path.read_text())
    assert governance["passed"] is True
    assert governance["checks"]["execution_reject_rate"]["source"] == "p95"
    assert governance["checks"]["regime_span"]["pass"] is True

    decision = json.loads(Path(result.promotion_decision_path).read_text())
    assert decision["paper_governance_report_path"] == str(governance_path)
    assert decision["paper_governance_passed"] is True
    assert result.to_dict()["paper_governance_report_path"] == str(governance_path)


def test_promote_alpha_writes_paper_governance_artifact_when_summary_missing(tmp_path: Path):
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            require_paper_trade_governance=True,
            validation_profile=_strict_profile(),
        )
    )
    assert not result.approved
    assert result.paper_governance_report_path is not None

    governance_path = Path(result.paper_governance_report_path)
    governance = json.loads(governance_path.read_text())
    assert governance["passed"] is False
    assert governance["summary"] is None
    assert governance["paper_trade_summary_source"] == "tracker"
    assert governance["paper_trade_summary_error"] == "paper_trade_sessions_missing"

    decision = json.loads(Path(result.promotion_decision_path).read_text())
    assert decision["paper_governance_report_path"] == str(governance_path)
    assert decision["paper_governance_passed"] is False


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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
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
        "execution_reject_rate": 0.001,  # mean is fine
        "reject_rate_p95": 0.05,  # P95 exceeds 0.01 threshold
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
            validation_profile=_strict_profile(),
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
        "reject_rate_p95": None,  # no P95 → fall back to mean
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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
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
            validation_profile=_strict_profile(),
        )
    )
    assert result.gate_d_passed
    decision = json.loads(Path(result.promotion_decision_path).read_text())
    # find feature_set_version check in integration report's gate_d checks
    report = json.loads(Path(result.integration_report_path).read_text())
    fsv_check = report.get("checks", {}).get("feature_set_version", {})
    assert fsv_check.get("match") is True


# ---------------------------------------------------------------------------
# Additional coverage: uncovered branches in promote_alpha
# ---------------------------------------------------------------------------


def test_promote_alpha_raises_file_not_found_for_missing_scorecard(tmp_path: Path):
    """promote_alpha raises FileNotFoundError when scorecard_path does not exist."""
    missing = str(tmp_path / "no_such_scorecard.json")
    with pytest.raises(FileNotFoundError, match="scorecard not found"):
        promote_alpha(
            PromotionConfig(
                alpha_id="ofi_mc",
                owner="charlie",
                project_root=str(tmp_path),
                scorecard_path=missing,
                validation_profile=_strict_profile(),
            )
        )


def test_promote_alpha_write_promotion_config_disabled_adds_reason(tmp_path: Path):
    """approved + write_promotion_config=False appends research-only reason."""
    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.6, max_drawdown=-0.08, turnover=0.2, corr=0.3)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
            write_promotion_config=False,
            validation_profile=_strict_profile(),
        )
    )
    assert result.approved
    assert result.promotion_config_path is None
    assert any("research-only" in r for r in result.reasons)


def test_promote_alpha_explicit_relative_scorecard_path(tmp_path: Path):
    """scorecard_path as a relative path is resolved against project_root."""
    # Create scorecard at a custom relative location
    scorecard_dir = tmp_path / "custom" / "sc"
    scorecard_dir.mkdir(parents=True)
    scorecard_file = scorecard_dir / "scorecard.json"
    _write_scorecard(scorecard_file, sharpe=1.5, max_drawdown=-0.1, turnover=0.5, corr=0.3)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            scorecard_path="custom/sc/scorecard.json",
            shadow_sessions=6,
            validation_profile=_strict_profile(),
        )
    )
    assert result.gate_d_passed


def test_promote_alpha_rust_benchmark_gate_passes(monkeypatch, tmp_path: Path):
    """enforce_rust_benchmark_gate=True runs benchmark cmd and passes if returncode=0."""
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
            enforce_rust_benchmark_gate=True,
            validation_profile=_strict_profile(),
        )
    )
    assert result.gate_f_passed


def test_promote_alpha_rust_parity_timeout(monkeypatch, tmp_path: Path):
    """Parity subprocess timeout is handled gracefully (gate F fails, no crash)."""
    import subprocess

    scorecard = tmp_path / "research" / "alphas" / "ofi_mc" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.4, max_drawdown=-0.1, turnover=0.2, corr=0.2)

    def _raise_timeout(*a, **k):
        exc = subprocess.TimeoutExpired(cmd=["uv"], timeout=1)
        exc.stdout = b""
        exc.stderr = b""
        raise exc

    monkeypatch.setattr("subprocess.run", _raise_timeout)

    result = promote_alpha(
        PromotionConfig(
            alpha_id="ofi_mc",
            owner="charlie",
            project_root=str(tmp_path),
            shadow_sessions=6,
            enable_rust_readiness_gate=True,
            rust_module_name="hft_platform.rust_core",
            validation_profile=_strict_profile(),
        )
    )
    assert not result.gate_f_passed


def test_build_checklist_with_paper_governance_adds_extra_items(tmp_path: Path):
    """require_paper_trade_governance=True adds 4 extra items to checklist (11 total)."""
    scorecard = tmp_path / "research" / "alphas" / "pg_alpha" / "scorecard.json"
    _write_scorecard(scorecard, sharpe=1.5, max_drawdown=-0.1, turnover=0.5, corr=0.3)

    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "alpha_id": "pg_alpha",
                "session_count": 6,
                "distinct_trading_days": 5,
                "calendar_span_days": 7,
                "min_session_duration_seconds": 7200,
                "invalid_session_duration_count": 0,
                "drift_alerts_total": 0,
                "execution_reject_rate_mean": 0.001,
                "regimes_covered": ["trending", "mean_reverting"],
            }
        )
    )

    result = promote_alpha(
        PromotionConfig(
            alpha_id="pg_alpha",
            owner="charlie",
            project_root=str(tmp_path),
            require_paper_trade_governance=True,
            paper_trade_summary_path=str(summary_path),
            min_paper_trade_session_minutes=30,
            validation_profile=_strict_profile(),
        )
    )
    assert result.checklist is not None
    # Base 7 items + 4 paper-trade items = 11
    assert len(result.checklist.items) == 11
    assert result.checklist.all_passed() is True


class TestStrictProfileRequirement:
    def test_promotion_requires_strict_profile(self, tmp_path: Path) -> None:
        from hft_platform.alpha.promotion import (
            PromotionConfig,
            PromotionError,
            promote_alpha,
        )

        config = PromotionConfig(
            alpha_id="test_alpha",
            owner="test",
            project_root=str(tmp_path),
            scorecard_path=None,
            validation_profile=None,
        )
        with pytest.raises(PromotionError, match="strict profile required"):
            promote_alpha(config)

    def test_promotion_accepts_strict_profile(self, tmp_path: Path) -> None:
        from hft_platform.alpha._validation_profile import ValidationProfile
        from hft_platform.alpha.promotion import (
            PromotionConfig,
            promote_alpha,
        )

        prof = ValidationProfile(
            name="vm_ul6_strict",
            is_strict=True,
            thresholds={},
            blocking_sub_gates=("sharpe_threshold",),
        )
        config = PromotionConfig(
            alpha_id="test_alpha",
            owner="test",
            project_root=str(tmp_path),
            scorecard_path=None,
            validation_profile=prof,
        )
        try:
            promote_alpha(config)
        except Exception as exc:
            assert "strict profile required" not in str(exc), exc


# ---------------------------------------------------------------------------
# Slice C Task 10: Gate D replay_parity_audit check
# ---------------------------------------------------------------------------


def _gate_d_scorecard_with_parity(match_pct: float | None) -> dict:
    """Build a scorecard dict that satisfies all other Gate D required fields,
    so the replay_parity_audit is the focus of the assertion."""
    payload: dict = {
        "sharpe_oos": 1.6,
        "max_drawdown": -0.08,
        "turnover": 0.2,
        "correlation_pool_max": 0.2,
        "latency_profile": "sim_p95_v2026-02-26",
    }
    if match_pct is not None:
        payload["replay_parity"] = {"match_pct": match_pct}
    return payload


def test_promotion_blocks_when_replay_parity_below_threshold():
    scorecard = _gate_d_scorecard_with_parity(match_pct=80.0)
    config = PromotionConfig(
        alpha_id="ofi_mc",
        owner="charlie",
        min_replay_parity_match_pct=95.0,
    )

    passed, checks = _evaluate_gate_d(scorecard, config)

    assert checks["replay_parity_audit"]["pass"] is False
    assert checks["replay_parity_audit"]["value"] == 80.0
    assert checks["replay_parity_audit"]["min"] == 95.0
    assert passed is False


def test_promotion_passes_when_replay_parity_at_or_above_threshold():
    scorecard = _gate_d_scorecard_with_parity(match_pct=96.0)
    config = PromotionConfig(
        alpha_id="ofi_mc",
        owner="charlie",
        min_replay_parity_match_pct=95.0,
    )

    _passed, checks = _evaluate_gate_d(scorecard, config)

    assert checks["replay_parity_audit"]["pass"] is True
    assert checks["replay_parity_audit"]["value"] == 96.0


# ---------------------------------------------------------------------------
# Slice-D T14: promote_alpha auto-kill on Gate-C raise + Gate-D rejection
# ---------------------------------------------------------------------------


class TestSliceDAutoKill:
    """Auto-kill ledger writes on Gate-C raise and Gate-D rejection paths.

    These tests exercise ``promote_alpha`` end-to-end with a synthetic
    scorecard + meta.json + manifest.yaml fixture and assert that the
    Slice-D kill-ledger jsonl sink picks up exactly one row per
    failed-promotion attempt (idempotent on re-run).
    """

    @staticmethod
    def _strict_profile():
        from hft_platform.alpha._validation_profile import ValidationProfile

        return ValidationProfile(
            name="test",
            is_strict=True,
            thresholds={},
            blocking_sub_gates=("sharpe_threshold",),
        )

    @staticmethod
    def _setup_alpha_fixture(
        tmp_path: Path,
        alpha_id: str,
        *,
        gate_c_passed: bool,
        sharpe: float = 1.6,
    ) -> tuple[Path, Path]:
        """Lay down ``research/alphas/<alpha_id>/`` with scorecard, meta, manifest.

        Returns ``(project_root, scorecard_path)``.
        """
        alpha_dir = tmp_path / "research" / "alphas" / alpha_id
        alpha_dir.mkdir(parents=True, exist_ok=True)

        # Scorecard tuned to pass Gate D when sharpe >= 1.0.
        sc_path = alpha_dir / "scorecard.json"
        _write_scorecard(sc_path, sharpe=sharpe, max_drawdown=-0.05, turnover=0.5, corr=0.3)

        # meta.json governs Gate-C verification.
        (alpha_dir / "meta.json").write_text(
            json.dumps({"gate_status": {"gate_c": gate_c_passed}})
        )

        # Minimal manifest so stable_artifact_hash is computable.
        (alpha_dir / "manifest.yaml").write_text(
            yaml.safe_dump(
                {
                    "alpha_id": alpha_id,
                    "hypothesis": "synthetic test alpha",
                    "formula": "x",
                    "paper_refs": ["1234.5678"],
                    "data_fields": ["feature[0]"],
                    "complexity": "O(1)",
                    "status": "draft",
                }
            )
        )
        return tmp_path, sc_path

    @pytest.fixture(autouse=True)
    def _isolate_kill_ledger(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> Path:
        from hft_platform.alpha import audit, kill_ledger

        jsonl = tmp_path / "_kill_ledger.jsonl"
        monkeypatch.setenv("HFT_ALPHA_KILL_LEDGER_PATH", str(jsonl))
        # Force jsonl path: keep CH disabled so the ledger writes land in
        # the file we can read back from disk.
        monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "0")
        audit._ENABLED = None  # noqa: SLF001 -- re-read env on next call
        kill_ledger._reset_cache_for_tests()
        return jsonl

    @staticmethod
    def _read_ledger(path: Path) -> list[dict]:
        if not path.exists():
            return []
        rows: list[dict] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
        return rows

    # -- Gate C raise path -----------------------------------------------

    def test_gate_c_failure_writes_kill_ledger_row(
        self,
        tmp_path: Path,
        _isolate_kill_ledger: Path,
    ) -> None:
        root, sc_path = self._setup_alpha_fixture(
            tmp_path, "alpha_t14_c", gate_c_passed=False
        )

        cfg = PromotionConfig(
            alpha_id="alpha_t14_c",
            owner="charlie",
            project_root=str(root),
            scorecard_path=str(sc_path),
            validation_profile=self._strict_profile(),
        )
        with pytest.raises(ValueError, match="Gate C has not passed"):
            promote_alpha(cfg)

        rows = self._read_ledger(_isolate_kill_ledger)
        assert len(rows) == 1, f"expected exactly 1 ledger row, got {rows}"
        assert rows[0]["alpha_id"] == "alpha_t14_c"
        assert rows[0]["gate"] == "C"
        assert "Gate C has not passed" in rows[0]["reason"]
        assert rows[0]["killed_by"] == "promote_alpha:auto"

    def test_gate_c_failure_re_run_is_idempotent(
        self,
        tmp_path: Path,
        _isolate_kill_ledger: Path,
    ) -> None:
        root, sc_path = self._setup_alpha_fixture(
            tmp_path, "alpha_t14_c_idem", gate_c_passed=False
        )
        cfg = PromotionConfig(
            alpha_id="alpha_t14_c_idem",
            owner="charlie",
            project_root=str(root),
            scorecard_path=str(sc_path),
            validation_profile=self._strict_profile(),
        )

        for _ in range(2):
            with pytest.raises(ValueError, match="Gate C has not passed"):
                promote_alpha(cfg)

        rows = self._read_ledger(_isolate_kill_ledger)
        assert len(rows) == 1, f"expected idempotent single row, got {rows}"

    def test_gate_c_failure_does_not_kill_when_env_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_kill_ledger: Path,
    ) -> None:
        monkeypatch.setenv("HFT_KILL_LEDGER_ENABLED", "0")
        root, sc_path = self._setup_alpha_fixture(
            tmp_path, "alpha_t14_c_off", gate_c_passed=False
        )
        cfg = PromotionConfig(
            alpha_id="alpha_t14_c_off",
            owner="charlie",
            project_root=str(root),
            scorecard_path=str(sc_path),
            validation_profile=self._strict_profile(),
        )
        with pytest.raises(ValueError, match="Gate C has not passed"):
            promote_alpha(cfg)

        rows = self._read_ledger(_isolate_kill_ledger)
        assert rows == [], f"ledger must be empty when env off, got {rows}"

    def test_gate_c_kill_re_raises_original_value_error(
        self,
        tmp_path: Path,
        _isolate_kill_ledger: Path,
    ) -> None:
        """The auto-kill side effect must NOT swallow the ValueError nor mutate its message."""
        root, sc_path = self._setup_alpha_fixture(
            tmp_path, "alpha_t14_c_msg", gate_c_passed=False
        )
        cfg = PromotionConfig(
            alpha_id="alpha_t14_c_msg",
            owner="charlie",
            project_root=str(root),
            scorecard_path=str(sc_path),
            validation_profile=self._strict_profile(),
        )
        with pytest.raises(ValueError) as exc_info:
            promote_alpha(cfg)

        # Original message preserved verbatim from _verify_gate_c_passed.
        assert "Gate C has not passed for this scorecard" in str(exc_info.value)
        assert str(sc_path) in str(exc_info.value)

    # -- Gate D rejection path -------------------------------------------

    def test_gate_d_rejection_writes_kill_ledger_row(
        self,
        tmp_path: Path,
        _isolate_kill_ledger: Path,
    ) -> None:
        # sharpe=0.2 < min_sharpe_oos=1.0 -> Gate D fails.
        root, sc_path = self._setup_alpha_fixture(
            tmp_path, "alpha_t14_d", gate_c_passed=True, sharpe=0.2
        )
        cfg = PromotionConfig(
            alpha_id="alpha_t14_d",
            owner="charlie",
            project_root=str(root),
            scorecard_path=str(sc_path),
            validation_profile=self._strict_profile(),
        )
        result = promote_alpha(cfg)
        assert result.approved is False
        assert result.gate_d_passed is False

        rows = self._read_ledger(_isolate_kill_ledger)
        assert len(rows) == 1, f"expected exactly 1 ledger row, got {rows}"
        assert rows[0]["alpha_id"] == "alpha_t14_d"
        assert rows[0]["gate"] == "D"
        # Reason should at least mention the failed sharpe_oos gate.
        assert "sharpe_oos" in rows[0]["reason"]
        assert rows[0]["killed_by"] == "promote_alpha:auto"

    def test_gate_d_rejection_re_run_is_idempotent(
        self,
        tmp_path: Path,
        _isolate_kill_ledger: Path,
    ) -> None:
        root, sc_path = self._setup_alpha_fixture(
            tmp_path, "alpha_t14_d_idem", gate_c_passed=True, sharpe=0.2
        )
        cfg = PromotionConfig(
            alpha_id="alpha_t14_d_idem",
            owner="charlie",
            project_root=str(root),
            scorecard_path=str(sc_path),
            validation_profile=self._strict_profile(),
        )
        for _ in range(2):
            result = promote_alpha(cfg)
            assert result.approved is False

        rows = self._read_ledger(_isolate_kill_ledger)
        assert len(rows) == 1, f"expected idempotent single row, got {rows}"

    def test_gate_d_rejection_does_not_kill_when_env_off(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _isolate_kill_ledger: Path,
    ) -> None:
        monkeypatch.setenv("HFT_KILL_LEDGER_ENABLED", "0")
        root, sc_path = self._setup_alpha_fixture(
            tmp_path, "alpha_t14_d_off", gate_c_passed=True, sharpe=0.2
        )
        cfg = PromotionConfig(
            alpha_id="alpha_t14_d_off",
            owner="charlie",
            project_root=str(root),
            scorecard_path=str(sc_path),
            validation_profile=self._strict_profile(),
        )
        result = promote_alpha(cfg)
        assert result.approved is False

        rows = self._read_ledger(_isolate_kill_ledger)
        assert rows == [], f"ledger must be empty when env off, got {rows}"
