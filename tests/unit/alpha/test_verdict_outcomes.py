"""Verdict rules v1: explicit gate outcomes and the single derive_verdict path.

Covers ``alpha.outcomes``, the sub-gates that used to pass by default, the
Gate C aggregate built in ``_invoke_sub_gates``, the research decision stored
from it, and ``KillRecord.from_verdict``. Scenarios S1-S3 use fixed-seed
synthetic daily PnL with a known effect size.
"""

from __future__ import annotations

import numpy as np
import pytest

from hft_platform.alpha._gate_c import _invoke_sub_gates, _invoke_sub_gates_with_verdict
from hft_platform.alpha._sub_gates import UnavailableDailyPnl
from hft_platform.alpha._sub_gates.common import MaxDrawdownGate
from hft_platform.alpha._sub_gates.min_sample_size import MinSampleSizeGate
from hft_platform.alpha._sub_gates.registry import SubGateResult
from hft_platform.alpha._validation_profile import ValidationProfile
from hft_platform.alpha.experiments import derive_research_decision_from_gate_c_report
from hft_platform.alpha.kill_ledger import KillRecord, VerdictNotKillError
from hft_platform.alpha.outcomes import (
    Outcome,
    Verdict,
    VerdictResult,
    check_entry,
    derive_verdict,
    resolve_outcome,
)

# ---------------------------------------------------------------------------
# alpha.outcomes
# ---------------------------------------------------------------------------


def _e(name: str, outcome: Outcome, **metrics) -> dict:
    return check_entry(name, outcome, metrics=metrics)


def test_resolve_outcome_derives_from_passed_when_absent():
    assert resolve_outcome(True, None) is Outcome.PASS
    assert resolve_outcome(False, None) is Outcome.FAIL
    assert resolve_outcome(None, None) is Outcome.ERROR


@pytest.mark.parametrize(
    ("passed", "outcome"),
    [(False, Outcome.PASS), (True, Outcome.FAIL), (True, Outcome.ERROR), (True, Outcome.INSUFFICIENT_SAMPLE)],
)
def test_resolve_outcome_rejects_contradiction(passed, outcome):
    with pytest.raises(ValueError, match="contradicts"):
        resolve_outcome(passed, outcome)


def test_sub_gate_result_outcome_defaults_from_passed_and_rejects_contradiction():
    assert SubGateResult(name="g", passed=True).outcome is Outcome.PASS
    assert SubGateResult(name="g", passed=np.bool_(False)).outcome is Outcome.FAIL
    with pytest.raises(ValueError):
        SubGateResult(name="g", passed=True, outcome=Outcome.ERROR)


def test_derive_verdict_blocking_error_is_invalid_even_with_a_powered_fail():
    v = derive_verdict(
        [_e("min_sample_size", Outcome.PASS), _e("a", Outcome.FAIL), _e("b", Outcome.ERROR)],
        blocking={"min_sample_size", "a", "b"},
    )
    assert v.verdict is Verdict.INVALID
    assert v.reasons == ("b:error",)


def test_derive_verdict_powered_fail_is_kill():
    v = derive_verdict([_e("min_sample_size", Outcome.PASS), _e("a", Outcome.FAIL)], blocking={"min_sample_size", "a"})
    assert v.verdict is Verdict.KILL
    assert v.reasons == ("a",)


def test_derive_verdict_fail_on_short_sample_is_needs_more_days():
    v = derive_verdict(
        [_e("min_sample_size", Outcome.INSUFFICIENT_SAMPLE, required_n=90), _e("a", Outcome.FAIL)],
        blocking={"min_sample_size", "a"},
    )
    assert v.verdict is Verdict.NEEDS_MORE_DAYS
    assert v.required_n == 90


def test_derive_verdict_unreachable_sample_is_inconclusive():
    v = derive_verdict(
        [_e("min_sample_size", Outcome.INSUFFICIENT_SAMPLE, required_n=None, reachable=False)],
        blocking={"min_sample_size"},
    )
    assert v.verdict is Verdict.INCONCLUSIVE
    assert v.reasons == ("min_sample_size:unreachable_sample",)


def test_derive_verdict_fail_without_sample_gate_is_inconclusive():
    v = derive_verdict([_e("a", Outcome.FAIL)], blocking={"a"})
    assert v.verdict is Verdict.INCONCLUSIVE


def test_derive_verdict_explicit_power_overrides_sample_gate():
    assert derive_verdict([_e("a", Outcome.FAIL)], blocking={"a"}, powered=True).verdict is Verdict.KILL


def test_derive_verdict_not_applicable_needs_a_deferral():
    entries = [_e("min_sample_size", Outcome.PASS), _e("replay_parity", Outcome.NOT_APPLICABLE)]
    blocking = {"min_sample_size", "replay_parity"}
    assert derive_verdict(entries, blocking=blocking).verdict is Verdict.INVALID
    deferred = derive_verdict(entries, blocking=blocking, deferrable={"replay_parity": "shadow_exit"})
    assert deferred.verdict is Verdict.PASS
    assert deferred.obligations == (("replay_parity", "shadow_exit"),)


def test_derive_verdict_ignores_non_blocking_checks():
    v = derive_verdict([_e("a", Outcome.ERROR), _e("b", Outcome.PASS)], blocking={"b"})
    assert v.verdict is Verdict.PASS
    assert v.outcomes == {"a": "error", "b": "pass"}


def test_verdict_result_round_trips_through_dict():
    v = derive_verdict(
        [_e("min_sample_size", Outcome.INSUFFICIENT_SAMPLE, required_n=70)], blocking={"min_sample_size"}
    )
    assert VerdictResult.from_dict(v.to_dict()) == v


# ---------------------------------------------------------------------------
# Sub-gates that used to pass by default
# ---------------------------------------------------------------------------


class _R:
    def __init__(self, daily_pnl=None, n_fills=0, n_trading_days=0):
        self.daily_pnl = daily_pnl
        self.n_fills = n_fills
        self.n_trading_days = n_trading_days


def test_max_drawdown_without_series_is_error_not_pass():
    res = MaxDrawdownGate().evaluate(_R(daily_pnl=[]), None, {})
    assert res.passed is False
    assert res.outcome is Outcome.ERROR


def test_max_drawdown_on_flat_equity_is_insufficient_sample():
    res = MaxDrawdownGate().evaluate(_R(daily_pnl=[0.0, 0.0, 0.0]), None, {})
    assert res.outcome is Outcome.INSUFFICIENT_SAMPLE


def test_max_drawdown_never_above_zero_with_losses_fails():
    res = MaxDrawdownGate().evaluate(_R(daily_pnl=[-5.0, -3.0, 1.0]), None, {"max_drawdown_pct": 30.0})
    assert res.outcome is Outcome.FAIL
    assert res.metrics["max_dd_pct"] == float("inf")


def test_max_drawdown_measured_case_unchanged():
    res = MaxDrawdownGate().evaluate(_R(daily_pnl=[10.0, -2.0, 5.0]), None, {"max_drawdown_pct": 30.0})
    assert res.outcome is Outcome.PASS
    assert res.metrics["max_dd_pct"] == pytest.approx(2.0 / 13.0 * 100.0)  # existing max-peak denominator


def test_min_sample_size_short_sample_reports_required_days():
    res = MinSampleSizeGate().evaluate(_R(n_fills=150, n_trading_days=40), None, {"min_fills": 300, "min_days": 60})
    assert res.passed is False
    assert res.outcome is Outcome.INSUFFICIENT_SAMPLE
    assert res.metrics["required_n"] == 80  # 300 fills at 150/40 per day
    assert res.metrics["reachable"] is None


def test_min_sample_size_zero_fills_is_unreachable():
    res = MinSampleSizeGate().evaluate(_R(n_fills=0, n_trading_days=40), None, {"min_fills": 300, "min_days": 60})
    assert res.outcome is Outcome.INSUFFICIENT_SAMPLE
    assert res.metrics["reachable"] is False


def test_min_sample_size_oos_floor_without_oos_counts_is_error():
    # Taker profiles state min_oos_trades / min_oos_days; full-sample counts
    # must not stand in for the out-of-sample axis.
    res = MinSampleSizeGate().evaluate(
        _R(n_fills=10_000, n_trading_days=500), None, {"min_oos_trades": 200, "min_oos_days": 30}
    )
    assert res.outcome is Outcome.ERROR


def test_min_sample_size_without_daily_rows_is_error():
    res = MinSampleSizeGate().evaluate(
        _R(daily_pnl=UnavailableDailyPnl("per-tick only"), n_fills=500, n_trading_days=0),
        None,
        {"min_fills": 300, "min_days": 60},
    )
    assert res.outcome is Outcome.ERROR


# ---------------------------------------------------------------------------
# Gate C aggregate (_invoke_sub_gates)
# ---------------------------------------------------------------------------

_STRICT = ValidationProfile(
    name="test_strict",
    is_strict=True,
    thresholds={
        "maker": {
            "min_fills": 300,
            "min_days": 60,
            "sharpe_is_min": 1.0,
            "winning_day_pct_min": 55.0,
            "max_drawdown_pct": 30.0,
        }
    },
    blocking_sub_gates=("min_sample_size", "sharpe_threshold", "winning_day_pct", "max_drawdown"),
)


def _maker_payload(daily: list[float], fills_per_day: int = 10) -> dict:
    n_days = len(daily)
    return {
        "run_id": "synthetic",
        "instrument": "SYN",
        "strategy_name": "synthetic",
        "pnl_pts": float(sum(daily)),
        "n_fills": n_days * fills_per_day,
        "n_trading_days": n_days,
        "pnl_per_fill": float(sum(daily)) / max(n_days * fills_per_day, 1),
        "fill_rate_per_day": float(fills_per_day),
        "daily_pnl": [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "pnl_pts": v} for i, v in enumerate(daily)],
    }


def _verdict(daily: list[float]) -> VerdictResult:
    _, blocking, verdict = _invoke_sub_gates_with_verdict(
        strategy_type="maker",
        result_payload=_maker_payload(daily),
        thresholds=_STRICT.thresholds_for(strategy_type="maker"),
        profile=_STRICT,
    )
    assert blocking is not None and blocking["verdict"] == verdict.to_dict()
    return verdict


def test_s1_zero_edge_small_sample_is_not_kill():
    daily = list(np.random.default_rng(1).normal(0.0, 10.0, 20))
    verdict = _verdict(daily)
    assert verdict.verdict in (Verdict.NEEDS_MORE_DAYS, Verdict.INCONCLUSIVE)
    assert verdict.verdict is not Verdict.KILL


def test_s2_negative_edge_large_sample_is_kill():
    daily = list(np.random.default_rng(2).normal(-5.0, 10.0, 120))
    verdict = _verdict(daily)
    assert verdict.verdict is Verdict.KILL
    assert "sharpe_threshold" in verdict.reasons


def test_s3_known_positive_edge_passes():
    daily = list(np.random.default_rng(3).normal(8.0, 4.0, 120))
    assert _verdict(daily).verdict is Verdict.PASS


def test_blocking_sub_gate_exception_is_invalid_not_killed(monkeypatch):
    def _boom(self, result, config, thresholds):
        raise RuntimeError("bad input")

    monkeypatch.setattr(MaxDrawdownGate, "evaluate", _boom)
    _, blocking = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=_maker_payload(list(np.random.default_rng(3).normal(8.0, 4.0, 120))),
        thresholds=_STRICT.thresholds_for(strategy_type="maker"),
        profile=_STRICT,
    )
    assert blocking is not None
    assert blocking["triage_status"] == "invalid"
    assert blocking["verdict"]["verdict"] == "INVALID"
    with pytest.raises(VerdictNotKillError):
        KillRecord.from_blocking(alpha_id="a", gate="C", blocking=blocking)


def test_failing_core_check_blocks_even_when_profile_sub_gates_pass():
    daily = list(np.random.default_rng(3).normal(8.0, 4.0, 120))
    _, blocking = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=_maker_payload(daily),
        thresholds=_STRICT.thresholds_for(strategy_type="maker"),
        profile=_STRICT,
        core_checks=[check_entry("gate_c.has_fills", Outcome.FAIL)],
    )
    assert blocking is not None
    assert blocking["passed"] is False
    assert [e["name"] for e in blocking["failing"]] == ["gate_c.has_fills"]


def test_disabled_core_check_does_not_block():
    daily = list(np.random.default_rng(3).normal(8.0, 4.0, 120))
    _, blocking = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=_maker_payload(daily),
        thresholds=_STRICT.thresholds_for(strategy_type="maker"),
        profile=_STRICT,
        core_checks=[check_entry("gate_c.walk_forward", Outcome.NOT_APPLICABLE) | {"blocking": False}],
    )
    assert blocking is not None and blocking["passed"] is True


def test_day_axis_gates_error_without_dated_rows():
    payload = _maker_payload([1.0] * 80)
    payload.pop("daily_pnl")
    payload["daily_pnl_unavailable"] = "per-tick equity only"
    advisory, _, verdict = _invoke_sub_gates_with_verdict(
        strategy_type="maker",
        result_payload=payload,
        thresholds=_STRICT.thresholds_for(strategy_type="maker"),
        profile=_STRICT,
    )
    outcomes = {e["name"]: e["outcome"] for e in advisory}
    assert outcomes["sharpe_threshold"] == "error"
    assert outcomes["min_sample_size"] == "error"
    assert verdict.verdict is Verdict.INVALID


# ---------------------------------------------------------------------------
# Research decision and kill record
# ---------------------------------------------------------------------------


def _report(blocking: dict) -> dict:
    return {"details": {"sub_gates_blocking": blocking}}


def test_research_decision_follows_stored_verdict():
    decision = derive_research_decision_from_gate_c_report(
        _report({"passed": False, "failing": [], "names": [], "verdict": {"verdict": "INVALID"}})
    )
    assert decision["status"] == "invalid"


def test_research_decision_failure_status_ignores_passing_blocking_names():
    # max_drawdown passed; only sharpe_threshold failed. The passing name used
    # to drive the status to blocked_by_risk.
    blocking = {
        "passed": False,
        "names": ["sharpe_threshold", "max_drawdown"],
        "failing": [{"name": "sharpe_threshold"}],
        "triage_status": "killed",
        "triage_reasons": ["sharpe_threshold"],
        "verdict": {"verdict": "KILL", "reasons": ["sharpe_threshold"]},
    }
    decision = derive_research_decision_from_gate_c_report(_report(blocking))
    assert decision["status"] == "failed"
    assert decision["evidence"] == ["sharpe_threshold"]


def test_kill_record_from_verdict_refuses_non_kill():
    for verdict in ("PASS", "NEEDS-MORE-DAYS", "INCONCLUSIVE", "INVALID"):
        with pytest.raises(VerdictNotKillError):
            KillRecord.from_verdict(alpha_id="a", gate="C", verdict={"verdict": verdict}, run_id="r1")


def test_kill_record_from_verdict_keys_the_kill_to_its_run():
    kill = {"verdict": "KILL", "reasons": ["sharpe_threshold"]}
    r1 = KillRecord.from_verdict(alpha_id="a", gate="C", verdict=kill, run_id="r1")
    r2 = KillRecord.from_verdict(alpha_id="a", gate="C", verdict=kill, run_id="r2")
    assert r1.reason == "sharpe_threshold"
    assert r1.kill_id() != r2.kill_id()
    assert r1.to_jsonl_dict()["run_id"] == "r1"


def test_kill_id_without_run_id_is_unchanged():
    import hashlib

    rec = KillRecord(alpha_id="a", gate="manual", reason="x", stable_artifact_hash="h")
    assert rec.kill_id() == hashlib.sha256(b"a:manual:h").hexdigest()


# ---------------------------------------------------------------------------
# Evidence checks report ERROR, not a measured failure
# ---------------------------------------------------------------------------


def test_replay_parity_missing_report_is_error_not_fail():
    from hft_platform.alpha._sub_gates.replay_parity import ReplayParityGate

    res = ReplayParityGate().evaluate(_R(), None, {})
    assert res.passed is False
    assert res.outcome is Outcome.ERROR


def test_strict_blocking_replay_parity_without_report_is_invalid_not_kill():
    profile = ValidationProfile(
        name="strict_parity",
        is_strict=True,
        thresholds=_STRICT.thresholds,
        blocking_sub_gates=(*_STRICT.blocking_sub_gates, "replay_parity"),
    )
    daily = list(np.random.default_rng(2).normal(-5.0, 10.0, 120))
    _, _, verdict = _invoke_sub_gates_with_verdict(
        strategy_type="maker",
        result_payload=_maker_payload(daily),
        thresholds=profile.thresholds_for(strategy_type="maker"),
        profile=profile,
    )
    assert verdict.verdict is Verdict.INVALID
    assert "replay_parity:error" in verdict.reasons
