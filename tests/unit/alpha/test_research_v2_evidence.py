"""Research v2 PR5b: evidence root, sealed runs, the verdict ledger, governed runs (ADR-005 section 6)."""

from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from hft_platform.alpha import validation
from hft_platform.alpha._validation_types import GateReport, ValidationConfig
from hft_platform.alpha.evidence import (
    DATA_ROOT_ENV,
    EVIDENCE_ROOT_ENV,
    EvidenceRootError,
    EvidenceSealedError,
    EvidenceWriter,
    GovernedRunRefused,
    RunMode,
    data_root,
    evidence_root,
    governed_refusals,
)
from hft_platform.alpha.experiments import ExperimentTracker
from hft_platform.alpha.paths import repo_root
from hft_platform.alpha.provenance import RUN_STAMP_SCHEMA, build_run_stamp

# --- roots ------------------------------------------------------------------


@pytest.mark.parametrize(("env", "resolve"), [(EVIDENCE_ROOT_ENV, evidence_root), (DATA_ROOT_ENV, data_root)])
def test_root_required_but_unset_is_refused(monkeypatch: pytest.MonkeyPatch, env, resolve) -> None:
    monkeypatch.delenv(env, raising=False)
    with pytest.raises(EvidenceRootError, match="not set"):
        resolve(required=True)


@pytest.mark.parametrize(("env", "resolve"), [(EVIDENCE_ROOT_ENV, evidence_root), (DATA_ROOT_ENV, data_root)])
def test_root_inside_the_repository_is_refused(monkeypatch: pytest.MonkeyPatch, env, resolve) -> None:
    monkeypatch.setenv(env, str(repo_root() / "research" / "experiments"))
    with pytest.raises(EvidenceRootError, match="inside the repository"):
        resolve()


def test_root_relative_value_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EVIDENCE_ROOT_ENV, "evidence")
    with pytest.raises(EvidenceRootError, match="relative"):
        evidence_root(required=True)


def test_root_default_applies_only_when_not_required(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(EVIDENCE_ROOT_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert evidence_root() == (tmp_path / "hft_research_evidence").resolve()
    assert data_root() == (tmp_path / "hft_research_data").resolve()


def test_root_is_read_at_call_time_and_is_cwd_independent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(EVIDENCE_ROOT_ENV, str(tmp_path / "ev"))
    first = evidence_root(required=True)
    monkeypatch.chdir(tmp_path)
    assert evidence_root(required=True) == first == (tmp_path / "ev").resolve()


def test_governed_refusals_name_every_failed_precondition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(EVIDENCE_ROOT_ENV, raising=False)
    reasons = governed_refusals(git_commit=None, git_dirty=True, profile_name=None)
    assert [r.split(":")[0] for r in reasons] == [
        "evidence_root",
        "git_commit_unknown",
        "dirty_tree",
        "profile_unresolved",
    ]
    assert "git_state_unknown" in governed_refusals(git_commit="c", git_dirty=None, profile_name="p")


def test_governed_refusals_empty_when_every_precondition_holds(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(EVIDENCE_ROOT_ENV, str(tmp_path))
    assert governed_refusals(git_commit="c", git_dirty=False, profile_name="p") == []


# --- sealing ----------------------------------------------------------------


def _run_dir(root: Path, name: str = "run-1") -> Path:
    d = root / "runs" / name
    (d / "sub").mkdir(parents=True)
    (d / "meta.json").write_text('{"a": 1}', encoding="utf-8")
    (d / "sub" / "equity.npy").write_bytes(b"\x00\x01")
    return d


def test_seal_lists_every_file_and_refuses_a_second_seal(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    run = _run_dir(tmp_path)
    manifest = json.loads(writer.seal(run, alpha_id="a1", mode=RunMode.GOVERNED).read_text())
    assert manifest["mode"] == "governed"
    assert sorted(manifest["files"]) == ["meta.json", "sub/equity.npy"]
    assert EvidenceWriter.verify(run) == []
    with pytest.raises(EvidenceSealedError):
        writer.seal(run, alpha_id="a1", mode=RunMode.GOVERNED)


def test_verify_reports_changed_missing_and_added_files(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    run = _run_dir(tmp_path)
    writer.seal(run, alpha_id="a1", mode=RunMode.GOVERNED)
    (run / "meta.json").write_text('{"a": 2}', encoding="utf-8")
    (run / "sub" / "equity.npy").unlink()
    (run / "late.json").write_text("{}", encoding="utf-8")
    assert EvidenceWriter.verify(run) == ["meta.json", "sub/equity.npy", "late.json"]


def test_seal_outside_the_root_is_refused(tmp_path: Path) -> None:
    outside = _run_dir(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        EvidenceWriter(tmp_path / "root").seal(outside, alpha_id="a1", mode=RunMode.GOVERNED)


def test_seal_hashes_do_not_depend_on_the_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    monkeypatch.chdir(tmp_path)
    first = json.loads(writer.seal(_run_dir(tmp_path, "r1"), alpha_id="a", mode=RunMode.GOVERNED).read_text())
    monkeypatch.chdir(tmp_path / "runs")
    second = json.loads(writer.seal(_run_dir(tmp_path, "r2"), alpha_id="a", mode=RunMode.GOVERNED).read_text())
    assert first["files"] == second["files"]


def test_log_run_refuses_to_rewrite_a_sealed_run(tmp_path: Path) -> None:
    tracker = ExperimentTracker(base_dir=tmp_path)
    kwargs = dict(
        run_id="r1",
        alpha_id="a1",
        config_hash="h",
        data_paths=[],
        metrics={},
        gate_status={},
        scorecard_payload={},
        backtest_report_payload={},
        run_stamp={},
    )
    meta = tracker.log_run(**kwargs)
    EvidenceWriter(tmp_path).seal(meta.parent, alpha_id="a1", mode=RunMode.GOVERNED)
    with pytest.raises(FileExistsError, match="sealed"):
        tracker.log_run(**kwargs)


# --- ledger -----------------------------------------------------------------


def test_ledger_appends_governed_events_in_order(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    writer.append_verdict({"alpha_id": "a1", "stage": "gate_a", "passed": True}, mode=RunMode.GOVERNED)
    writer.append_verdict(
        {"alpha_id": "a1", "stage": "gate_c", "passed": False, "verdict": "KILL"}, mode=RunMode.GOVERNED
    )
    events = list(writer.read_verdicts())
    assert [(e["stage"], e["mode"]) for e in events] == [("gate_a", "governed"), ("gate_c", "governed")]
    assert len({e["event_id"] for e in events}) == 2
    assert len(writer.ledger_path.read_text().splitlines()) == 2


def test_ledger_refuses_exploratory_and_incomplete_events(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    with pytest.raises(ValueError, match="only governed"):
        writer.append_verdict({"alpha_id": "a1", "stage": "gate_a", "passed": True}, mode=RunMode.EXPLORATORY)
    with pytest.raises(ValueError, match="passed"):
        writer.append_verdict({"alpha_id": "a1", "stage": "gate_a"}, mode=RunMode.GOVERNED)
    assert not writer.ledger_path.exists()


def test_ledger_malformed_line_is_an_error_not_skipped(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    writer.append_verdict({"alpha_id": "a1", "stage": "gate_a", "passed": True}, mode=RunMode.GOVERNED)
    with writer.ledger_path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    with pytest.raises(ValueError, match=":2: not JSON"):
        list(writer.read_verdicts())


# --- RunStamp v1 -------------------------------------------------------------


def test_run_stamp_v1_records_mode_rules_engines_protocol_and_calibration(tmp_path: Path) -> None:
    data = tmp_path / "d.npz"
    data.write_bytes(b"npz")
    stamp = build_run_stamp(
        data_paths=[str(data)],
        config_hash="h",
        profile="vm_ul6_strict_v2",
        mode="governed",
        protocol_sha256="ab" * 32,
        calibration="uncalibrated",
    )
    assert stamp["schema"] == RUN_STAMP_SCHEMA == "run_stamp.v1"
    assert stamp["mode"] == "governed"
    assert stamp["verdict_rules_version"] == "v1"
    assert stamp["engine_versions"]["python"]
    assert {"hftbacktest", "numpy", "numba"} <= set(stamp["engine_versions"])
    assert stamp["protocol_sha256"] == "ab" * 32
    assert stamp["calibration"] == "uncalibrated"
    assert stamp["datasets"][str(data)] is not None


# --- governed validation ----------------------------------------------------


def _report(gate: str, passed: bool, **details) -> GateReport:
    return GateReport(gate=gate, passed=passed, details=dict(details))


@pytest.fixture
def governed_validation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """run_alpha_validation with stubbed gates, a clean tree and an evidence root."""
    root = tmp_path / "evidence"
    monkeypatch.setenv(EVIDENCE_ROOT_ENV, str(root))
    state = {"git": ("c0ffee", False), "stamp_dirty": False, "status_updates": [], "gate_a_calls": 0}
    monkeypatch.setattr(validation, "git_state", lambda _root: state["git"])
    alpha = types.SimpleNamespace(manifest=types.SimpleNamespace(alpha_id="a1"))
    registry = types.SimpleNamespace(discover=lambda _path: {"a1": alpha})
    monkeypatch.setattr("hft_platform.alpha.discovery.AlphaDiscoveryRegistry", lambda: registry)

    def _gate_a(*_a, **_k):
        state["gate_a_calls"] += 1
        return _report("Gate A", True)

    def _gate_c(_alpha, _config, _root, _data, experiments_base):
        tracker = ExperimentTracker(base_dir=experiments_base)
        meta = tracker.log_run(
            run_id="run-1",
            alpha_id="a1",
            config_hash="h",
            data_paths=[],
            metrics={},
            gate_status={},
            scorecard_payload={},
            backtest_report_payload={},
            run_stamp={"git_commit": "c0ffee", "git_dirty": state["stamp_dirty"]},
        )
        report = _report("Gate C", False, verdict={"verdict": "NEEDS-MORE-DAYS"})
        return report, "run-1", "h", str(meta.parent / "scorecard.json"), str(meta)

    monkeypatch.setattr(validation, "run_gate_a", _gate_a)
    monkeypatch.setattr(validation, "run_gate_b", lambda **_k: _report("Gate B", True))
    monkeypatch.setattr(validation, "run_gate_c", _gate_c)
    monkeypatch.setattr(
        validation, "_update_manifest_status", lambda alpha_id, status, _root: state["status_updates"].append(status)
    )

    def _run(**overrides):
        cfg = {
            "alpha_id": "a1",
            "data_paths": [],
            "project_root": str(tmp_path),
            "experiments_dir": str(root),
            "run_mode": "governed",
            "profile": types.SimpleNamespace(name="vm_ul6_strict_v2"),
        }
        cfg.update(overrides)
        return validation.run_alpha_validation(ValidationConfig(**cfg))

    return types.SimpleNamespace(run=_run, root=root, state=state)


def test_governed_validation_seals_evidence_and_appends_one_event_per_gate(governed_validation) -> None:
    governed_validation.run()

    writer = EvidenceWriter(governed_validation.root)
    events = list(writer.read_verdicts())
    assert [(e["stage"], e["passed"]) for e in events] == [("gate_a", True), ("gate_b", True), ("gate_c", False)]
    assert events[2]["verdict"] == "NEEDS-MORE-DAYS"
    assert all(e["git_commit"] == "c0ffee" and e["profile"] == "vm_ul6_strict_v2" for e in events)
    manifests = sorted(events[0]["evidence"])
    assert manifests[0] == "runs/run-1/manifest.json"
    assert manifests[1].startswith("validations/a1/") and manifests[1].endswith("/manifest.json")
    assert EvidenceWriter.verify(governed_validation.root / "runs" / "run-1") == []
    # Governed runs never rewrite impl.py; status comes from the ledger.
    assert governed_validation.state["status_updates"] == []


def test_exploratory_validation_keeps_legacy_status_and_writes_no_ledger(governed_validation) -> None:
    governed_validation.run(run_mode="exploratory")
    assert governed_validation.state["status_updates"] == ["GATE_A", "GATE_B"]
    assert not EvidenceWriter(governed_validation.root).ledger_path.exists()


def test_governed_validation_on_a_dirty_tree_is_refused_before_any_gate(governed_validation) -> None:
    governed_validation.state["git"] = ("c0ffee", True)
    with pytest.raises(GovernedRunRefused, match="dirty_tree"):
        governed_validation.run()
    assert governed_validation.state["gate_a_calls"] == 0


def test_governed_validation_without_a_profile_is_refused(governed_validation) -> None:
    with pytest.raises(GovernedRunRefused, match="profile_unresolved"):
        governed_validation.run(profile=None)


def test_governed_validation_outside_the_evidence_root_is_refused(governed_validation, tmp_path: Path) -> None:
    with pytest.raises(GovernedRunRefused, match="not the evidence root"):
        governed_validation.run(experiments_dir=str(tmp_path / "other"))


def test_governed_validation_whose_gate_c_stamp_is_dirty_reaches_no_ledger(governed_validation) -> None:
    governed_validation.state["stamp_dirty"] = True
    with pytest.raises(GovernedRunRefused, match="same clean commit"):
        governed_validation.run()
    assert not EvidenceWriter(governed_validation.root).ledger_path.exists()


def test_validation_unknown_run_mode_is_an_error(governed_validation) -> None:
    with pytest.raises(ValueError, match="governd"):
        governed_validation.run(run_mode="governd")


def test_governed_gate_c_unreadable_verdict_is_recorded_invalid() -> None:
    verdict, reason = validation._gate_c_ledger_verdict(_report("Gate C", False))
    assert verdict == "INVALID"
    assert reason is not None


# --- strict pipeline --------------------------------------------------------


def test_strict_pipeline_without_evidence_root_stops_before_any_work(monkeypatch, tmp_path: Path) -> None:
    from research import pipeline

    def _never(*_a, **_k):
        raise AssertionError("no work may start for a refused governed run")

    monkeypatch.delenv(EVIDENCE_ROOT_ENV, raising=False)
    monkeypatch.setattr(pipeline, "_run_factory_optimize", _never)
    monkeypatch.setattr(pipeline, "run_alpha_validation", _never)
    args = pipeline.build_parser().parse_args(
        ["run", "--alpha-id", "a", "--owner", "o", "--data", "d.npy", "--out-dir", str(tmp_path)]
    )

    assert pipeline._run_pipeline(args, mode="strict") == 2
    report = json.loads(next(tmp_path.glob("*_pipeline_report.json")).read_text())
    assert any(EVIDENCE_ROOT_ENV in note and "triage mode" in note for note in report["notes"])


def test_strict_pipeline_explicit_experiments_dir_must_be_the_evidence_root(
    monkeypatch, tmp_path: Path, governed_env: Path
) -> None:
    from research import pipeline

    monkeypatch.setattr(pipeline, "_run_factory_optimize", lambda *a, **k: 0)
    args = pipeline.build_parser().parse_args(
        ["run", "--alpha-id", "a", "--owner", "o", "--data", "d.npy", "--out-dir", str(tmp_path)]
        + ["--experiments-dir", str(tmp_path / "elsewhere")]
    )

    assert pipeline._run_pipeline(args, mode="strict") == 2
    report = json.loads(next(tmp_path.glob("*_pipeline_report.json")).read_text())
    assert any("differs from the evidence root" in note for note in report["notes"])


def test_ledger_write_is_a_single_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[bytes] = []
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(data) or real_write(fd, data))
    EvidenceWriter(tmp_path).append_verdict({"alpha_id": "a", "stage": "gate_a", "passed": True}, mode="governed")
    assert len(writes) == 1 and writes[0].endswith(b"\n")


def test_governed_validation_whose_gate_c_meta_has_no_stamp_reaches_no_ledger(
    governed_validation, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_log_run = ExperimentTracker.log_run

    def _no_stamp(self, **kwargs):
        meta = real_log_run(self, **kwargs)
        payload = json.loads(meta.read_text())
        payload.pop("run_stamp")
        meta.write_text(json.dumps(payload))
        return meta

    monkeypatch.setattr(ExperimentTracker, "log_run", _no_stamp)
    with pytest.raises(GovernedRunRefused, match="same clean commit"):
        governed_validation.run()
    assert not EvidenceWriter(governed_validation.root).ledger_path.exists()
