"""PR6: the defects registry and ``evidence audit-stale`` (S8: a run made before a fix is flagged)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from hft_platform.alpha.evidence import EVIDENCE_ROOT_ENV, STALE_RELPATH, EvidenceWriter, RunMode
from research.registry import legacy_evidence, stale_audit, status_cli

SHIPPED = Path(__file__).resolve().parents[3] / stale_audit.DEFECTS_RELPATH


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def history(tmp_path: Path) -> dict[str, str]:
    """A repo with commits ``before`` -> ``fix`` -> ``after`` and an unrelated ``side`` branch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    shas: dict[str, str] = {"repo": str(repo)}
    for name in ("before", "fix", "after"):
        (repo / name).write_text(name, encoding="utf-8")
        _git(repo, "add", name)
        _git(repo, "commit", "-q", "-m", name)
        shas[name] = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "side", shas["before"])
    (repo / "side").write_text("side", encoding="utf-8")
    _git(repo, "add", "side")
    _git(repo, "commit", "-q", "-m", "side")
    shas["side"] = _git(repo, "rev-parse", "HEAD")
    return shas


def _registry(path: Path, defects: list[dict], components: dict | None = None) -> Path:
    doc = {
        "schema": stale_audit.DEFECTS_SCHEMA,
        "components": components or {c: c for c in sorted(stale_audit.RUN_COMPONENTS | {"mining.search"})},
        "defects": defects,
    }
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _defect(defect_id: str, fix: str | None, components: list[str], **extra: object) -> dict:
    return {"id": defect_id, "summary": f"{defect_id} summary", "components": components, "fix_commit": fix, **extra}


def _run(
    commit: str | None, datasets: tuple[str, ...] | None = None, key: str = "governed:r1"
) -> stale_audit.AuditedRun:
    return stale_audit.AuditedRun(key, "governed", "a", "r1", commit, datasets, ("e1",))


# --- registry ---------------------------------------------------------------


def test_shipped_registry_loads_and_names_the_planned_defects() -> None:
    registry = stale_audit.load_registry(SHIPPED)
    ids = {d.id for d in registry.defects}
    assert {
        "search-self-correlation",
        "mining-reset-leak",
        "duplicate-tick-delivery",
        "evidence-pollution",
        "default-pass-and-false-kill",
        "taker-row-vs-day",
        "maker-latency-instant-rtt",
        "taker-latency-unresolved",
        "maker-report-constants",
        "gp-mutation-shreds",
    } <= ids
    assert stale_audit.RUN_COMPONENTS <= registry.components
    assert {d.id for d in registry.defects if d.fix_commit is None} == {"maker-report-constants", "gp-mutation-shreds"}


@pytest.mark.parametrize(
    ("defects", "message"),
    [
        ([_defect("d", "abc123", ["verdict"])], "40-hex"),
        ([_defect("d", None, ["nope"])], "not a subset"),
        ([_defect("d", None, ["verdict"]), _defect("d", None, ["verdict"])], "duplicate"),
        ([{"id": "d", "summary": "s", "components": ["verdict"]}], "no fix_commit"),
        ([_defect("d", None, ["verdict"], requires=["magic"])], "requires"),
        ([{**_defect("d", None, ["verdict"]), "summary": ""}], "no summary"),
    ],
)
def test_load_registry_rejects_a_malformed_entry(tmp_path: Path, defects: list[dict], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        stale_audit.load_registry(_registry(tmp_path / "d.yaml", defects))


def test_load_registry_rejects_another_schema(tmp_path: Path) -> None:
    path = tmp_path / "d.yaml"
    path.write_text("schema: other\n", encoding="utf-8")
    with pytest.raises(ValueError, match="research_defects.v1"):
        stale_audit.load_registry(path)


# --- staleness rules (S8) ---------------------------------------------------


def test_run_before_the_fix_is_stale_and_after_it_is_not(history: dict[str, str], tmp_path: Path) -> None:
    registry = stale_audit.load_registry(_registry(tmp_path / "d.yaml", [_defect("d", history["fix"], ["verdict"])]))
    git = stale_audit.GitHistory(Path(history["repo"]))
    runs = [
        _run(history["before"], key="governed:old"),
        _run(history["side"], key="governed:side"),
        _run(history["fix"], key="governed:fix"),
        _run(history["after"], key="governed:new"),
    ]
    marks = stale_audit.audit(runs, registry, git)
    assert {(m["run_key"], m["basis"]) for m in marks} == {
        ("governed:old", "before_fix"),
        ("governed:side", "before_fix"),
    }


@pytest.mark.parametrize(
    ("commit", "fix", "basis"),
    [
        (None, "fix", "no_commit"),
        ("f" * 40, "fix", "commit_unknown"),
        ("after", None, "open"),
    ],
)
def test_stale_basis_names_why_a_run_is_stale(
    history: dict[str, str], tmp_path: Path, commit: str | None, fix: str | None, basis: str
) -> None:
    fix_sha = history[fix] if fix else None
    defect = stale_audit.Defect("d", frozenset({"verdict"}), fix_sha, frozenset())
    run = _run(history.get(commit, commit) if commit else None)
    assert stale_audit.stale_basis(run, defect, stale_audit.GitHistory(Path(history["repo"]))) == basis


def test_a_defect_of_a_component_no_run_carries_marks_nothing(history: dict[str, str]) -> None:
    defect = stale_audit.Defect("d", frozenset({"mining.search"}), None, frozenset())
    assert stale_audit.stale_basis(_run(None), defect, stale_audit.GitHistory(Path(history["repo"]))) is None


def _dataset(base: Path, name: str, meta: dict | None) -> str:
    data = base / name
    data.write_bytes(b"x")
    if meta is not None:
        (base / f"{name}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return str(data)


@pytest.mark.parametrize(
    ("meta", "stale"),
    [
        ({"source_type": "real", "dedup_rule": "content_multiplicity_v1"}, False),
        ({"source_type": "synthetic"}, False),
        ({"source_type": "real", "dedup_rule": "unrecorded"}, True),
        ({"source_type": "real"}, True),
        (None, True),
    ],
)
def test_dataset_dedup_defect_needs_a_recorded_rule_even_after_the_fix(
    history: dict[str, str], tmp_path: Path, meta: dict | None, stale: bool
) -> None:
    defect = stale_audit.Defect("dedup", frozenset({"dataset"}), history["fix"], frozenset({"dataset_dedup"}))
    datasets = (_dataset(tmp_path, "day.npz", meta), _dataset(tmp_path, "hftbt.npz", None))
    basis = stale_audit.stale_basis(
        _run(history["after"], datasets), defect, stale_audit.GitHistory(Path(history["repo"])), base=tmp_path
    )
    assert basis == ("dataset_not_deduped" if stale else None)


def test_audit_refuses_when_a_fix_commit_is_not_in_the_checkout(history: dict[str, str], tmp_path: Path) -> None:
    registry = stale_audit.load_registry(_registry(tmp_path / "d.yaml", [_defect("d", "e" * 40, ["verdict"])]))
    with pytest.raises(ValueError, match="fix commits not in this checkout: d"):
        stale_audit.audit([_run(history["after"])], registry, stale_audit.GitHistory(Path(history["repo"])))


# --- runs audited -----------------------------------------------------------


def _verdict(root: Path, **event: object) -> dict:
    return EvidenceWriter(root).append_verdict({"stage": "gate_c", "passed": True, **event}, mode=RunMode.GOVERNED)


def test_governed_runs_split_a_promotion_on_a_later_commit(tmp_path: Path) -> None:
    root = tmp_path / "ev"
    first = _verdict(root, alpha_id="a", run_id="r1", git_commit="c1")
    _verdict(root, alpha_id="a", run_id="r1", git_commit="c2", stage="gate_d")
    lone = _verdict(root, alpha_id="b", run_id=None, git_commit="c1", stage="gate_a")
    stamp = {"git_commit": "c1", "datasets": {"/data/day.npz": "sha"}}
    (root / "runs" / "r1").mkdir(parents=True)
    (root / "runs" / "r1" / "meta.json").write_text(
        json.dumps({"alpha_id": "a", "run_id": "r1", "run_stamp": stamp}), encoding="utf-8"
    )
    runs = stale_audit.governed_runs(root)
    assert [(r.run_key, r.git_commit, r.dataset_paths) for r in runs] == [
        ("governed:r1@c1", "c1", ("/data/day.npz",)),
        ("governed:r1@c2", "c2", ("/data/day.npz",)),
        (f"governed:event:{lone['event_id']}@c1", "c1", None),
    ]
    assert runs[0].event_ids == (first["event_id"],)


def test_legacy_runs_skip_pytest_fixtures_but_audit_runs_of_deleted_alphas(tmp_path: Path) -> None:
    runs_root = tmp_path / "runs"
    for run_id, alpha_id, extra in (
        ("real1", "real", {}),
        ("fix1", "real", {"data_paths": ["/tmp/pytest-of-u/x.npy"]}),
        ("ghost", "alpha_x", {}),
    ):
        (runs_root / run_id).mkdir(parents=True)
        payload = {"alpha_id": alpha_id, "run_id": run_id, "timestamp": "2026-05-01", **extra}
        (runs_root / run_id / "meta.json").write_text(json.dumps(payload), encoding="utf-8")
    runs = stale_audit.legacy_runs(runs_root, frozenset({"real"}))
    assert [(r.run_key, r.git_commit) for r in runs] == [("legacy:ghost", None), ("legacy:real1", None)]


def test_append_stale_marks_without_a_defect_id_raises_and_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="defect_id"):
        EvidenceWriter(tmp_path).append_stale_marks([{"mark_id": "m", "run_key": "k"}])
    assert not (tmp_path / STALE_RELPATH).exists()


# --- CLI --------------------------------------------------------------------


@pytest.fixture
def audit_env(history: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Evidence root with one governed run before the fix and one legacy run; registry of two defects."""
    root = tmp_path / "evidence"
    monkeypatch.setenv(EVIDENCE_ROOT_ENV, str(root))
    research = tmp_path / "research"
    (research / "alphas" / "real").mkdir(parents=True)
    legacy = research / "experiments" / "runs" / "legacy1"
    legacy.mkdir(parents=True)
    (legacy / "meta.json").write_text(json.dumps({"alpha_id": "real", "run_id": "legacy1"}), encoding="utf-8")
    monkeypatch.setattr(legacy_evidence, "research_root", lambda: research)
    monkeypatch.setattr(status_cli, "_discover_declared", dict)
    path = _registry(
        tmp_path / "d.yaml",
        [_defect("fixed", history["fix"], ["verdict"]), _defect("mining", None, ["mining.search"])],
    )
    monkeypatch.setattr(stale_audit, "repo_path", lambda _rel: path)
    monkeypatch.setattr(stale_audit, "repo_root", lambda: Path(history["repo"]))
    _verdict(root, alpha_id="real", run_id="old", git_commit=history["before"], verdict="PASS")
    _verdict(root, alpha_id="real", run_id="new", git_commit=history["after"], verdict="PASS")
    return root


def test_audit_stale_marks_runs_before_the_fix_once(audit_env: Path, history: dict[str, str], capsys) -> None:
    assert status_cli.main(["evidence", "audit-stale"]) == 0
    out = capsys.readouterr().out
    assert "fixed\tstale_runs=2" in out and "mining\tstale_runs=0" in out
    marks = list(EvidenceWriter(audit_env).read_stale_marks())
    assert {(m["run_key"], m["basis"]) for m in marks} == {
        (f"governed:old@{history['before']}", "before_fix"),
        ("legacy:legacy1", "no_commit"),
    }
    assert all(m["marked_at"] and m["registry_sha256"] for m in marks)
    assert status_cli.main(["evidence", "audit-stale"]) == 0
    assert "0 new marks" in capsys.readouterr().out
    assert len(list(EvidenceWriter(audit_env).read_stale_marks())) == 2


def test_audit_stale_dry_run_json_writes_nothing(audit_env: Path, capsys) -> None:
    assert status_cli.main(["evidence", "audit-stale", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["audited_runs"], payload["stale_runs_per_defect"], payload["added"]) == (
        3,
        {"fixed": 2, "mining": 0},
        0,
    )
    assert not (audit_env / STALE_RELPATH).exists()


def test_audit_stale_malformed_registry_exits_2(audit_env: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema: research_defects.v1\ncomponents: {}\n", encoding="utf-8")
    monkeypatch.setattr(stale_audit, "repo_path", lambda _rel: bad)
    assert status_cli.main(["evidence", "audit-stale"]) == 2
    assert "Cannot audit stale runs" in capsys.readouterr().err
    assert not (audit_env / STALE_RELPATH).exists()


def test_status_flags_a_status_resting_on_a_stale_run_without_changing_it(audit_env: Path, capsys) -> None:
    assert status_cli.main(["status", "--json"]) == 0
    before = json.loads(capsys.readouterr().out)["alphas"]
    assert status_cli.main(["evidence", "audit-stale"]) == 0
    capsys.readouterr()
    assert status_cli.main(["status", "--json"]) == 0
    after = json.loads(capsys.readouterr().out)["alphas"]
    assert [r["status"] for r in after] == [r["status"] for r in before]
    row = after[0]
    assert (row["alpha_id"], row["stale_run_count"]) == ("real", 2)
    # The latest gate_c event is from the fixed run, so the status does not rest on the stale one.
    assert row["status_on_stale_run"] is False


def test_status_on_stale_run_when_the_latest_event_is_from_a_stale_run(audit_env: Path, history, capsys) -> None:
    _verdict(audit_env, alpha_id="real", run_id="old2", git_commit=history["before"], verdict="PASS")
    assert status_cli.main(["evidence", "audit-stale"]) == 0
    capsys.readouterr()
    assert status_cli.main(["status", "--alpha", "real", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["alphas"][0]["status_on_stale_run"] is True


def test_status_on_stale_run_when_a_stale_run_killed_the_alpha(audit_env: Path, history, capsys) -> None:
    _verdict(
        audit_env,
        alpha_id="real",
        run_id="k",
        git_commit=history["before"],
        stage="gate_a",
        passed=False,
        verdict="KILL",
    )
    _verdict(audit_env, alpha_id="real", run_id="new2", git_commit=history["after"], stage="gate_a", verdict="PASS")
    assert status_cli.main(["evidence", "audit-stale"]) == 0
    capsys.readouterr()
    assert status_cli.main(["status", "--alpha", "real", "--json"]) == 0
    row = json.loads(capsys.readouterr().out)["alphas"][0]
    assert (row["status"], row["status_on_stale_run"]) == ("KILLED", True)


def test_research_main_dispatches_audit_stale(audit_env: Path, monkeypatch, capsys) -> None:
    from research.__main__ import main

    monkeypatch.setattr("sys.argv", ["research", "evidence", "audit-stale", "--dry-run"])
    assert main() == 0
    assert "dry run, nothing written" in capsys.readouterr().out
