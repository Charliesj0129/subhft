import json
from pathlib import Path

import numpy as np
import pytest

from research.combinatorial.expression_lang import compile_expression, validate_expression
from research.combinatorial.ledger import TrialLedger
from research.combinatorial.search_engine import AlphaSearchEngine


def test_compile_expression_evaluate():
    expr = compile_expression("zscore(ts_delta(ofi, 3), 5)")
    out = expr.evaluate({"ofi": np.array([0.0, 1.0, 2.0, 3.0, 2.0, 1.0], dtype=np.float64)})
    assert out.shape == (6,)
    assert np.isfinite(out).all()


def test_validate_expression_rejects_raw_price():
    try:
        validate_expression("ts_delta(price, 5)")
    except ValueError as exc:
        assert "Raw price level variable" in str(exc)
    else:
        raise AssertionError("Expected validate_expression to reject raw price feature")


def test_search_engine_random_and_template():
    features = {
        "ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1], dtype=np.float64),
        "bid_qty": np.array([10, 12, 11, 9, 13, 14, 13], dtype=np.float64),
    }
    returns = np.array([0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.005], dtype=np.float64)
    engine = AlphaSearchEngine(features=features, returns=returns, random_seed=7)

    random_results = engine.random_search(n_trials=8)
    assert random_results
    assert all(np.isfinite(item.score) for item in random_results)

    template_results = engine.template_sweep("zscore(ts_delta(ofi, {w}), 5)", {"w": [3, 5]})
    assert len(template_results) == 2
    assert template_results[0].score >= template_results[1].score


def test_selection_sharpe_field_replaces_fake_oos_label():
    features = {"ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1], dtype=np.float64)}
    engine = AlphaSearchEngine(features=features, random_seed=7)
    result = engine.evaluate_expression("sign(ts_delta(ofi, 3))")
    assert hasattr(result, "selection_sharpe")
    assert not hasattr(result, "sharpe_oos")


def test_signal_information_ratio_not_annualized():
    # sign(ts_delta(ofi, 1)) with no returns falls back to signal-vs-itself pnl:
    # pnl = sig * (shift(sig, -1) - sig). Assert the reported selection_sharpe matches
    # a raw mean/std ratio with no sqrt(252) annualization factor applied.
    ofi = np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1, -0.2], dtype=np.float64)
    features = {"ofi": ofi}
    engine = AlphaSearchEngine(features=features, random_seed=7)
    expr = "sign(ts_delta(ofi, 1))"
    result = engine.evaluate_expression(expr)

    from research.combinatorial.expression_lang import compile_expression

    compiled = compile_expression(expr)
    sig = compiled.evaluate({"ofi": ofi})
    shifted = np.roll(sig, -1)
    shifted[-1] = sig[-1]
    pnl = sig * (shifted - sig)
    expected_raw_ratio = float(np.mean(pnl) / np.std(pnl))
    expected_annualized = expected_raw_ratio * float(np.sqrt(252.0))

    assert result.selection_sharpe == pytest.approx(expected_raw_ratio, abs=1e-9)
    if abs(expected_annualized) > 1e-9:
        assert result.selection_sharpe != pytest.approx(expected_annualized, rel=1e-3)


def test_dataset_fingerprint_and_search_run_id_recorded(tmp_path):
    features = {"ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1], dtype=np.float64)}
    engine = AlphaSearchEngine(features=features, random_seed=7, dataset_fingerprint="abc123")
    results = engine.random_search(n_trials=2)

    out_path = engine.save_results(results, path=str(tmp_path / "results.json"))
    payload = json.loads(Path(out_path).read_text())
    assert payload["dataset_fingerprint"] == "abc123"
    assert payload["search_run_id"] == engine.search_run_id
    assert payload["search_run_id"]
    assert len(payload["results"]) == 2


def test_genetic_search_tags_genetic_search_algorithm_with_no_legacy_warning():
    import structlog.testing

    features = {
        "ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1, -0.2, 0.05], dtype=np.float64),
    }
    engine = AlphaSearchEngine(features=features, random_seed=7)

    with structlog.testing.capture_logs() as logs:
        results = engine.genetic_search(population=4, generations=1)

    assert results
    assert all(item.metadata["search_algorithm"] == "genetic_search" for item in results)

    legacy_events = [e for e in logs if e.get("event") == "legacy_mutation_search_invoked"]
    assert not legacy_events


def test_genetic_search_best_score_is_monotonic_non_decreasing_across_generations():
    import structlog.testing

    features = {
        "ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1, -0.2, 0.05, 0.15, -0.05, 0.3], dtype=np.float64),
    }
    engine = AlphaSearchEngine(features=features, random_seed=11)

    with structlog.testing.capture_logs() as logs:
        engine.genetic_search(population=10, generations=4)

    gen_events = [e for e in logs if e.get("event") == "genetic_search_generation_complete"]
    assert len(gen_events) == 4
    scores = [e["best_score"] for e in gen_events]
    # Elitism structurally guarantees this: the prior generation's best survivor is
    # always carried into `results` unchanged, so the post-sort best can never regress.
    assert scores == sorted(scores)


def test_genetic_search_records_parent_ids_for_crossover_and_mutation_children(monkeypatch):
    # Deterministic by construction (fixed seed + fixed rng.random() return value), not
    # a statistical/flaky test: genetic_search's branch-selection rolls call
    # self._rng.random() (patched here), while tournament selection and crossover/
    # mutation's own subtree/token picks call self._rng.choice()/sample() (untouched,
    # since random.Random routes those through _randbelow/getrandbits, not .random()).
    #
    # Captures parent_ids at the evaluate_expression() call site directly (a spy on the
    # instance method) rather than round-tripping through the ledger: with a single
    # feature and only 6 window choices, a genuinely-mutated child can legitimately be
    # textually identical to an already-evaluated population member, and the ledger's
    # (pre-existing, Phase 1/2) idempotent dedup-by-trial_id then keeps the *first*
    # occurrence's parent_ids — masking a real, correctly-threaded parent_ids on
    # rediscovery. That's expected ledger behavior, not a Phase 3 bug, but it makes the
    # ledger an unreliable place to assert branch-selection bookkeeping from.
    features = {
        "ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1, -0.2, 0.05, 0.15], dtype=np.float64),
    }

    def run_and_capture_child_parent_ids(rng_value: float) -> list[tuple[str, ...]]:
        engine = AlphaSearchEngine(features=features, random_seed=1)
        monkeypatch.setattr(engine._rng, "random", lambda: rng_value)
        seen: list[tuple[str, ...]] = []
        original_evaluate = engine.evaluate_expression

        def spy_evaluate(expression, *, algorithm="manual", parent_ids=()):
            if parent_ids:
                seen.append(tuple(parent_ids))
            return original_evaluate(expression, algorithm=algorithm, parent_ids=parent_ids)

        monkeypatch.setattr(engine, "evaluate_expression", spy_evaluate)
        engine.genetic_search(population=4, generations=1, crossover_prob=0.5, mutation_prob=0.9)
        return seen

    # 0.0 always < crossover_prob (0.5) -> crossover branch every slot.
    cross_parent_ids = run_and_capture_child_parent_ids(0.0)
    assert any(len(p) == 2 for p in cross_parent_ids)

    # 0.7 fails the crossover roll (0.7 !< 0.5) then passes the mutation roll (0.7 < 0.9)
    # -> mutation branch every slot.
    mutate_parent_ids = run_and_capture_child_parent_ids(0.7)
    assert any(len(p) == 1 for p in mutate_parent_ids)


def test_mutate_expression_produces_genuine_mutations_not_a_silent_random_fallback():
    # Regression test: _mutate_expression used to strip "(", ")", "," entirely during
    # tokenization and never restore them, so compile_expression(rebuilt) always raised
    # SyntaxError and it silently fell back to _random_expression() on every call —
    # 100% of the time, undetected because no test asserted anything about mutation's
    # actual output. Assert real token-level mutations occur and nothing falls back.
    features = {
        "ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1, -0.2, 0.05, 0.15], dtype=np.float64),
        "bidq": np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0], dtype=np.float64),
    }
    engine = AlphaSearchEngine(features=features, random_seed=1)

    parent = "sign(ts_delta(ofi, 3))"
    mutated = [engine._mutate_expression(parent) for _ in range(30)]

    assert engine._mutation_failures == 0
    assert any(m != parent for m in mutated)
    for m in mutated:
        compile_expression(m)  # every result must be valid, re-compilable syntax


def test_evaluate_expression_records_trial_when_ledger_attached(tmp_path):
    features = {"ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1], dtype=np.float64)}
    ledger = TrialLedger(path=tmp_path / "trials.jsonl")
    engine = AlphaSearchEngine(
        features=features,
        random_seed=7,
        dataset_fingerprint="fp1",
        trial_ledger=ledger,
        partition_manifest_hash="mh1",
    )

    engine.evaluate_expression("sign(ts_delta(ofi, 3))", algorithm="random_search")

    rows = ledger.read_trials(search_run_id=engine.search_run_id)
    assert len(rows) == 1
    assert rows[0]["stage"] == "search"
    assert rows[0]["candidate_id"] == TrialLedger.candidate_id_for("sign(ts_delta(ofi, 3))")
    assert rows[0]["dataset_fingerprint"] == "fp1"
    assert "score" in rows[0]["metrics"]


def test_evaluate_expression_records_compile_failure_and_still_raises(tmp_path):
    features = {"ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1], dtype=np.float64)}
    ledger = TrialLedger(path=tmp_path / "trials.jsonl")
    engine = AlphaSearchEngine(features=features, random_seed=7, dataset_fingerprint="fp1", trial_ledger=ledger)

    with pytest.raises(Exception):
        engine.evaluate_expression("not_a_real_function(ofi", algorithm="random_search")

    rows = ledger.read_trials()
    assert len(rows) == 1
    assert rows[0]["stage"] == "compile"
    assert rows[0]["status"] == "error"
    assert rows[0]["failure_reason"]


def test_evaluate_expression_records_runtime_failure_for_unknown_feature_and_still_raises(tmp_path):
    # Regression test: previously only compile_expression() was wrapped in the
    # ledger's try/except, so a runtime failure inside compiled.evaluate() (e.g. a
    # feature name the expression's own grammar can't validate at compile time)
    # would raise uncaught instead of being recorded as a clean compile-stage failure.
    features = {"ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1], dtype=np.float64)}
    ledger = TrialLedger(path=tmp_path / "trials.jsonl")
    engine = AlphaSearchEngine(features=features, random_seed=7, dataset_fingerprint="fp1", trial_ledger=ledger)

    with pytest.raises(KeyError):
        engine.evaluate_expression("sign(ts_delta(missing_feature, 3))", algorithm="random_search")

    rows = ledger.read_trials()
    assert len(rows) == 1
    assert rows[0]["stage"] == "compile"
    assert rows[0]["status"] == "error"
    assert rows[0]["failure_reason"]


def test_arity_mismatched_expression_records_compile_failure_instead_of_uncaught_typeerror(tmp_path):
    features = {"ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1], dtype=np.float64)}
    ledger = TrialLedger(path=tmp_path / "trials.jsonl")
    engine = AlphaSearchEngine(features=features, random_seed=7, dataset_fingerprint="fp1", trial_ledger=ledger)

    with pytest.raises(ValueError):
        engine.evaluate_expression("ts_mean(ofi)", algorithm="random_search")  # missing required window arg

    rows = ledger.read_trials()
    assert len(rows) == 1
    assert rows[0]["stage"] == "compile"
    assert rows[0]["status"] == "error"


def test_evaluate_expression_reuses_cached_result_for_semantically_equivalent_expressions(tmp_path, monkeypatch):
    features = {"ofi": np.array([0.0, 0.1, 0.3, -0.1, 0.2, 0.25, 0.1], dtype=np.float64)}
    ledger = TrialLedger(path=tmp_path / "trials.jsonl")
    engine = AlphaSearchEngine(features=features, random_seed=7, dataset_fingerprint="fp1", trial_ledger=ledger)

    import research.combinatorial.search_engine as search_engine_module

    call_count = {"n": 0}
    original_compile = search_engine_module.compile_expression

    def counting_compile(*args, **kwargs):
        call_count["n"] += 1
        return original_compile(*args, **kwargs)

    monkeypatch.setattr(search_engine_module, "compile_expression", counting_compile)

    first = engine.evaluate_expression("add(ofi, 1)", algorithm="random_search")
    second = engine.evaluate_expression("1 + ofi", algorithm="random_search")

    assert call_count["n"] == 1, "second call should hit the within-run result cache, not recompile"
    assert second is first
    assert second.score == first.score

    # Both calls share the same semantic trial_id (same expression, dataset, algorithm
    # expressed two ways), so the ledger's existing idempotent dedup-on-trial_id
    # collapses them to one row — this is correct, not a lost trial: they are the
    # same hypothesis, not two distinct ones.
    rows = ledger.read_trials()
    assert len(rows) == 1
