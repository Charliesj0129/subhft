"""Tests for CE3-04: ReplayContract + validate_replay_preconditions()."""
from unittest.mock import MagicMock

import pytest

from hft_platform.recorder.replay_contract import ReplayContract, validate_replay_preconditions


def _make_loader(
    strict_order: bool = False,
    dedup_enabled: bool = False,
    manifest_enabled: bool = True,
    archive_dir: str = ".wal/archive",
    ch_client=None,
):
    loader = MagicMock()
    loader._strict_order = strict_order
    loader._dedup_enabled = dedup_enabled
    loader._manifest_enabled = manifest_enabled
    loader.archive_dir = archive_dir
    loader.ch_client = ch_client
    return loader


def test_default_config_no_violations():
    loader = _make_loader()
    violations = validate_replay_preconditions(loader)
    assert violations == []


def test_strict_order_without_manifest_violates():
    loader = _make_loader(strict_order=True, manifest_enabled=False)
    violations = validate_replay_preconditions(loader)
    assert any("manifest" in v.lower() for v in violations)


def test_strict_order_with_manifest_ok():
    loader = _make_loader(strict_order=True, manifest_enabled=True)
    violations = validate_replay_preconditions(loader)
    assert violations == []


def test_dedup_without_client_violates():
    loader = _make_loader(dedup_enabled=True, ch_client=None)
    violations = validate_replay_preconditions(loader)
    assert any("dedup" in v.lower() or "clickhouse" in v.lower() for v in violations)


def test_dedup_with_client_ok():
    loader = _make_loader(dedup_enabled=True, ch_client=MagicMock())
    violations = validate_replay_preconditions(loader)
    assert violations == []


def test_no_archive_dir_violates():
    loader = _make_loader(archive_dir=None)
    violations = validate_replay_preconditions(loader)
    assert any("archive" in v.lower() for v in violations)


def test_multiple_violations():
    loader = _make_loader(strict_order=True, manifest_enabled=False, dedup_enabled=True, ch_client=None, archive_dir=None)
    violations = validate_replay_preconditions(loader)
    assert len(violations) >= 3


def test_replay_contract_defaults():
    rc = ReplayContract()
    assert rc.file_ordering == "best_effort"
    assert rc.dedup_enabled is False
    assert rc.manifest_enabled is True
    assert rc.require_archive_on_success is True
