"""Tests for feasibility validation scorecard."""
import pytest
from hft_platform.cli._feasibility import FeasibilityScorecard, Verdict


class TestVerdict:
    def test_pass_when_all_criteria_met(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=1820,
            daily_pnl_values=[200, 300, -100, 250, 400, -50, 320, 500],
            net_alpha_retention_rate=0.73,
            hard_limit_triggers=0,
            max_consecutive_loss_days=1,
        )
        assert sc.verdict == Verdict.PASS

    def test_fail_when_cumulative_loss(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=-500,
            daily_pnl_values=[-100, -200, 50, -250],
            net_alpha_retention_rate=0.60,
            hard_limit_triggers=0,
            max_consecutive_loss_days=2,
        )
        assert sc.verdict == Verdict.FAIL

    def test_fail_when_retention_too_low(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=500,
            daily_pnl_values=[100, 100, 100, 100, 100],
            net_alpha_retention_rate=0.30,
            hard_limit_triggers=0,
            max_consecutive_loss_days=0,
        )
        assert sc.verdict == Verdict.FAIL

    def test_fail_when_too_many_hard_limits(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=500,
            daily_pnl_values=[100, 100, 100, 100, 100],
            net_alpha_retention_rate=0.70,
            hard_limit_triggers=3,
            max_consecutive_loss_days=0,
        )
        assert sc.verdict == Verdict.FAIL

    def test_fail_when_too_many_consecutive_loss_days(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=500,
            daily_pnl_values=[100, 100, 100, 100, 100],
            net_alpha_retention_rate=0.70,
            hard_limit_triggers=0,
            max_consecutive_loss_days=4,
        )
        assert sc.verdict == Verdict.FAIL


class TestTTest:
    def test_significant_positive_returns(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=2000,
            daily_pnl_values=[200, 300, 250, 200, 350, 300, 400, 250, 300, 200],
            net_alpha_retention_rate=0.70,
            hard_limit_triggers=0,
            max_consecutive_loss_days=0,
        )
        assert sc.t_test_p_value < 0.05

    def test_single_day_returns_p_value_1(self):
        sc = FeasibilityScorecard(
            cumulative_net_pnl_ntd=100,
            daily_pnl_values=[100],
            net_alpha_retention_rate=0.70,
            hard_limit_triggers=0,
            max_consecutive_loss_days=0,
        )
        assert sc.t_test_p_value == 1.0
