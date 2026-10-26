"""Alpha status derived from the verdict ledger (research v2, PR5a)."""

from __future__ import annotations

import json
import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

from hft_platform.alpha.evidence import EVIDENCE_ROOT_ENV, EvidenceWriter, RunMode
from hft_platform.alpha.status import (
    StatusSource,
    derive_status,
    effective_status,
    ledger_events_by_alpha,
)
from hft_platform.contracts.alpha import AlphaStatus


def _ev(stage: str, passed: bool, verdict: str | None = None, alpha_id: str = "a1", run_id: str | None = None) -> dict:
    event = {"alpha_id": alpha_id, "stage": stage, "passed": passed, "run_id": run_id}
    if verdict is not None:
        event["verdict"] = verdict
    return event


def _validation(c_passed: bool = True, verdict: str = "PASS", run_id: str | None = None) -> list[dict]:
    return [
        _ev("gate_a", True, run_id=run_id),
        _ev("gate_b", True, run_id=run_id),
        _ev("gate_c", c_passed, verdict, run_id=run_id),
    ]


def _promotion(d: bool = True, e: bool = True, f: bool = True, run_id: str | None = None) -> list[dict]:
    return [
        _ev("gate_d", d, "PASS" if d else "NEEDS-MORE-DAYS", run_id=run_id),
        _ev("gate_e", e, run_id=run_id),
        _ev("gate_f", f, run_id=run_id),
    ]


# --- derive_status ------------------------------------------------------------


def test_derive_status_without_events_is_none() -> None:
    assert derive_status([]) is None


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        (_validation(c_passed=False, verdict="NEEDS-MORE-DAYS"), AlphaStatus.GATE_B),
        (_validation(), AlphaStatus.GATE_C),
        (_validation() + _promotion(), AlphaStatus.GATE_E),
        (_validation() + _promotion(d=False, e=True), AlphaStatus.GATE_C),
        # gate_f has no status; a failed gate_e is not hidden by a later gate_f pass.
        (_validation() + _promotion(e=False, f=True), AlphaStatus.GATE_D),
        ([_ev("gate_a", False)], AlphaStatus.DRAFT),
    ],
)
def test_derive_status_climbs_the_ladder_to_the_first_failed_stage(events, expected) -> None:
    assert derive_status(events) is expected


def test_derive_status_a_later_failed_lower_stage_drops_the_status() -> None:
    assert derive_status(_validation() + _promotion() + [_ev("gate_a", False)]) is AlphaStatus.DRAFT


def test_derive_status_a_newer_lower_stage_run_supersedes_an_older_higher_pass() -> None:
    # Gate D passed on run 1's scorecard; run 2 passed Gate C again but was not promoted.
    assert derive_status(_validation() + _promotion() + _validation()) is AlphaStatus.GATE_C


def test_derive_status_a_late_promotion_of_an_older_run_does_not_count_for_a_newer_one() -> None:
    # r1 and r2 both pass A-C; a promotion of r1's scorecard is appended afterwards.
    events = _validation(run_id="r1") + _validation(run_id="r2") + _promotion(run_id="r1")
    assert derive_status(events) is AlphaStatus.GATE_C


def test_derive_status_a_promotion_of_the_latest_run_counts() -> None:
    events = _validation(run_id="r1") + _validation(run_id="r2") + _promotion(run_id="r2")
    assert derive_status(events) is AlphaStatus.GATE_E


@pytest.mark.parametrize(
    ("event", "match"),
    [
        ({"stage": "gate_a", "passed": True}, "no alpha_id"),
        ({"alpha_id": "a1", "passed": True}, "unknown stage"),
        ({"alpha_id": "a1", "stage": "gate_z", "passed": True}, "unknown stage"),
        ({"alpha_id": "a1", "stage": "gate_a"}, "not a boolean"),
        ({"alpha_id": "a1", "stage": "gate_a", "passed": "yes"}, "not a boolean"),
        ({"alpha_id": "a1", "stage": "gate_c", "passed": False, "verdict": "MAYBE"}, "unknown verdict"),
        ({"alpha_id": "a1", "stage": "gate_c", "passed": True, "verdict": "NEEDS-MORE-DAYS"}, "passed with verdict"),
        ({"alpha_id": "a1", "stage": "gate_a", "passed": True, "run_id": 7}, "run_id"),
    ],
)
def test_derive_status_malformed_event_raises(event, match) -> None:
    with pytest.raises(ValueError, match=match):
        derive_status(_validation() + _promotion() + [event])


def test_derive_status_ignores_an_invalid_verdict() -> None:
    events = _validation() + _promotion() + [_ev("gate_d", False, "INVALID")]
    assert derive_status(events) is AlphaStatus.GATE_E


def test_derive_status_kill_is_sticky() -> None:
    events = _validation(c_passed=False, verdict="KILL") + _validation()
    assert derive_status(events) is AlphaStatus.KILLED


# --- effective_status -----------------------------------------------------------


def test_effective_status_without_events_keeps_the_declared_status() -> None:
    result = effective_status(AlphaStatus.GATE_C, [])
    assert (result.status, result.source) == (AlphaStatus.GATE_C, StatusSource.MANIFEST)


def test_effective_status_ledger_overrides_a_legacy_rewritten_status() -> None:
    # A legacy run rewrote impl.py to GATE_C; the first governed run failed Gate A.
    result = effective_status(AlphaStatus.GATE_C, [_ev("gate_a", False)])
    assert (result.status, result.source) == (AlphaStatus.DRAFT, StatusSource.LEDGER)


@pytest.mark.parametrize("declared", [AlphaStatus.PRODUCTION, AlphaStatus.DEPRECATED, AlphaStatus.KILLED])
def test_effective_status_keeps_a_manual_status_over_a_ladder_status(declared) -> None:
    result = effective_status(declared, _validation())
    assert (result.status, result.source) == (declared, StatusSource.MANIFEST)


def test_effective_status_ledger_kill_wins_over_production() -> None:
    result = effective_status(AlphaStatus.PRODUCTION, _validation(c_passed=False, verdict="KILL"))
    assert (result.status, result.source) == (AlphaStatus.KILLED, StatusSource.LEDGER)


# --- ledger reading -------------------------------------------------------------


def _ledger(root: Path, events: list[dict]) -> Path:
    writer = EvidenceWriter(root)
    for event in events:
        writer.append_verdict(event, mode=RunMode.GOVERNED)
    return writer.ledger_path


def test_ledger_events_by_alpha_groups_in_ledger_order(tmp_path: Path) -> None:
    _ledger(tmp_path, [_ev("gate_a", True), _ev("gate_a", False, alpha_id="b2"), _ev("gate_b", True)])
    grouped = ledger_events_by_alpha(tmp_path)
    assert [e["stage"] for e in grouped["a1"]] == ["gate_a", "gate_b"]
    assert [e["stage"] for e in grouped["b2"]] == ["gate_a"]


def test_ledger_events_by_alpha_missing_ledger_is_empty(tmp_path: Path) -> None:
    assert ledger_events_by_alpha(tmp_path) == {}


def test_ledger_events_by_alpha_reads_the_configured_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _ledger(tmp_path, [_ev("gate_a", True)])
    monkeypatch.setenv(EVIDENCE_ROOT_ENV, str(tmp_path))
    assert list(ledger_events_by_alpha()) == ["a1"]


def test_ledger_events_by_alpha_event_without_alpha_id_raises(tmp_path: Path) -> None:
    path = _ledger(tmp_path, [_ev("gate_a", True)])
    record = {"schema": "verdict_event.v1", "stage": "gate_a", "passed": False}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    with pytest.raises(ValueError, match="no alpha_id"):
        ledger_events_by_alpha(tmp_path)


def test_ledger_events_by_alpha_malformed_ledger_raises(tmp_path: Path) -> None:
    path = _ledger(tmp_path, [_ev("gate_a", True)])
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    with pytest.raises(ValueError, match="not JSON"):
        ledger_events_by_alpha(tmp_path)


# --- readers --------------------------------------------------------------------


def _alpha(status: AlphaStatus) -> types.SimpleNamespace:
    manifest = types.SimpleNamespace(
        alpha_id="a1",
        status=status,
        tier=None,
        complexity="O(1)",
        paper_refs=(),
        data_fields=(),
        rust_module=None,
        latency_profile=None,
    )
    return types.SimpleNamespace(manifest=manifest)


@pytest.fixture
def ledger_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "evidence"
    monkeypatch.setenv(EVIDENCE_ROOT_ENV, str(root))
    return root


@pytest.fixture
def fake_discovery(monkeypatch: pytest.MonkeyPatch):
    registry = types.SimpleNamespace(errors=(), discover=lambda _path: {"a1": _alpha(AlphaStatus.GATE_C)})
    monkeypatch.setitem(
        sys.modules, "hft_platform.alpha.discovery", types.SimpleNamespace(AlphaDiscoveryRegistry=lambda: registry)
    )
    return registry


def test_alpha_list_shows_the_ledger_status_and_its_source(ledger_root, fake_discovery, capsys) -> None:
    from hft_platform.cli._alpha import cmd_alpha_list

    _ledger(ledger_root, [_ev("gate_a", True), _ev("gate_b", False)])
    cmd_alpha_list(Namespace())
    assert "a1\tstatus=GATE_A\tsource=ledger\ttier=-" in capsys.readouterr().out


def test_alpha_list_without_events_shows_the_manifest_status(ledger_root, fake_discovery, capsys) -> None:
    from hft_platform.cli._alpha import cmd_alpha_list

    cmd_alpha_list(Namespace())
    assert "a1\tstatus=GATE_C\tsource=manifest\ttier=-" in capsys.readouterr().out


def _append_raw(root: Path, record: dict) -> None:
    with (root / "ledger" / "verdicts.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"schema": "verdict_event.v1", **record}) + "\n")


def test_alpha_list_event_without_a_stage_exits_2(ledger_root, fake_discovery, capsys) -> None:
    from hft_platform.cli._alpha import cmd_alpha_list

    _ledger(ledger_root, _validation() + _promotion())
    _append_raw(ledger_root, {"alpha_id": "a1", "passed": False})
    with pytest.raises(SystemExit) as exc:
        cmd_alpha_list(Namespace())
    assert exc.value.code == 2
    assert "status=" not in capsys.readouterr().out


def test_alpha_list_malformed_ledger_exits_2(ledger_root, fake_discovery, capsys) -> None:
    from hft_platform.cli._alpha import cmd_alpha_list

    ledger_root.joinpath("ledger").mkdir(parents=True)
    ledger_root.joinpath("ledger", "verdicts.jsonl").write_text("{broken\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cmd_alpha_list(Namespace())
    assert exc.value.code == 2
    assert "verdict ledger" in capsys.readouterr().err


@pytest.fixture
def factory_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from research import factory

    registry = types.SimpleNamespace(errors=(), discover=lambda _path: {"a1": _alpha(AlphaStatus.GATE_C)})
    monkeypatch.setattr("research.registry.alpha_registry.AlphaRegistry", lambda: registry)
    monkeypatch.setattr(factory, "_load_experiment_stats", lambda _root, _ids=(): {})
    out = tmp_path / "index.json"
    return lambda: (factory.cmd_index(Namespace(out=str(out))), out)


def test_factory_index_records_the_ledger_status_beside_the_declared_one(ledger_root, factory_index) -> None:
    _ledger(ledger_root, _validation(c_passed=False, verdict="KILL"))
    rc, out = factory_index()
    assert rc == 0
    (row,) = json.loads(out.read_text(encoding="utf-8"))["alphas"]
    assert (row["status"], row["status_source"], row["declared_status"]) == ("KILLED", "ledger", "GATE_C")


def test_factory_index_contradictory_verdict_returns_2(ledger_root, factory_index) -> None:
    _ledger(ledger_root, [_ev("gate_a", True), _ev("gate_b", True)])
    _append_raw(ledger_root, {"alpha_id": "a1", "stage": "gate_c", "passed": True, "verdict": "NEEDS-MORE-DAYS"})
    rc, out = factory_index()
    assert rc == 2
    assert not out.exists()


def test_factory_index_malformed_ledger_returns_2(ledger_root, factory_index) -> None:
    ledger_root.joinpath("ledger").mkdir(parents=True)
    ledger_root.joinpath("ledger", "verdicts.jsonl").write_text("[]\n", encoding="utf-8")
    rc, out = factory_index()
    assert rc == 2
    assert not out.exists()
