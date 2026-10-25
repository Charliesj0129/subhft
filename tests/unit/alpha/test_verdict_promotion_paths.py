"""Only a KILL verdict writes a kill, and only a PASS verdict enters promotion.

Covers the Gate D verdict in ``promote_alpha``, the governed pipeline's
promotion step, and Gate C's maker protocol check.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

import research.pipeline as pipeline
from hft_platform.alpha import audit, kill_ledger
from hft_platform.alpha._gate_c import run_gate_c
from hft_platform.alpha._validation_profile import ValidationProfile
from hft_platform.alpha._validation_types import ValidationConfig
from hft_platform.alpha.promotion import PromotionConfig, _evaluate_gate_d_verdict, promote_alpha


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "_kill_ledger.jsonl"
    monkeypatch.setenv("HFT_ALPHA_KILL_LEDGER_PATH", str(path))
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "0")
    audit._ENABLED = None  # noqa: SLF001 -- re-read env on next call
    kill_ledger._reset_cache_for_tests()
    return path


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _scorecard(**overrides) -> dict:
    payload = {
        "sharpe_oos": 1.6,
        "max_drawdown": -0.05,
        "turnover": 0.5,
        "correlation_pool_max": 0.3,
        "latency_profile": "sim_p95_v2026-02-26",
        "replay_parity": {"match_pct": 99.0},
        "strategy_type": "taker",
        "cost_uncertainty": {"passed": True, "metrics": {"p95_lower_bound_pts": 0.5}},
    }
    payload.update(overrides)
    return payload


def _promote(tmp_path: Path, scorecard: dict) -> object:
    alpha_dir = tmp_path / "research" / "alphas" / "alpha_v"
    alpha_dir.mkdir(parents=True)
    sc_path = alpha_dir / "scorecard.json"
    sc_path.write_text(json.dumps(scorecard))
    (alpha_dir / "meta.json").write_text(json.dumps({"run_id": "run-v", "gate_status": {"gate_c": True}}))
    profile = ValidationProfile(name="t", is_strict=True, blocking_sub_gates=("sharpe_threshold",))
    return promote_alpha(
        PromotionConfig(
            alpha_id="alpha_v",
            owner="charlie",
            project_root=str(tmp_path),
            scorecard_path=str(sc_path),
            min_sharpe_oos=1.0,
            validation_profile=profile,
        )
    )


def test_gate_d_missing_scorecard_field_is_invalid_and_writes_no_kill(tmp_path: Path, ledger: Path):
    result = _promote(tmp_path, _scorecard(correlation_pool_max=None))
    assert result.gate_d_passed is False
    assert _rows(ledger) == []
    decision = json.loads(Path(result.promotion_decision_path).read_text())
    assert decision["gate_d_verdict"] == "INVALID"


def test_gate_d_missing_replay_parity_is_error_not_fail():
    scorecard = _scorecard()
    scorecard.pop("replay_parity")
    verdict, checks = _evaluate_gate_d_verdict(scorecard, PromotionConfig(alpha_id="a", owner="o"))
    assert checks["replay_parity_audit"]["outcome"] == "error"
    assert verdict.verdict.value == "INVALID"


def test_gate_d_measured_failure_writes_kill_with_run_id(tmp_path: Path, ledger: Path):
    result = _promote(tmp_path, _scorecard(sharpe_oos=0.2))
    assert result.gate_d_passed is False
    rows = _rows(ledger)
    assert len(rows) == 1
    assert rows[0]["gate"] == "D"
    assert rows[0]["reason"] == "sharpe_oos"
    assert rows[0]["run_id"] == "run-v"


def test_pipeline_skips_promotion_when_validation_did_not_pass(monkeypatch, tmp_path: Path):
    def _no_promotion(_config):
        raise AssertionError("promote_alpha must not run on a non-PASS validation")

    validation = types.SimpleNamespace(passed=False, scorecard_path="", to_dict=lambda: {"passed": False})
    monkeypatch.setattr(pipeline, "_run_factory_optimize", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "_run_factory_index", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "_apply_validation_profile", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "run_alpha_validation", lambda _cfg: validation)
    monkeypatch.setattr(pipeline, "promote_alpha", _no_promotion)
    args = pipeline.build_parser().parse_args(
        ["run", "--alpha-id", "alpha_v", "--owner", "o", "--data", "d.npy", "--out-dir", str(tmp_path)]
    )

    rc = pipeline._run_pipeline(args, mode="strict")

    assert rc != 0
    report = json.loads(next(tmp_path.glob("*_pipeline_report.json")).read_text())
    assert report["promotion_report"] is None
    assert any("did not reach a PASS verdict" in note for note in report["notes"])


def test_maker_without_strategy_protocol_is_error_report_not_attribute_error(tmp_path: Path):
    def _no_clickhouse():
        raise AssertionError("protocol check must run before any data access")

    maker_runtime = types.SimpleNamespace(
        load_cost_profile=None,
        queue_depletion_fill=None,
        clickhouse_source=_no_clickhouse,
        latency_profile=None,
        maker_engine=None,
        result_store=None,
    )
    runtime = types.SimpleNamespace(
        hft_native_runner=None,
        ensure_hftbt_npz=None,
        backtest_config=None,
        walk_forward_config=None,
        compute_scorecard=None,
        load_maker_runtime=lambda: maker_runtime,
    )
    alpha = types.SimpleNamespace(
        manifest=types.SimpleNamespace(alpha_id="maker_x", strategy_type="maker", instrument="TMF"),
    )

    report, run_id, *_ = run_gate_c(
        alpha, ValidationConfig(alpha_id="maker_x", data_paths=[]), tmp_path, [], tmp_path, runtime=runtime
    )

    assert report.passed is False
    assert report.details["verdict"]["verdict"] == "INVALID"
    assert "on_tick" in report.details["error"]
    assert run_id == ""


@pytest.mark.parametrize(
    ("override", "check"),
    [
        ({"replay_parity": {"match_pct": 50.0}}, "replay_parity_audit"),
        ({"equity_source": "synthetic"}, "equity_source"),
    ],
)
def test_gate_d_failed_evidence_audit_is_error_and_writes_no_kill(tmp_path: Path, ledger: Path, override, check):
    verdict, checks = _evaluate_gate_d_verdict(
        _scorecard(**override), PromotionConfig(alpha_id="a", owner="o", min_sharpe_oos=1.0)
    )
    assert checks[check]["outcome"] == "error"
    assert verdict.verdict.value == "INVALID"
    result = _promote(tmp_path, _scorecard(**override))
    assert result.gate_d_passed is False
    assert _rows(ledger) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"passed": False, "outcome": "pass", "metrics": {"p95_lower_bound_pts": 0.5}},
        {"passed": True, "outcome": "fail", "metrics": {"p95_lower_bound_pts": 0.5}},
        {"outcome": "bogus", "metrics": {"p95_lower_bound_pts": 0.5}},
        {"metrics": {"p95_lower_bound_pts": 0.5}},
    ],
    ids=["failed_flag_pass_outcome", "passed_flag_fail_outcome", "unknown_outcome", "neither"],
)
def test_gate_d_contradictory_audit_payload_is_error_and_not_approved(tmp_path: Path, ledger: Path, payload):
    verdict, checks = _evaluate_gate_d_verdict(
        _scorecard(cost_uncertainty=payload), PromotionConfig(alpha_id="a", owner="o", min_sharpe_oos=1.0)
    )
    assert checks["cost_uncertainty_audit"]["outcome"] == "error"
    assert verdict.verdict.value == "INVALID"
    result = _promote(tmp_path, _scorecard(cost_uncertainty=payload))
    assert result.approved is False
    assert _rows(ledger) == []


def _stored_run(tmp_path: Path, verdict: str) -> Path:
    run_dir = tmp_path / "runs" / "run-k"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"run_id": "run-k", "gate_status": {"gate_c": False}}))
    (run_dir / "backtest_report.json").write_text(
        json.dumps(
            {
                "details": {
                    "verdict": {"verdict": verdict, "reasons": ["gate_c.sharpe_oos"], "failing": ["gate_c.sharpe_oos"]}
                }
            }
        )
    )
    sc_path = run_dir / "scorecard.json"
    sc_path.write_text("{}")
    return sc_path


@pytest.mark.parametrize(("verdict", "n_rows"), [("KILL", 1), ("NEEDS-MORE-DAYS", 0), ("INVALID", 0)])
def test_pipeline_skipping_promotion_still_records_a_stored_kill(monkeypatch, tmp_path, ledger, verdict, n_rows):
    sc_path = _stored_run(tmp_path, verdict)
    validation = types.SimpleNamespace(passed=False, scorecard_path=str(sc_path), to_dict=lambda: {"passed": False})
    monkeypatch.setattr(pipeline, "_run_factory_optimize", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "_run_factory_index", lambda *a, **k: 0)
    monkeypatch.setattr(pipeline, "_apply_validation_profile", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "run_alpha_validation", lambda _cfg: validation)
    args = pipeline.build_parser().parse_args(
        ["run", "--alpha-id", "alpha_k", "--owner", "o", "--data", "d.npy", "--out-dir", str(tmp_path / "out")]
    )

    pipeline._run_pipeline(args, mode="strict")
    pipeline._run_pipeline(args, mode="strict")

    rows = _rows(ledger)
    assert len(rows) == n_rows
    if n_rows:
        assert rows[0]["gate"] == "C"
        assert rows[0]["run_id"] == "run-k"
