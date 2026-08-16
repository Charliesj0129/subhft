import json

import numpy as np
import pytest

from research.combinatorial.partitioning import (
    DatasetPartitionManager,
    LockedPartitionAccessError,
    PartitionConfigError,
)


def _session_ids(n_sessions: int = 20, rows_per_session: int = 10) -> np.ndarray:
    return np.repeat(np.arange(n_sessions), rows_per_session)


def _manager(
    tmp_path,
    *,
    embargo_rows: int = 2,
    ratios=None,
    seed: int = 42,
    symbols=("TXFD6",),
    feature_schema_version: str = "v1",
    target_definition: str = "fwd_ret_5",
) -> DatasetPartitionManager:
    return DatasetPartitionManager(
        session_ids=_session_ids(),
        dataset_fingerprint="fp-abc123",
        embargo_rows=embargo_rows,
        manifest_dir=tmp_path / "manifest",
        ratios=ratios,
        random_seed=seed,
        symbols=symbols,
        feature_schema_version=feature_schema_version,
        target_definition=target_definition,
    )


def test_contiguous_session_ratio_assignment(tmp_path):
    manager = _manager(tmp_path, embargo_rows=0)
    indices = manager.partition_indices()

    assert indices["discovery"].tolist() == list(range(0, 100))
    assert indices["selection"].tolist() == list(range(100, 150))
    assert indices["locked_validation"].tolist() == list(range(150, 180))
    assert indices["final_holdout"].tolist() == list(range(180, 200))


def test_embargo_trims_boundary_rows_with_no_overlap(tmp_path):
    manager = _manager(tmp_path, embargo_rows=2)
    indices = manager.partition_indices()

    assert indices["discovery"].tolist() == list(range(0, 98))
    assert indices["selection"].tolist() == list(range(102, 148))
    assert indices["locked_validation"].tolist() == list(range(152, 178))
    assert indices["final_holdout"].tolist() == list(range(182, 200))

    all_idx = np.concatenate(list(indices.values()))
    assert len(all_idx) == len(set(all_idx.tolist())), "post-embargo partitions must not overlap"


def test_non_contiguous_sessions_raise_partition_config_error(tmp_path):
    interleaved = np.array([0, 1, 0, 1, 2, 2, 3, 3, 3, 3] * 2)
    with pytest.raises(PartitionConfigError):
        DatasetPartitionManager(
            session_ids=interleaved,
            dataset_fingerprint="fp",
            embargo_rows=0,
            manifest_dir=tmp_path / "manifest",
        )


def test_ratios_must_sum_to_one(tmp_path):
    with pytest.raises(PartitionConfigError):
        DatasetPartitionManager(
            session_ids=_session_ids(),
            dataset_fingerprint="fp",
            embargo_rows=0,
            manifest_dir=tmp_path / "manifest",
            ratios={"discovery": 0.5, "selection": 0.5, "locked_validation": 0.5, "final_holdout": 0.5},
        )


@pytest.mark.parametrize("invalid_ratio", [0.0, -0.1, np.nan, np.inf])
def test_ratios_must_be_finite_and_strictly_positive(tmp_path, invalid_ratio):
    ratios = {"discovery": 0.50, "selection": 0.25, "locked_validation": 0.15, "final_holdout": 0.10}
    ratios["final_holdout"] = invalid_ratio

    with pytest.raises(PartitionConfigError, match="finite and strictly positive"):
        DatasetPartitionManager(
            session_ids=_session_ids(),
            dataset_fingerprint="fp",
            embargo_rows=0,
            manifest_dir=tmp_path / "manifest",
            ratios=ratios,
        )


def test_four_sessions_are_rejected_when_default_allocation_leaves_empty_holdout(tmp_path):
    with pytest.raises(PartitionConfigError, match="session allocation leaves governed partitions empty"):
        DatasetPartitionManager(
            session_ids=_session_ids(n_sessions=4),
            dataset_fingerprint="fp",
            embargo_rows=0,
            manifest_dir=tmp_path / "manifest",
        )


def test_five_sessions_create_four_nonempty_default_partitions(tmp_path):
    manager = DatasetPartitionManager(
        session_ids=_session_ids(n_sessions=5),
        dataset_fingerprint="fp",
        embargo_rows=0,
        manifest_dir=tmp_path / "manifest",
    )

    assert {name: item["session_count"] for name, item in manager.partition_summary().items()} == {
        "discovery": 2,
        "selection": 1,
        "locked_validation": 1,
        "final_holdout": 1,
    }
    assert all(item["post_embargo_row_count"] > 0 for item in manager.partition_summary().values())


def test_embargo_that_empties_partition_is_rejected(tmp_path):
    with pytest.raises(PartitionConfigError, match="embargo leaves governed partitions empty"):
        DatasetPartitionManager(
            session_ids=_session_ids(n_sessions=5),
            dataset_fingerprint="fp",
            embargo_rows=6,
            manifest_dir=tmp_path / "manifest",
        )


def test_get_rows_locked_partition_denied_before_freeze_and_granted_after(tmp_path):
    manager = _manager(tmp_path)
    features = {"x": np.arange(200, dtype=np.float64)}

    # Discovery/selection are open by default.
    open_rows = manager.get_rows("discovery", features)
    assert open_rows["x"].size == 98

    with pytest.raises(LockedPartitionAccessError):
        manager.get_rows("locked_validation", features)

    candidate_id = "candidate-1"
    assert manager.freeze_candidate(candidate_id) is True

    locked_rows = manager.get_rows("locked_validation", features, frozen_candidate_id=candidate_id)
    assert locked_rows["x"].size == 26

    # A different, unfrozen candidate is still denied.
    with pytest.raises(LockedPartitionAccessError):
        manager.get_rows("final_holdout", features, frozen_candidate_id="candidate-2")


def test_every_locked_access_attempt_is_audit_logged(tmp_path):
    manager = _manager(tmp_path)
    features = {"x": np.arange(200, dtype=np.float64)}

    with pytest.raises(LockedPartitionAccessError):
        manager.get_rows("locked_validation", features)

    candidate_id = "candidate-1"
    manager.freeze_candidate(candidate_id)
    manager.get_rows("locked_validation", features, frozen_candidate_id=candidate_id)

    log_path = tmp_path / "manifest" / "locked_access_log.jsonl"
    assert log_path.exists()
    rows = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 2
    assert rows[0]["granted"] is False
    assert rows[0]["partition"] == "locked_validation"
    assert rows[1]["granted"] is True
    assert rows[1]["frozen_candidate_id"] == candidate_id


def test_freeze_candidate_is_idempotent_on_resume(tmp_path):
    manager = _manager(tmp_path)
    assert manager.freeze_candidate("candidate-1") is True
    assert manager.freeze_candidate("candidate-1") is False

    frozen_path = tmp_path / "manifest" / "frozen_candidates.jsonl"
    rows = [line for line in frozen_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1

    # A fresh manager instance pointed at the same manifest_dir sees the freeze too.
    manager2 = _manager(tmp_path)
    assert manager2.freeze_candidate("candidate-1") is False


def test_write_manifest_round_trips_required_fields(tmp_path):
    manager = _manager(tmp_path)
    manifest_path = manager.write_manifest(tmp_path / "manifest" / "partition_manifest.json")
    payload = json.loads(open(manifest_path).read())

    for key in (
        "manifest_schema_version",
        "manifest_hash",
        "dataset_fingerprint",
        "symbols",
        "ratios",
        "embargo_rows",
        "random_seed",
        "feature_schema_version",
        "target_definition",
        "created_at_ns",
        "sessions",
        "session_assignment",
        "partitions",
    ):
        assert key in payload, f"missing required manifest field: {key}"

    assert payload["manifest_hash"] == manager.manifest_hash
    assert payload["dataset_fingerprint"] == "fp-abc123"
    assert payload["symbols"] == ["TXFD6"]
    assert payload["feature_schema_version"] == "v1"
    assert payload["target_definition"] == "fwd_ret_5"
    assert len(payload["sessions"]) == 20


def test_write_manifest_is_idempotent_on_resume(tmp_path):
    manager = _manager(tmp_path)
    path = tmp_path / "manifest" / "partition_manifest.json"
    first = manager.write_manifest(path)
    second = manager.write_manifest(path)
    assert first == second


def test_write_manifest_rejects_overwrite_with_different_content(tmp_path):
    manager_a = _manager(tmp_path, embargo_rows=2)
    path = tmp_path / "manifest" / "partition_manifest.json"
    manager_a.write_manifest(path)

    manager_b = _manager(tmp_path, embargo_rows=5)
    with pytest.raises(PartitionConfigError):
        manager_b.write_manifest(path)


def test_manifest_hash_deterministic_and_sensitive_to_embargo(tmp_path):
    manager_a = _manager(tmp_path, embargo_rows=2, seed=1)
    manager_b = _manager(tmp_path, embargo_rows=2, seed=1)
    manager_c = _manager(tmp_path, embargo_rows=3, seed=1)

    assert manager_a.manifest_hash == manager_b.manifest_hash
    assert manager_a.manifest_hash != manager_c.manifest_hash


@pytest.mark.parametrize(
    "differing",
    [
        {"symbols": ("MXFD6",)},
        {"feature_schema_version": "v2"},
        {"target_definition": "fwd_ret_30"},
    ],
)
def test_manifest_hash_is_sensitive_to_semantic_fields(tmp_path, differing):
    """These three describe *what was mined*, not just how rows were split.

    Before ``MANIFEST_SCHEMA_VERSION`` 2 they were written into the manifest but
    left out of its hash, so a second ``mine init`` differing only in ``--symbols``
    produced an identical hash.
    """
    baseline = _manager(tmp_path)
    other = _manager(tmp_path, **differing)

    assert baseline.manifest_hash != other.manifest_hash


@pytest.mark.parametrize(
    "differing",
    [
        {"symbols": ("MXFD6",)},
        {"feature_schema_version": "v2"},
        {"target_definition": "fwd_ret_30"},
    ],
)
def test_write_manifest_rejects_a_rerun_that_changes_a_semantic_field(tmp_path, differing):
    path = tmp_path / "manifest" / "partition_manifest.json"
    _manager(tmp_path).write_manifest(path)

    with pytest.raises(PartitionConfigError, match="immutable"):
        _manager(tmp_path, **differing).write_manifest(path)


def test_manifest_written_before_schema_version_2_still_resumes(tmp_path):
    """An in-flight run must not abort just because this file's hash changed."""
    from research.combinatorial import partitioning as partitioning_module

    manager = _manager(tmp_path)
    path = tmp_path / "manifest" / "partition_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_payload = {
        "manifest_hash": manager._legacy_manifest_hash,
        "dataset_fingerprint": manager.dataset_fingerprint,
    }
    path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    assert manager.write_manifest(path) == str(path)
    # Left byte-for-byte alone: rewriting it would be the mutation the
    # immutability rule exists to prevent.
    assert json.loads(path.read_text(encoding="utf-8")) == legacy_payload
    assert manager.manifest_hash != manager._legacy_manifest_hash
    assert partitioning_module.MANIFEST_SCHEMA_VERSION == 2


def test_manifest_written_before_schema_version_2_still_rejects_a_different_partitioning(tmp_path):
    path = tmp_path / "manifest" / "partition_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"manifest_hash": "some-other-run"}), encoding="utf-8")

    with pytest.raises(PartitionConfigError, match="immutable"):
        _manager(tmp_path).write_manifest(path)
