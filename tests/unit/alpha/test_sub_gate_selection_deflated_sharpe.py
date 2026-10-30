"""Tests for SelectionDeflatedSharpeGate and Gate C's search record."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import yaml

from hft_platform.alpha._gate_c import _search_record
from hft_platform.alpha._sub_gates.selection_deflated_sharpe import SelectionDeflatedSharpeGate
from hft_platform.alpha.outcomes import Outcome
from hft_platform.alpha.stats import selection_sr0


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


def _daily(loc: float, *, n: int = 120, seed: int = 0) -> list[float]:
    return np.random.default_rng(seed).normal(loc=loc, scale=1.0, size=n).tolist()


def test_selection_deflated_sharpe_without_a_search_record_is_not_applicable() -> None:
    out = SelectionDeflatedSharpeGate().evaluate(_FakeResult(_daily(1.0)), config=None, thresholds={})

    assert out.outcome is Outcome.NOT_APPLICABLE
    assert "no search record" in out.details


def test_selection_deflated_sharpe_with_only_a_raw_count_is_not_applicable() -> None:
    out = SelectionDeflatedSharpeGate().evaluate(
        _FakeResult(_daily(1.0)), config=None, thresholds={"search_trials": 400}
    )

    assert out.outcome is Outcome.NOT_APPLICABLE
    assert "400 distinct trials" in out.details


def test_selection_deflated_sharpe_deducts_sr0_of_the_effective_count() -> None:
    daily = _daily(0.3)
    out = SelectionDeflatedSharpeGate().evaluate(
        _FakeResult(daily),
        config=None,
        thresholds={"search_trials": 400, "search_trials_effective": 25, "deflated_sharpe_min": 0.5},
    )

    sharpe = float(np.mean(daily) / np.std(daily, ddof=1) * math.sqrt(252))
    penalty = selection_sr0(25, observations=len(daily), annualization=252.0)
    assert out.metrics["n_trials_effective"] == 25
    assert out.metrics["n_trials_distinct"] == 400
    assert math.isclose(out.metrics["penalty"], penalty)
    assert math.isclose(out.metrics["deflated_sharpe"], sharpe - penalty)
    assert out.passed is (sharpe - penalty >= 0.5)


def test_selection_deflated_sharpe_fails_a_thin_edge_picked_from_many_trials() -> None:
    gate = SelectionDeflatedSharpeGate()
    daily = _daily(0.12, n=60, seed=3)

    alone = gate.evaluate(_FakeResult(daily), config=None, thresholds={"search_trials_effective": 1})
    searched = gate.evaluate(_FakeResult(daily), config=None, thresholds={"search_trials_effective": 500})

    assert searched.metrics["deflated_sharpe"] < alone.metrics["deflated_sharpe"]
    assert searched.passed is False


def test_selection_deflated_sharpe_with_one_day_is_insufficient_sample() -> None:
    out = SelectionDeflatedSharpeGate().evaluate(
        _FakeResult([1.0]), config=None, thresholds={"search_trials_effective": 10}
    )

    assert out.outcome is Outcome.INSUFFICIENT_SAMPLE


def test_search_record_reads_the_manifest_trial_counts(tmp_path) -> None:
    alpha_dir = tmp_path / "research" / "alphas" / "gp_x"
    alpha_dir.mkdir(parents=True)
    (alpha_dir / "manifest.yaml").write_text(
        yaml.safe_dump({"experiment_metadata": {"search_trials": 300, "search_trials_effective": 40}})
    )

    assert _search_record(tmp_path, "gp_x") == {"search_trials": 300, "search_trials_effective": 40}


def test_search_record_is_empty_without_a_manifest_or_counts(tmp_path) -> None:
    alpha_dir = tmp_path / "research" / "alphas" / "hand_made"
    alpha_dir.mkdir(parents=True)
    (alpha_dir / "manifest.yaml").write_text(yaml.safe_dump({"experiment_metadata": {"search_trials": None}}))

    assert _search_record(tmp_path, "hand_made") == {}
    assert _search_record(tmp_path, "missing") == {}
