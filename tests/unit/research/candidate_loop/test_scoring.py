"""score_v1 gates (first-failure-wins), final_score caps, §14 promotion."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.candidate_loop.schema import DeathReason, Status
from research.candidate_loop.scoring import (
    GATE_ORDER,
    GateOutcome,
    ScoredCandidate,
    apply_hard_gates,
    assign_statuses,
    compute_final_score,
    direction_match,
    is_maker_rescuable,
    load_scoring_config,
)

CONFIG_PATH = Path(__file__).resolve().parents[4] / "config" / "research" / "candidate_loop" / "scoring_v1.yaml"
CFG = load_scoring_config(CONFIG_PATH)


def _train(**overrides: float) -> dict:
    base = {
        "signal_std_zero_day_fraction": 0.0,
        "ic_tstat": 2.5,
        "ic": 0.05,
        "sign_consistency": 0.8,
        "cost_survival_score": 0.6,
        "maker_cost_survival_score": 0.7,
        "latency_1ms_score": 0.8,
        "one_day_concentration": 0.3,
    }
    base.update(overrides)
    return base


def _val(**overrides: float) -> dict:
    base = {
        "ic_tstat": 2.0,
        "ic": 0.04,
        "sign_consistency": 0.7,
        "cost_survival_score": 0.5,
        "latency_1ms_score": 0.7,
        "one_day_concentration": 0.3,
        "turnover_proxy": 50.0,
    }
    base.update(overrides)
    return base


class TestConfig:
    def test_frozen_taker_threshold_and_maker_block(self) -> None:
        assert CFG.scoring_version == "score_v1"
        assert CFG.cost_survival_min == 0.3  # taifex_v1, FROZEN
        assert CFG.maker_cost_survival_min == 0.3
        assert CFG.q_hat_symbol == "TXFD6"
        assert CFG.maker_cost_assumption_version == "taifex_maker_qhat_v1"
        assert CFG.q_hat_table_path.endswith("txfd6_q_hat.parquet")


class TestHardGates:
    def test_healthy_candidate_passes_all_gates(self) -> None:
        outcome = apply_hard_gates(_train(), _val(), CFG)
        assert outcome.survived
        assert outcome.gates_passed == GATE_ORDER
        assert outcome.death_reason is None

    @pytest.mark.parametrize(
        ("overrides", "reason", "gate"),
        [
            ({"signal_std_zero_day_fraction": 0.6}, DeathReason.NO_SIGNAL, "no_signal"),
            ({"ic_tstat": 0.5}, DeathReason.NO_SIGNAL, "no_signal"),
            ({"sign_consistency": 0.4}, DeathReason.SIGN_UNSTABLE, "sign_unstable"),
            ({"cost_survival_score": 0.2}, DeathReason.COST_KILLED, "cost_proxy_taker"),
            ({"latency_1ms_score": 0.4}, DeathReason.LATENCY_KILLED, "latency_1ms"),
            ({"latency_1ms_score": -0.8}, DeathReason.LATENCY_KILLED, "latency_1ms"),
            ({"one_day_concentration": 0.7}, DeathReason.ONE_DAY_ONLY, "one_day_only"),
        ],
    )
    def test_single_gate_failures(self, overrides: dict, reason: DeathReason, gate: str) -> None:
        outcome = apply_hard_gates(_train(**overrides), _val(), CFG)
        assert outcome.death_reason == reason
        assert gate in outcome.gates_failed

    def test_train_validation_contradiction_is_sign_unstable(self) -> None:
        outcome = apply_hard_gates(_train(ic=0.05), _val(ic=-0.05), CFG)
        assert outcome.death_reason == DeathReason.SIGN_UNSTABLE

    def test_tiny_opposite_ics_below_floor_are_not_contradiction(self) -> None:
        outcome = apply_hard_gates(_train(ic=0.005), _val(ic=-0.005), CFG)
        assert outcome.survived

    def test_first_failure_wins_death_reason(self) -> None:
        outcome = apply_hard_gates(
            _train(ic_tstat=0.1, cost_survival_score=0.1, one_day_concentration=0.9), _val(), CFG
        )
        assert outcome.death_reason == DeathReason.NO_SIGNAL
        assert set(outcome.gates_failed) >= {"no_signal", "cost_proxy_taker", "one_day_only"}

    def test_maker_rescuable_trace(self) -> None:
        # Dead at taker, alive under maker: COST_KILLED but maker gate passes.
        outcome = apply_hard_gates(_train(cost_survival_score=0.2, maker_cost_survival_score=0.5), _val(), CFG)
        assert outcome.death_reason == DeathReason.COST_KILLED
        assert "cost_proxy_taker" in outcome.gates_failed
        assert "cost_proxy_maker" in outcome.gates_passed
        assert is_maker_rescuable(outcome)

    def test_dead_under_any_execution_fails_both_cost_gates(self) -> None:
        outcome = apply_hard_gates(_train(cost_survival_score=0.1, maker_cost_survival_score=0.1), _val(), CFG)
        assert outcome.death_reason == DeathReason.COST_KILLED
        assert {"cost_proxy_taker", "cost_proxy_maker"} <= set(outcome.gates_failed)
        assert not is_maker_rescuable(outcome)

    def test_maker_gate_ordered_after_taker_gate(self) -> None:
        assert GATE_ORDER.index("cost_proxy_maker") == GATE_ORDER.index("cost_proxy_taker") + 1


class TestFinalScore:
    def test_components_and_weighted_sum(self) -> None:
        val = _val(
            ic_tstat=2.0,  # predictive 0.5 (cap 4)
            sign_consistency=0.7,
            cost_survival_score=0.5,
            latency_1ms_score=0.7,
            one_day_concentration=0.3,
            turnover_proxy=50.0,  # 0.25 of 200 cap
        )
        final, comp = compute_final_score(val, signal_node_count=16, cfg=CFG)
        assert comp["predictive_score"] == pytest.approx(0.5)
        assert comp["stability_score"] == pytest.approx(0.7)
        assert comp["cost_survival_score"] == pytest.approx(0.5)
        assert comp["latency_survival_score"] == pytest.approx(0.7)
        assert comp["fragility_penalty"] == pytest.approx(0.3)
        assert comp["turnover_penalty"] == pytest.approx(0.25)
        assert comp["complexity_penalty"] == pytest.approx(16 / 64)
        assert final == pytest.approx(0.5 + 0.7 + 0.5 + 0.7 - 0.3 - 0.25 - 0.25)

    def test_caps_clamp_extremes(self) -> None:
        val = _val(ic_tstat=10.0, cost_survival_score=5.0, latency_1ms_score=-2.0, turnover_proxy=1e6)
        _, comp = compute_final_score(val, signal_node_count=200, cfg=CFG)
        assert comp["predictive_score"] == 1.0
        assert comp["cost_survival_score"] == 1.0
        assert comp["latency_survival_score"] == 0.0  # negative clamped
        assert comp["turnover_penalty"] == 1.0
        assert comp["complexity_penalty"] == 1.0


def _scored(alpha_id: str, score: float, *, survived: bool = True, tstat: float = 0.5) -> ScoredCandidate:
    gate = GateOutcome(
        gates_passed=GATE_ORDER if survived else GATE_ORDER[1:],
        gates_failed=() if survived else ("no_signal",),
        death_reason=None if survived else DeathReason.NO_SIGNAL,
    )
    return ScoredCandidate(
        alpha_id=alpha_id, family="microprice", gate=gate, final_score=score, validation_ic_tstat=tstat
    )


class TestPromotion:
    def test_hundred_survivors_one_promoted_next_decile_watchlist(self) -> None:
        rows = [_scored(f"a{i:03d}", score=float(100 - i)) for i in range(100)]
        statuses = assign_statuses(rows, CFG)
        assert statuses["a000"] == Status.PROMOTED
        assert sum(1 for s in statuses.values() if s == Status.PROMOTED) == 1
        for i in range(1, 11):  # next decile (10 of 100)
            assert statuses[f"a{i:03d}"] == Status.WATCHLIST
        assert statuses["a011"] == Status.REJECTED

    def test_below_cut_high_tstat_lands_on_watchlist(self) -> None:
        rows = [_scored(f"a{i:03d}", score=float(100 - i)) for i in range(99)]
        rows.append(_scored("a099", score=0.0, tstat=1.8))
        statuses = assign_statuses(rows, CFG)
        assert statuses["a099"] == Status.WATCHLIST

    def test_gate_failures_never_promoted(self) -> None:
        rows = [_scored("dead0", score=99.0, survived=False, tstat=3.0), _scored("live0", score=1.0)]
        statuses = assign_statuses(rows, CFG)
        assert statuses["dead0"] == Status.REJECTED
        assert statuses["live0"] == Status.PROMOTED  # only survivor -> top 1%

    def test_no_survivors_everything_rejected(self) -> None:
        rows = [_scored("dead0", score=1.0, survived=False)]
        assert assign_statuses(rows, CFG) == {"dead0": Status.REJECTED}

    def test_deterministic_tie_break_by_alpha_id(self) -> None:
        rows = [_scored("bbb", score=1.0), _scored("aaa", score=1.0)]
        statuses = assign_statuses(rows, CFG)
        assert statuses["aaa"] == Status.PROMOTED  # ceil(0.01*2)=1; tie -> lower alpha_id
        assert statuses["bbb"] == Status.WATCHLIST  # next decile of 2 -> 1


class TestDirectionMatch:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [(0.05, 0.02, True), (0.05, -0.02, False), (0.0, 0.02, False), (-0.1, -0.2, True)],
    )
    def test_direction_match(self, a: float, b: float, expected: bool) -> None:
        assert direction_match(a, b) is expected
