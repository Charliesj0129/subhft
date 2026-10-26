"""PR5c: ``research status``, ``evidence init``/``import-kills`` and the legacy evidence adapter."""

from __future__ import annotations

import json
import sys
import types
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hft_platform.alpha.evidence import (
    EVIDENCE_ROOT_ENV,
    LEGACY_KILLS_RELPATH,
    ROOT_LAYOUT,
    EvidenceWriter,
    RunMode,
    init_root,
)
from hft_platform.alpha.status import ledger_events_by_alpha
from hft_platform.contracts.alpha import AlphaStatus
from research.registry import legacy_evidence, status_cli


@pytest.fixture
def research_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A research root with alpha ``real`` and archived alpha ``old``."""
    root = tmp_path / "research"
    (root / "alphas" / "real").mkdir(parents=True)
    (root / "alphas" / "_kill_ledger_dir").mkdir()
    (root / "archive" / "alphas_2026-04-17" / "old").mkdir(parents=True)
    (root / "experiments" / "runs").mkdir(parents=True)
    monkeypatch.setattr(legacy_evidence, "research_root", lambda: root)
    return root


@pytest.fixture
def evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "evidence"
    monkeypatch.setenv(EVIDENCE_ROOT_ENV, str(root))
    return root


def _meta(runs_root: Path, run_id: str, alpha_id: str, ts: str, **extra: object) -> None:
    run = runs_root / run_id
    run.mkdir(parents=True)
    payload = {"alpha_id": alpha_id, "run_id": run_id, "timestamp": ts, **extra}
    (run / "meta.json").write_text(json.dumps(payload), encoding="utf-8")


def _kill(alpha_id: str, kill_id: str, killed_by: str = "cli:kill:op", reason: str = "no edge") -> dict:
    return {"alpha_id": alpha_id, "kill_id": kill_id, "gate": "C", "reason": reason, "killed_by": killed_by}


# --- evidence root ---------------------------------------------------------


def test_init_root_creates_the_layout_and_a_local_git_repo(tmp_path: Path) -> None:
    root = tmp_path / "ev"
    created = init_root(root)
    assert created == [f"{name}/" for name in ROOT_LAYOUT] + [".git"]
    assert all((root / name).is_dir() for name in ROOT_LAYOUT)
    assert (root / ".git").is_dir()


def test_init_root_twice_creates_nothing(tmp_path: Path) -> None:
    init_root(tmp_path / "ev")
    assert init_root(tmp_path / "ev") == []


def test_append_legacy_kills_skips_an_already_archived_kill_id(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    assert writer.append_legacy_kills([_kill("a", "k1"), _kill("a", "k1"), _kill("b", "k2")]) == 2
    assert writer.append_legacy_kills([_kill("a", "k1"), _kill("c", "k3")]) == 1
    assert [k["kill_id"] for k in writer.read_legacy_kills()] == ["k1", "k2", "k3"]


def test_append_legacy_kills_without_a_kill_id_raises_and_writes_nothing(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    with pytest.raises(ValueError, match="kill_id"):
        writer.append_legacy_kills([_kill("a", "k1"), {"alpha_id": "b"}])
    assert not (tmp_path / LEGACY_KILLS_RELPATH).exists()


def test_archived_legacy_kills_never_reach_the_verdict_ledger(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    writer.append_legacy_kills([_kill("a", "k1")])
    assert ledger_events_by_alpha(tmp_path) == {}


def test_read_legacy_kills_malformed_line_raises(tmp_path: Path) -> None:
    path = tmp_path / LEGACY_KILLS_RELPATH
    path.parent.mkdir(parents=True)
    path.write_text('{"alpha_id": "a"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="legacy_kill.v1"):
        list(EvidenceWriter(tmp_path).read_legacy_kills())


# --- legacy adapter: quarantine -------------------------------------------


def test_known_alpha_ids_covers_alphas_and_archive_batches(research_tree: Path) -> None:
    assert legacy_evidence.known_alpha_ids() == frozenset({"real", "old"})


@pytest.mark.parametrize(
    ("row", "automated", "expected"),
    [
        (
            {"alpha_id": "real", "reason": "Gate C has not passed: /tmp/pytest-of-u/pytest-3/x"},
            False,
            "pytest_tmp_path",
        ),
        ({"alpha_id": "alpha_a", "reason": "sharpe_oos"}, True, "unknown_alpha"),
        ({"alpha_id": "abandoned_scaffold", "reason": "no manifest"}, False, None),
        ({"alpha_id": "real", "reason": "sharpe_oos"}, True, None),
    ],
)
def test_quarantine_reason_marks_only_fixture_rows(research_tree: Path, row, automated, expected) -> None:
    known = legacy_evidence.known_alpha_ids()
    assert legacy_evidence.quarantine_reason(row, automated=automated, known=known) == expected


def test_read_runs_quarantines_fixture_and_unreadable_runs(research_tree: Path) -> None:
    runs_root = research_tree / "experiments" / "runs"
    _meta(runs_root, "r1", "real", "2026-05-01T00:00:00")
    _meta(runs_root, "r2", "test_alpha", "2026-05-02T00:00:00")
    _meta(runs_root, "r3", "old", "2026-05-03T00:00:00", data_paths=["/tmp/pytest-of-u/pytest-1/d.npy"])
    (runs_root / "r4").mkdir()
    (runs_root / "r4" / "meta.json").write_text("{", encoding="utf-8")
    runs = legacy_evidence.read_runs(runs_root, legacy_evidence.known_alpha_ids())
    assert [(r.run_id, r.quarantine) for r in runs] == [
        ("r1", None),
        ("r2", "unknown_alpha"),
        ("r3", "pytest_tmp_path"),
        ("r4", "unreadable_meta"),
    ]


def test_run_stats_counts_only_unquarantined_runs(research_tree: Path) -> None:
    runs = [
        legacy_evidence.LegacyRun("real", "r1", "2026-05-01", None),
        legacy_evidence.LegacyRun("real", "r2", "2026-05-03", "pytest_tmp_path"),
        legacy_evidence.LegacyRun("real", "r3", "2026-05-02", None),
    ]
    assert legacy_evidence.run_stats(runs)["real"] == {
        "run_count": 2,
        "latest_run_id": "r3",
        "latest_timestamp": "2026-05-02",
        "quarantined_count": 1,
    }


def test_combined_run_stats_counts_both_roots(research_tree: Path, evidence: Path) -> None:
    _meta(research_tree / "experiments" / "runs", "legacy1", "real", "2026-05-01T00:00:00")
    _meta(research_tree / "experiments" / "runs", "fixture", "alpha_x", "2026-05-01T00:00:00")
    _meta(evidence / "runs", "gov1", "real", "2026-10-26T00:00:00")
    _meta(evidence / "runs", "gov2", "new_candidate", "2026-10-26T01:00:00")
    stats = legacy_evidence.combined_run_stats(evidence)
    assert stats["real"] == {
        "legacy_run_count": 1,
        "governed_run_count": 1,
        "run_count": 2,
        "quarantined_run_count": 0,
        "latest_run_id": "gov1",
        "latest_timestamp": "2026-10-26T00:00:00",
    }
    assert stats["alpha_x"]["run_count"] == 0
    assert stats["alpha_x"]["quarantined_run_count"] == 1
    assert stats["new_candidate"]["governed_run_count"] == 1


# --- legacy adapter: kill sources -----------------------------------------


def test_read_jsonl_kills_flags_fixtures_and_keeps_operator_kills(research_tree: Path, tmp_path: Path) -> None:
    path = tmp_path / "kills.jsonl"
    rows = [
        _kill("real", "k1", killed_by="promote_alpha:auto"),
        _kill("test_alpha", "k2", killed_by="promote_alpha:auto"),
        _kill("abandoned_scaffold", "k3", killed_by="cli:kill:op"),
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    kills = legacy_evidence.read_jsonl_kills(path, legacy_evidence.known_alpha_ids())
    assert [(k["kill_id"], k["quarantine"], k["source"]) for k in kills] == [
        ("k1", None, "jsonl"),
        ("k2", "unknown_alpha", "jsonl"),
        ("k3", None, "jsonl"),
    ]


def test_read_jsonl_kills_row_without_a_kill_id_raises(tmp_path: Path) -> None:
    path = tmp_path / "kills.jsonl"
    path.write_text('{"alpha_id": "a"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="not a kill row"):
        legacy_evidence.read_jsonl_kills(path, frozenset())


class _FakeClient:
    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def query(self, sql: str, **_kwargs: object) -> types.SimpleNamespace:
        self.queries.append(sql)
        return types.SimpleNamespace(result_rows=self.rows)


def test_read_ch_kills_only_selects_and_converts_the_timestamp(research_tree: Path) -> None:
    killed_at = datetime(2026, 5, 6, tzinfo=UTC)
    client = _FakeClient([("k1", killed_at, "real", "D", "parity", "h", "s", "promote_alpha:auto")])
    (kill,) = legacy_evidence.read_ch_kills(client, legacy_evidence.known_alpha_ids())
    assert client.queries[0].startswith("SELECT ") and "audit.alpha_kill_ledger" in client.queries[0]
    assert kill["killed_at"] == int(killed_at.timestamp()) * 1_000_000_000
    assert (kill["alpha_id"], kill["gate"], kill["source"], kill["quarantine"]) == ("real", "D", "clickhouse", None)


# --- CLI ------------------------------------------------------------------


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    def _manifest(alpha_id: str, status: AlphaStatus) -> types.SimpleNamespace:
        manifest = types.SimpleNamespace(
            alpha_id=alpha_id,
            status=status,
            tier=None,
            complexity="O1",
            paper_refs=(),
            data_fields=(),
            rust_module=None,
            latency_profile=None,
        )
        return types.SimpleNamespace(manifest=manifest)

    loaded = {"real": _manifest("real", AlphaStatus.GATE_C), "old": _manifest("old", AlphaStatus.DRAFT)}
    fake = types.SimpleNamespace(errors=(), discover=lambda _path: loaded)
    monkeypatch.setattr("research.registry.alpha_registry.AlphaRegistry", lambda: fake)
    return loaded


def _verdicts(root: Path, events: list[dict]) -> None:
    writer = EvidenceWriter(root)
    for event in events:
        writer.append_verdict(event, mode=RunMode.GOVERNED)


def test_status_shows_ledger_status_runs_and_archived_kills(research_tree, evidence, registry, capsys) -> None:
    _verdicts(evidence, [{"alpha_id": "real", "stage": "gate_a", "passed": False, "verdict": "KILL", "run_id": "g"}])
    _meta(research_tree / "experiments" / "runs", "legacy1", "old", "2026-05-01T00:00:00")
    EvidenceWriter(evidence).append_legacy_kills(
        [{**_kill("old", "k1"), "quarantine": None}, {**_kill("alpha_a", "k2"), "quarantine": "unknown_alpha"}]
    )
    assert status_cli.main(["status", "--json"]) == 0
    rows = {r["alpha_id"]: r for r in json.loads(capsys.readouterr().out)["alphas"]}
    assert set(rows) == {"real", "old"}
    assert (rows["real"]["status"], rows["real"]["status_source"], rows["real"]["declared_status"]) == (
        "KILLED",
        "ledger",
        "GATE_C",
    )
    assert (rows["old"]["status"], rows["old"]["status_source"]) == ("DRAFT", "manifest")
    assert (rows["old"]["legacy_run_count"], rows["old"]["legacy_kill_count"]) == (1, 1)


def test_status_lists_an_alpha_known_only_to_the_ledger(research_tree, evidence, registry, capsys) -> None:
    _verdicts(evidence, [{"alpha_id": "gone", "stage": "gate_a", "passed": True, "verdict": "PASS", "run_id": "g"}])
    assert status_cli.main(["status", "--alpha", "gone"]) == 0
    out = capsys.readouterr().out
    assert "gone\tstatus=GATE_A\tsource=ledger\tdeclared=-" in out


def test_status_unknown_alpha_exits_1(research_tree, evidence, registry, capsys) -> None:
    assert status_cli.main(["status", "--alpha", "nope"]) == 1
    assert "Unknown alpha" in capsys.readouterr().err


def test_status_malformed_ledger_exits_2(research_tree, evidence, registry, capsys) -> None:
    (evidence / "ledger").mkdir(parents=True)
    (evidence / "ledger" / "verdicts.jsonl").write_text("[]\n", encoding="utf-8")
    assert status_cli.main(["status"]) == 2
    assert "Cannot read the evidence root" in capsys.readouterr().err


def test_status_relative_evidence_root_exits_2(research_tree, registry, monkeypatch, capsys) -> None:
    monkeypatch.setenv(EVIDENCE_ROOT_ENV, "relative/evidence")
    assert status_cli.main(["status"]) == 2
    assert "relative" in capsys.readouterr().err


def test_evidence_init_initialises_the_configured_root(evidence, capsys) -> None:
    assert status_cli.main(["evidence", "init"]) == 0
    assert (evidence / ".git").is_dir() and (evidence / "ledger").is_dir()
    assert status_cli.main(["evidence", "init"]) == 0
    assert "already initialised" in capsys.readouterr().out


@pytest.fixture
def kill_sources(research_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "kill_ledger.jsonl"
    rows = [_kill("old", "k1"), _kill("test_alpha", "k2", killed_by="promote_alpha:auto")]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    monkeypatch.setenv("HFT_ALPHA_KILL_LEDGER_PATH", str(path))
    client = _FakeClient(
        [("k3", datetime(2026, 5, 6, tzinfo=UTC), "real", "D", "parity", "", "", "promote_alpha:auto")]
    )
    monkeypatch.setattr("hft_platform.alpha.audit._get_client", lambda: client)
    return client


def test_import_kills_archives_both_sources_once(evidence, kill_sources, capsys) -> None:
    assert status_cli.main(["evidence", "import-kills"]) == 0
    assert "read 3 kills (1 quarantined as fixtures); archived 3 new" in capsys.readouterr().out
    assert status_cli.main(["evidence", "import-kills"]) == 0
    assert "archived 0 new" in capsys.readouterr().out
    archived = list(EvidenceWriter(evidence).read_legacy_kills())
    assert [(k["kill_id"], k["source"], k["quarantine"]) for k in archived] == [
        ("k1", "jsonl", None),
        ("k2", "jsonl", "unknown_alpha"),
        ("k3", "clickhouse", None),
    ]
    assert ledger_events_by_alpha(evidence) == {}


def test_import_kills_dry_run_writes_nothing(evidence, kill_sources, capsys) -> None:
    assert status_cli.main(["evidence", "import-kills", "--dry-run"]) == 0
    assert "dry run, nothing written" in capsys.readouterr().out
    assert not (evidence / LEGACY_KILLS_RELPATH).exists()


def test_import_kills_clickhouse_failure_exits_2_and_writes_nothing(
    evidence, kill_sources, monkeypatch, capsys
) -> None:
    def _down() -> None:
        raise ConnectionError("clickhouse unreachable")

    monkeypatch.setattr("hft_platform.alpha.audit._get_client", _down)
    assert status_cli.main(["evidence", "import-kills"]) == 2
    assert "clickhouse unreachable" in capsys.readouterr().err
    assert not (evidence / LEGACY_KILLS_RELPATH).exists()


def test_import_kills_from_jsonl_does_not_touch_clickhouse(evidence, kill_sources, capsys) -> None:
    assert status_cli.main(["evidence", "import-kills", "--from", "jsonl"]) == 0
    assert kill_sources.queries == []
    assert "archived 2 new" in capsys.readouterr().out


def test_research_main_dispatches_status(research_tree, evidence, registry, monkeypatch, capsys) -> None:
    from research.__main__ import main

    monkeypatch.setattr(sys, "argv", ["research", "status", "--json"])
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["evidence_root"] == str(evidence.resolve())


def test_factory_index_records_run_counts_from_both_roots(research_tree, evidence, registry, tmp_path, monkeypatch):
    from research import factory

    monkeypatch.setattr(factory, "ROOT", research_tree)
    _meta(research_tree / "experiments" / "runs", "legacy1", "real", "2026-05-01T00:00:00")
    _meta(
        research_tree / "experiments" / "runs",
        "fixture",
        "real",
        "2026-05-02T00:00:00",
        data_paths=["/tmp/pytest-of-u/x"],
    )
    _meta(evidence / "runs", "gov1", "real", "2026-10-26T00:00:00")
    out = tmp_path / "index.json"
    assert factory.cmd_index(Namespace(out=str(out))) == 0
    row = next(r for r in json.loads(out.read_text(encoding="utf-8"))["alphas"] if r["alpha_id"] == "real")
    assert (row["run_count"], row["legacy_run_count"], row["governed_run_count"], row["quarantined_run_count"]) == (
        2,
        1,
        1,
        1,
    )
    assert row["latest_run_id"] == "gov1"
