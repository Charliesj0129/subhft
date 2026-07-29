from pathlib import Path

import pytest

from research.combinatorial.ledger import TrialLedger, _default_jsonl_path


def test_record_trial_dedupes_on_trial_id_and_stage(tmp_path):
    ledger = TrialLedger(path=tmp_path / "trials.jsonl")
    trial_id = ledger.trial_id_for(
        expression="sign(ofi)", dataset_fingerprint="fp1", partition_manifest_hash="mh1", algorithm="random_search"
    )
    candidate_id = ledger.candidate_id_for("sign(ofi)")

    first = ledger.record_trial(
        trial_id=trial_id,
        search_run_id="run1",
        candidate_id=candidate_id,
        stage="search",
        status="passed",
        metrics={"score": 1.0},
    )
    second = ledger.record_trial(
        trial_id=trial_id,
        search_run_id="run1",
        candidate_id=candidate_id,
        stage="search",
        status="passed",
        metrics={"score": 1.0},
    )
    assert first is True
    assert second is False

    # A new stage for the same trial_id is a new row (not a dedupe hit).
    third = ledger.record_trial(
        trial_id=trial_id,
        search_run_id="run1",
        candidate_id=candidate_id,
        stage="selection",
        status="passed",
        metrics={},
    )
    assert third is True

    rows = ledger.read_trials()
    assert len(rows) == 2
    assert {row["stage"] for row in rows} == {"search", "selection"}


def test_record_compile_failure_produces_compile_stage_row(tmp_path):
    ledger = TrialLedger(path=tmp_path / "trials.jsonl")
    result = ledger.record_compile_failure(
        expression="bad_expr(",
        dataset_fingerprint="fp1",
        partition_manifest_hash="mh1",
        algorithm="random_search",
        search_run_id="run1",
        reason="SyntaxError: unexpected end",
    )
    assert result is True

    rows = ledger.read_trials()
    assert len(rows) == 1
    assert rows[0]["stage"] == "compile"
    assert rows[0]["status"] == "error"
    assert rows[0]["failure_reason"] == "SyntaxError: unexpected end"

    # Recording the identical compile failure again is a dedupe no-op.
    again = ledger.record_compile_failure(
        expression="bad_expr(",
        dataset_fingerprint="fp1",
        partition_manifest_hash="mh1",
        algorithm="random_search",
        search_run_id="run1",
        reason="SyntaxError: unexpected end",
    )
    assert again is False
    assert len(ledger.read_trials()) == 1


def test_unique_trial_count_respects_filters(tmp_path):
    ledger = TrialLedger(path=tmp_path / "trials.jsonl")
    for i, run in enumerate(["run1", "run1", "run2"]):
        expr = f"sign(ofi_{i})"
        trial_id = ledger.trial_id_for(
            expression=expr, dataset_fingerprint="fp1", partition_manifest_hash="mh", algorithm="random_search"
        )
        ledger.record_trial(
            trial_id=trial_id,
            search_run_id=run,
            candidate_id=ledger.candidate_id_for(expr),
            dataset_fingerprint="fp1",
            stage="search",
            status="passed",
            metrics={},
        )

    assert ledger.unique_trial_count() == 3
    assert ledger.unique_trial_count(search_run_id="run1") == 2
    assert ledger.unique_trial_count(search_run_id="run2") == 1
    assert ledger.unique_trial_count(dataset_fingerprint="fp1") == 3
    assert ledger.unique_trial_count(dataset_fingerprint="other-fp") == 0


def test_resume_after_restart_does_not_double_count(tmp_path):
    path = tmp_path / "trials.jsonl"
    ledger1 = TrialLedger(path=path)
    trial_id = ledger1.trial_id_for(
        expression="sign(ofi)", dataset_fingerprint="fp1", partition_manifest_hash="mh1", algorithm="random_search"
    )
    candidate_id = ledger1.candidate_id_for("sign(ofi)")
    ledger1.record_trial(
        trial_id=trial_id, search_run_id="run1", candidate_id=candidate_id, stage="search", status="passed", metrics={}
    )

    # Simulate a process restart: a brand-new instance pointed at the same file.
    ledger2 = TrialLedger(path=path)
    result = ledger2.record_trial(
        trial_id=trial_id, search_run_id="run1", candidate_id=candidate_id, stage="search", status="passed", metrics={}
    )
    assert result is False
    assert ledger2.unique_trial_count() == 1


def test_candidate_id_is_dataset_independent_and_stable():
    a = TrialLedger.candidate_id_for("sign(ts_delta(ofi, 3))")
    b = TrialLedger.candidate_id_for("sign(ts_delta(ofi, 3))")
    c = TrialLedger.candidate_id_for("sign(ts_delta(ofi, 5))")
    assert a == b
    assert a != c


def test_trial_id_changes_with_dataset_or_partition_or_algorithm():
    base = dict(
        expression="sign(ofi)", dataset_fingerprint="fp1", partition_manifest_hash="mh1", algorithm="random_search"
    )
    baseline = TrialLedger.trial_id_for(**base)

    assert TrialLedger.trial_id_for(**{**base, "dataset_fingerprint": "fp2"}) != baseline
    assert TrialLedger.trial_id_for(**{**base, "partition_manifest_hash": "mh2"}) != baseline
    assert TrialLedger.trial_id_for(**{**base, "algorithm": "template_sweep"}) != baseline


def test_default_jsonl_path_respects_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    override = tmp_path / "custom_trial_ledger.jsonl"
    monkeypatch.setenv("HFT_ALPHA_MINE_TRIAL_LEDGER_PATH", str(override))
    assert _default_jsonl_path() == override


def test_candidate_id_is_identical_for_semantically_equivalent_expressions():
    a = TrialLedger.candidate_id_for("add(x, y)")
    b = TrialLedger.candidate_id_for("y + x")
    assert a == b


def test_trial_id_is_identical_for_semantically_equivalent_expressions():
    base = dict(dataset_fingerprint="fp1", partition_manifest_hash="mh1", algorithm="random_search")
    a = TrialLedger.trial_id_for(expression="add(x, y)", **base)
    b = TrialLedger.trial_id_for(expression="y + x", **base)
    assert a == b


def test_trial_id_differs_for_non_commutative_reordering():
    base = dict(dataset_fingerprint="fp1", partition_manifest_hash="mh1", algorithm="random_search")
    a = TrialLedger.trial_id_for(expression="sub(x, y)", **base)
    b = TrialLedger.trial_id_for(expression="sub(y, x)", **base)
    assert a != b


def test_malformed_expression_still_yields_a_stable_fallback_identity():
    a = TrialLedger.candidate_id_for("add(x,")
    b = TrialLedger.candidate_id_for("add(x,")
    assert a == b
    assert isinstance(a, str) and len(a) == 64
