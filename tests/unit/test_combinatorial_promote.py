from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from hft_platform.alpha.discovery import AlphaDiscoveryRegistry
from hft_platform.contracts.alpha import AlphaStatus
from research.combinatorial.ledger import TrialLedger
from research.combinatorial.promote import promote_candidate, promote_from_results

_ALPHAS_ROOT = Path("research/alphas")


@pytest.fixture()
def scratch_alpha_id():
    alpha_id = f"zz_test_gp_promote_{uuid.uuid4().hex[:8]}"
    yield alpha_id
    alpha_dir = _ALPHAS_ROOT / alpha_id
    if alpha_dir.exists():
        shutil.rmtree(alpha_dir)
    for mod_name in [m for m in sys.modules if m.startswith(f"research.alphas.{alpha_id}")]:
        del sys.modules[mod_name]


def _ledger(tmp_path: Path, name: str = "ledger.jsonl") -> TrialLedger:
    return TrialLedger(path=tmp_path / name)


def test_promote_candidate_writes_manifest_with_correct_fields(scratch_alpha_id, tmp_path) -> None:
    alpha_dir = promote_candidate(
        "ts_mean(x, 5)",
        alpha_id=scratch_alpha_id,
        owner="charlie",
        instrument="TMFD6",
        trial_ledger=_ledger(tmp_path),
    )
    manifest_data = yaml.safe_load((alpha_dir / "manifest.yaml").read_text())
    assert manifest_data["alpha_id"] == scratch_alpha_id
    assert manifest_data["formula"] == "ts_mean(x, 5)"
    assert manifest_data["dsl_formula"] is None
    assert manifest_data["data_fields"] == ["x"]
    assert manifest_data["complexity"] == "O(N)"
    assert manifest_data["status"] == "DRAFT"


def test_promote_candidate_pointwise_expression_is_o1(scratch_alpha_id, tmp_path) -> None:
    alpha_dir = promote_candidate(
        "add(x, y)",
        alpha_id=scratch_alpha_id,
        owner="charlie",
        instrument="TMFD6",
        trial_ledger=_ledger(tmp_path),
    )
    manifest_data = yaml.safe_load((alpha_dir / "manifest.yaml").read_text())
    assert manifest_data["complexity"] == "O(1)"
    assert sorted(manifest_data["data_fields"]) == ["x", "y"]


def test_promote_candidate_refuses_overwrite_without_force(scratch_alpha_id, tmp_path) -> None:
    kwargs = dict(alpha_id=scratch_alpha_id, owner="charlie", instrument="TMFD6", trial_ledger=_ledger(tmp_path))
    promote_candidate("add(x, y)", **kwargs)
    with pytest.raises(FileExistsError):
        promote_candidate("add(x, y)", **kwargs)


def test_promote_candidate_force_overwrites(scratch_alpha_id, tmp_path) -> None:
    ledger = _ledger(tmp_path)
    promote_candidate("add(x, y)", alpha_id=scratch_alpha_id, owner="charlie", instrument="TMFD6", trial_ledger=ledger)
    alpha_dir = promote_candidate(
        "mul(x, y)", alpha_id=scratch_alpha_id, owner="charlie", instrument="TMFD6", trial_ledger=ledger, force=True
    )
    manifest_data = yaml.safe_load((alpha_dir / "manifest.yaml").read_text())
    assert manifest_data["formula"] == "mul(x, y)"


def test_promote_candidate_rejects_noncausal_expression(scratch_alpha_id, tmp_path) -> None:
    with pytest.raises(ValueError):
        promote_candidate(
            "rank(x)",
            alpha_id=scratch_alpha_id,
            owner="charlie",
            instrument="TMFD6",
            trial_ledger=_ledger(tmp_path),
        )
    assert not (_ALPHAS_ROOT / scratch_alpha_id).exists()


def test_promote_candidate_lineage_empty_when_no_matching_ledger_row(scratch_alpha_id, tmp_path) -> None:
    alpha_dir = promote_candidate(
        "add(x, y)",
        alpha_id=scratch_alpha_id,
        owner="charlie",
        instrument="TMFD6",
        trial_ledger=_ledger(tmp_path, "empty_ledger.jsonl"),
    )
    readme = (alpha_dir / "README.md").read_text()
    assert "none recorded" in readme


def test_promote_candidate_lineage_recovered_from_matching_ledger_row(scratch_alpha_id, tmp_path) -> None:
    ledger = _ledger(tmp_path)
    expression = "add(x, y)"
    candidate_id = TrialLedger.candidate_id_for(expression)
    ledger.record_trial(
        trial_id="t1",
        search_run_id="run-abc",
        candidate_id=candidate_id,
        stage="search",
        status="passed",
        metrics={"score": 1.23, "selection_sharpe": 0.5, "correlation_pool_max": 0.1},
        parent_ids=("parent-1", "parent-2"),
    )
    alpha_dir = promote_candidate(
        expression,
        alpha_id=scratch_alpha_id,
        owner="charlie",
        instrument="TMFD6",
        trial_ledger=ledger,
    )
    readme = (alpha_dir / "README.md").read_text()
    assert "run-abc" in readme
    assert "parent-1" in readme
    assert "parent-2" in readme
    manifest_data = yaml.safe_load((alpha_dir / "manifest.yaml").read_text())
    assert manifest_data["experiment_metadata"]["discovery_score"] == pytest.approx(1.23)


def test_promote_from_results_selects_ranked_entry(scratch_alpha_id, tmp_path) -> None:
    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps(
            {
                "search_run_id": "run-xyz",
                "results": [
                    {
                        "expression": "ts_mean(x, 5)",
                        "score": 2.0,
                        "selection_sharpe": 1.0,
                        "correlation_pool_max": 0.2,
                        "passed": True,
                        "metadata": {"search_algorithm": "genetic_search"},
                    },
                    {
                        "expression": "add(x, y)",
                        "score": 1.0,
                        "selection_sharpe": 0.5,
                        "correlation_pool_max": 0.1,
                        "passed": True,
                        "metadata": {"search_algorithm": "random_search"},
                    },
                ],
            }
        )
    )
    alpha_dir = promote_from_results(
        results_path, rank=1, alpha_id=scratch_alpha_id, owner="charlie", instrument="TMFD6"
    )
    manifest_data = yaml.safe_load((alpha_dir / "manifest.yaml").read_text())
    assert manifest_data["formula"] == "add(x, y)"
    readme = (alpha_dir / "README.md").read_text()
    assert "random_search" in readme


def test_promote_from_results_rank_out_of_range_raises(tmp_path) -> None:
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps({"results": []}))
    with pytest.raises(IndexError):
        promote_from_results(results_path, rank=0, alpha_id="zz_unused_rank_oob", owner="charlie", instrument="TMFD6")


def test_generated_impl_round_trips_through_alpha_discovery_registry(scratch_alpha_id, tmp_path) -> None:
    promote_candidate(
        "ts_mean(x, 5)",
        alpha_id=scratch_alpha_id,
        owner="charlie",
        instrument="TMFD6",
        trial_ledger=_ledger(tmp_path),
    )
    registry = AlphaDiscoveryRegistry()
    loaded = registry.discover("research/alphas")
    assert not any(scratch_alpha_id in err for err in registry.errors), registry.errors
    assert scratch_alpha_id in loaded

    alpha = registry.get(scratch_alpha_id)
    assert alpha is not None
    assert alpha.manifest.status == AlphaStatus.DRAFT
    assert alpha.manifest.alpha_id == scratch_alpha_id

    value = alpha.update(x=1.0)
    assert isinstance(value, float)
