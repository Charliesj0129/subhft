"""Slice-D Task 4: kill_ledger writer + idempotency tests.

The kill ledger has two parallel sinks:
  - ClickHouse table ``audit.alpha_kill_ledger`` (durable).
  - JSON-lines file ``research/alphas/_kill_ledger.jsonl`` (offline fallback).

Both must dedupe on ``(alpha_id, kill_id)`` where
``kill_id = sha256(alpha_id || ':' || gate || ':' || stable_artifact_hash)``.

CH is exercised via a fake client injected through monkeypatch on
``audit._get_client``; the fake records inserts in-memory and replays the
SELECT count() pre-check semantics.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hft_platform.alpha import audit, kill_ledger
from hft_platform.alpha.kill_ledger import (
    KillRecord,
    append_kill,
    latest_reason,
    read_kills,
    stable_artifact_hash,
)
from research.registry.schemas import AlphaManifest, AlphaStatus


def _make_record(**overrides: Any) -> KillRecord:
    base: dict[str, Any] = dict(
        alpha_id="test_alpha",
        gate="C",
        reason="failed gate C: invalid_data",
        stable_artifact_hash="hash_abc",
        scorecard_id="sc_001",
        killed_at=1_700_000_000_000_000_000,
    )
    base.update(overrides)
    return KillRecord(**base)


def _make_manifest(**overrides: Any) -> AlphaManifest:
    base: dict[str, Any] = dict(
        alpha_id="test_alpha",
        hypothesis="h",
        formula="x",
        paper_refs=("1234.5678",),
        data_fields=("feature[0]",),
        complexity="O(1)",
        status=AlphaStatus.DRAFT,
    )
    base.update(overrides)
    return AlphaManifest(**base)


@pytest.fixture(autouse=True)
def _isolated_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    jsonl = tmp_path / "_kill_ledger.jsonl"
    monkeypatch.setenv("HFT_ALPHA_KILL_LEDGER_PATH", str(jsonl))
    kill_ledger._reset_cache_for_tests()
    return jsonl


@pytest.fixture()
def _no_ch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the jsonl path by ensuring CH-enabled audit is off."""
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "0")
    audit._ENABLED = None  # noqa: SLF001 — re-read env on next call


def test_kill_id_deterministic() -> None:
    a = _make_record()
    b = _make_record()
    assert a.kill_id() == b.kill_id()


def test_kill_id_changes_with_gate() -> None:
    a = _make_record(gate="C")
    b = _make_record(gate="D")
    assert a.kill_id() != b.kill_id()


def test_kill_id_changes_with_alpha_id() -> None:
    a = _make_record(alpha_id="alpha_x")
    b = _make_record(alpha_id="alpha_y")
    assert a.kill_id() != b.kill_id()


def test_kill_id_changes_with_stable_artifact_hash() -> None:
    a = _make_record(stable_artifact_hash="h1")
    b = _make_record(stable_artifact_hash="h2")
    assert a.kill_id() != b.kill_id()


def test_kill_id_invariant_under_reason_change() -> None:
    """Reason is per-attempt narrative; same kill_id must dedupe regardless."""
    a = _make_record(reason="reason A")
    b = _make_record(reason="completely different reason text")
    assert a.kill_id() == b.kill_id()


def test_invalid_gate_rejected() -> None:
    with pytest.raises(ValueError, match="gate must be one of"):
        _make_record(gate="Z")


def test_empty_alpha_id_rejected() -> None:
    with pytest.raises(ValueError, match="alpha_id must be non-empty"):
        _make_record(alpha_id="")


def test_empty_reason_rejected() -> None:
    with pytest.raises(ValueError, match="reason must be non-empty"):
        _make_record(reason="")


def test_stable_artifact_hash_deterministic() -> None:
    m1 = _make_manifest()
    m2 = _make_manifest()
    assert stable_artifact_hash(m1) == stable_artifact_hash(m2)


def test_stable_artifact_hash_differs_when_intrinsic_changes() -> None:
    m1 = _make_manifest(formula="x")
    m2 = _make_manifest(formula="y")
    assert stable_artifact_hash(m1) != stable_artifact_hash(m2)


def test_stable_artifact_hash_excludes_run_outcome_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive check: even if a future schema mutation re-adds kill_reason or
    cluster_id to the manifest dict, the hash must stay invariant under them.
    """
    m = _make_manifest()
    base_hash = stable_artifact_hash(m)

    original_to_dict = AlphaManifest.to_dict

    def _to_dict_with_run_outcomes(self: AlphaManifest) -> dict[str, Any]:
        d = original_to_dict(self)
        d["kill_reason"] = "some run-specific reason"
        d["cluster_id"] = "cluster_42"
        return d

    monkeypatch.setattr(AlphaManifest, "to_dict", _to_dict_with_run_outcomes)
    new_hash = stable_artifact_hash(m)
    assert base_hash == new_hash, "kill_reason/cluster_id must be excluded from stable_artifact_hash"


def test_jsonl_first_append_returns_true(_no_ch: None, _isolated_jsonl: Path) -> None:
    record = _make_record()
    assert append_kill(record) is True
    assert _isolated_jsonl.exists()


def test_jsonl_duplicate_append_returns_false(_no_ch: None, _isolated_jsonl: Path) -> None:
    record = _make_record()
    assert append_kill(record) is True
    assert append_kill(record) is False
    rows = _isolated_jsonl.read_text().splitlines()
    assert len(rows) == 1, f"expected 1 row, got {rows}"


def test_jsonl_different_gate_inserts_both(_no_ch: None, _isolated_jsonl: Path) -> None:
    rec_c = _make_record(gate="C")
    rec_d = _make_record(gate="D")
    assert append_kill(rec_c) is True
    assert append_kill(rec_d) is True
    rows = [json.loads(line) for line in _isolated_jsonl.read_text().splitlines()]
    assert len(rows) == 2
    gates = {r["gate"] for r in rows}
    assert gates == {"C", "D"}


def test_jsonl_dedupe_warms_cache_from_file(_no_ch: None, _isolated_jsonl: Path) -> None:
    """Even after a process restart (cache reset), a second call must dedupe
    against on-disk state."""
    record = _make_record()
    assert append_kill(record) is True
    kill_ledger._reset_cache_for_tests()
    assert append_kill(record) is False


def test_jsonl_killed_at_zero_fills_in_now(_no_ch: None, _isolated_jsonl: Path) -> None:
    record = _make_record(killed_at=0)
    assert append_kill(record) is True
    rows = [json.loads(line) for line in _isolated_jsonl.read_text().splitlines()]
    assert rows[0]["killed_at"] > 0


def test_jsonl_kill_id_persisted(_no_ch: None, _isolated_jsonl: Path) -> None:
    record = _make_record()
    append_kill(record)
    rows = [json.loads(line) for line in _isolated_jsonl.read_text().splitlines()]
    assert rows[0]["kill_id"] == record.kill_id()


def test_read_kills_filters_by_alpha_id(_no_ch: None) -> None:
    append_kill(_make_record(alpha_id="alpha_a", gate="C"))
    append_kill(_make_record(alpha_id="alpha_b", gate="D"))
    a = read_kills(alpha_id="alpha_a")
    b = read_kills(alpha_id="alpha_b")
    assert len(a) == 1 and a[0].alpha_id == "alpha_a"
    assert len(b) == 1 and b[0].alpha_id == "alpha_b"


def test_read_kills_returns_empty_for_unknown(_no_ch: None) -> None:
    assert read_kills(alpha_id="never_killed") == []


def test_latest_reason_returns_most_recent(_no_ch: None) -> None:
    append_kill(_make_record(alpha_id="alpha_a", gate="C", reason="first kill"))
    append_kill(_make_record(alpha_id="alpha_a", gate="D", reason="second kill"))
    assert latest_reason("alpha_a") == "second kill"


def test_latest_reason_returns_none_for_unknown(_no_ch: None) -> None:
    assert latest_reason("never_killed") is None


class _FakeQueryResult:
    def __init__(self, rows: list[list[Any]]):
        self.result_rows = rows


class _FakeCHClient:
    def __init__(self) -> None:
        self.inserts: list[dict[str, Any]] = []
        self.existing: dict[tuple[str, str], int] = {}

    def query(self, sql: str, parameters: dict[str, Any]) -> _FakeQueryResult:
        if "WHERE alpha_id" in sql:
            cnt = self.existing.get((parameters["a"], parameters["k"]), 0)
            return _FakeQueryResult([[cnt]])
        return _FakeQueryResult([])

    def insert(self, table: str, rows: list[list[Any]], column_names: list[str]) -> None:
        for row in rows:
            payload = dict(zip(column_names, row, strict=True))
            self.inserts.append(payload)
            self.existing[(payload["alpha_id"], payload["kill_id"])] = (
                self.existing.get((payload["alpha_id"], payload["kill_id"]), 0) + 1
            )


@pytest.fixture()
def _fake_ch(monkeypatch: pytest.MonkeyPatch) -> _FakeCHClient:
    fake = _FakeCHClient()
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "1")
    audit._ENABLED = None  # noqa: SLF001
    monkeypatch.setattr(audit, "_get_client", lambda: fake)
    return fake


def test_ch_first_append_inserts(_fake_ch: _FakeCHClient) -> None:
    record = _make_record()
    assert append_kill(record) is True
    assert len(_fake_ch.inserts) == 1
    assert _fake_ch.inserts[0]["alpha_id"] == record.alpha_id
    assert _fake_ch.inserts[0]["kill_id"] == record.kill_id()


def test_ch_duplicate_append_returns_false_no_insert(_fake_ch: _FakeCHClient) -> None:
    record = _make_record()
    assert append_kill(record) is True
    assert append_kill(record) is False
    assert len(_fake_ch.inserts) == 1


def test_ch_failure_falls_back_to_jsonl(monkeypatch: pytest.MonkeyPatch, _isolated_jsonl: Path) -> None:
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "1")
    audit._ENABLED = None  # noqa: SLF001

    def _raise_client() -> Any:
        raise RuntimeError("CH unreachable")

    monkeypatch.setattr(audit, "_get_client", _raise_client)
    record = _make_record()
    assert append_kill(record) is True
    assert _isolated_jsonl.exists()
    rows = _isolated_jsonl.read_text().splitlines()
    assert len(rows) == 1


class TestKillRecordFromBlocking:
    """Guard: triage_status='sample_*' must not be loggable as a KILL.

    Goal 限制 §3 / 驗證標準 §4 forbid marking sample-insufficient runs
    complete or dead; the hypothesis is still alive pending more data.
    """

    def _blocking(self, *, status: str, reasons: list[str], passed: bool = False) -> dict:
        return {
            "passed": passed,
            "failing": [{"name": r, "passed": False, "metrics": {}, "details": ""} for r in reasons],
            "names": reasons,
            "profile": "test_strict",
            "triage_status": status,
            "triage_reasons": reasons,
        }

    def test_killed_status_builds_record_with_pipe_separated_reasons(self) -> None:
        blocking = self._blocking(
            status="killed",
            reasons=["min_sample_size", "single_day_dominance"],
        )
        rec = KillRecord.from_blocking(alpha_id="c99", gate="C", blocking=blocking, stable_artifact_hash="h")
        assert rec.alpha_id == "c99"
        assert rec.gate == "C"
        assert rec.reason == "min_sample_size|single_day_dominance"

    def test_sample_promising_rejected(self) -> None:
        blocking = self._blocking(status="sample_promising", reasons=["min_sample_size"])
        with pytest.raises(kill_ledger.SampleInsufficientKillError, match="sample_promising"):
            KillRecord.from_blocking(alpha_id="c99", gate="C", blocking=blocking)

    def test_sample_needs_more_sample_rejected(self) -> None:
        blocking = self._blocking(status="sample_needs_more_sample", reasons=["min_sample_size"])
        with pytest.raises(kill_ledger.SampleInsufficientKillError):
            KillRecord.from_blocking(alpha_id="c99", gate="C", blocking=blocking)

    def test_sample_inconclusive_rejected(self) -> None:
        blocking = self._blocking(status="sample_inconclusive", reasons=["min_sample_size"])
        with pytest.raises(kill_ledger.SampleInsufficientKillError):
            KillRecord.from_blocking(alpha_id="c99", gate="C", blocking=blocking)

    def test_passed_blocking_rejected(self) -> None:
        # Defensive: if the caller hands us a blocking dict that actually
        # passed, refuse to write a kill — there's nothing to kill.
        blocking = self._blocking(status="passed", reasons=[], passed=True)
        with pytest.raises(ValueError, match="passed=True"):
            KillRecord.from_blocking(alpha_id="c99", gate="C", blocking=blocking)

    def test_empty_reasons_rejected(self) -> None:
        blocking = {
            "passed": False,
            "failing": [],
            "names": [],
            "profile": "test_strict",
            "triage_status": "killed",
            "triage_reasons": [],
        }
        with pytest.raises(ValueError, match="no failing gates"):
            KillRecord.from_blocking(alpha_id="c99", gate="C", blocking=blocking)

    def test_falls_back_to_failing_names_when_triage_reasons_absent(self) -> None:
        # Backward-compat: older blocking dicts (pre-Round-6) won't carry
        # triage_reasons; build the reason string from failing[] instead.
        blocking = {
            "passed": False,
            "failing": [
                {"name": "edge_per_round_trip", "passed": False, "metrics": {}, "details": ""},
            ],
            "names": ["edge_per_round_trip"],
            "profile": "legacy",
            "triage_status": "killed",
        }
        rec = KillRecord.from_blocking(alpha_id="c99", gate="C", blocking=blocking, stable_artifact_hash="h")
        assert rec.reason == "edge_per_round_trip"

    def test_non_dict_blocking_rejected(self) -> None:
        with pytest.raises(TypeError):
            KillRecord.from_blocking(alpha_id="c99", gate="C", blocking="not a dict")  # type: ignore[arg-type]
