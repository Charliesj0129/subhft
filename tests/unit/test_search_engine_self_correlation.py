"""The search engine must not manufacture self-correlated candidates.

``ts_corr(x, x, w)`` is +1 for every window in which ``x`` varies, so
``sign(ts_corr(x, x, w))`` is a permanent long signal. Its Sharpe measures
market drift, not predictive skill, and it will rank as a "winner" against a
trending series. ``_random_expression`` drew the second correlation operand
independently of the first, so with ``k`` features it produced a tautology on
roughly one in ``k`` of every mix-family trial.
"""

from __future__ import annotations

import numpy as np
import pytest

from research.combinatorial.search_engine import AlphaSearchEngine, has_self_correlation


def _engine(n_features: int = 3, *, seed: int = 7) -> AlphaSearchEngine:
    rng = np.random.default_rng(seed)
    features = {f"f{i}": rng.normal(size=256) for i in range(n_features)}
    return AlphaSearchEngine(features=features, returns=rng.normal(size=256), random_seed=seed)


@pytest.mark.parametrize(
    "expression",
    [
        "ts_corr(mid, mid, 50)",
        "sign(ts_corr(mid, mid, 50))",
        "sign(ts_corr((mid), mid, 50))",
        "zscore(ts_corr(ts_delta(mid, 5), ts_delta(mid, 5), 20), 20)",
    ],
)
def test_self_correlated_expressions_are_detected(expression: str) -> None:
    assert has_self_correlation(expression) is True


@pytest.mark.parametrize(
    "expression",
    [
        "ts_corr(mid, volume, 50)",
        "sign(ts_corr(mid, ts_delta(mid, 5), 50))",
        "zscore(ts_delta(mid, 10), 10)",
        "rank(ts_sum(volume, 20))",
    ],
)
def test_genuinely_paired_expressions_are_not_flagged(expression: str) -> None:
    assert has_self_correlation(expression) is False


def test_unparseable_expression_is_left_to_the_compiler() -> None:
    """The compiler owns validity; this guard must not raise on junk."""
    assert has_self_correlation("ts_corr(mid, mid") is False


def test_random_search_never_emits_a_self_correlated_expression() -> None:
    engine = _engine(n_features=2)
    results = engine.random_search(n_trials=400)
    offenders = [r.expression for r in results if has_self_correlation(r.expression)]
    assert offenders == [], f"generator produced self-correlated candidates: {offenders[:3]}"


def test_correlation_family_still_gets_generated() -> None:
    """Excluding the self-pair must not silently delete the whole mix family."""
    engine = _engine(n_features=3)
    results = engine.random_search(n_trials=400)
    assert any("ts_corr(" in r.expression for r in results), "mix family disappeared entirely"


def test_single_feature_universe_falls_back_instead_of_self_correlating() -> None:
    """With one feature there is no distinct partner, so no ts_corr may be emitted."""
    engine = _engine(n_features=1)
    results = engine.random_search(n_trials=200)
    assert all(not has_self_correlation(r.expression) for r in results)
    assert all("ts_corr(" not in r.expression for r in results)


def test_genetic_search_never_emits_a_self_correlated_expression() -> None:
    """Mutation swaps feature tokens, which can also collapse the two operands."""
    engine = _engine(n_features=2)
    results = engine.genetic_search(population=24, generations=6)
    offenders = [r.expression for r in results if has_self_correlation(r.expression)]
    assert offenders == [], f"mutation produced self-correlated candidates: {offenders[:3]}"
