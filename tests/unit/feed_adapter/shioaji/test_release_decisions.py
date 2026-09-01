"""The shioaji upgrade decision ledger, and what it refuses to record.

A captured API surface is evidence, not a decision. These tests pin the
difference: the ledger must hold a verdict for every release newer than the
pin, that verdict must agree with the diff it cites, and a deferral must come
back on its own instead of quietly becoming permanent.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.shioaji_api_diff import decisions as dec
from scripts.shioaji_api_diff import watch
from scripts.shioaji_api_diff.paths import GOLDEN_DIR

PIN = "1.5.6"


def _write_evidence(root: Path, pin: str, version: str, *, breaking: list[str]) -> str:
    """A minimal diff golden shaped like the real ones, under ``root``."""
    rel = f"goldens/diff_{pin}_to_{version}.json"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "changes": [{"qualname": name, "classification": "BREAKING", "platform_used": True} for name in breaking],
        "counts": {"breaking_platform": len(breaking)},
        "verdict": "BLOCKED" if breaking else "SAFE",
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return rel


def _write_ledger(root: Path, entries: list[dict[str, Any]], *, pin: str = PIN) -> Path:
    path = root / "ledger.yaml"
    path.write_text(yaml.safe_dump({"pin": pin, "decisions": entries}, sort_keys=False), encoding="utf-8")
    return path


def _load(root: Path, entries: list[dict[str, Any]], *, pin: str = PIN):
    return dec.load_decisions(_write_ledger(root, entries, pin=pin), repo_root=root)


def _safe_entry(root: Path, version: str = "9.9.9", **overrides: Any) -> dict[str, Any]:
    entry = {
        "version": version,
        "verdict": dec.DEFER,
        "decided_on": date(2026, 8, 27),
        "revisit_after": date(2026, 9, 26),
        "rationale": "waiting on a sim soak",
        "evidence": _write_evidence(root, PIN, version, breaking=[]),
    }
    entry.update(overrides)
    return entry


# --- the ledger that actually ships -------------------------------------------------


def test_the_shipped_ledger_loads_and_agrees_with_every_diff_it_cites() -> None:
    ledger_pin, records = dec.load_decisions()
    assert ledger_pin == PIN
    assert records, "the ledger records no decisions at all"


def test_the_shipped_ledger_is_written_against_the_version_pyproject_pins() -> None:
    # Every verdict is relative to the pin. A ledger against a stale pin does not
    # describe the upgrade in front of anyone.
    ledger_pin, _ = dec.load_decisions()
    assert ledger_pin == watch.read_pin()


def test_every_captured_release_newer_than_the_pin_has_a_recorded_decision() -> None:
    _, records = dec.load_decisions()
    pin_key = watch.parse_version(PIN)
    assert pin_key is not None
    captured = {path.name[len("surface_") : -len(".json")] for path in GOLDEN_DIR.glob("surface_*.json")}
    newer = {version for version in captured if (key := watch.parse_version(version)) is not None and key > pin_key}
    assert newer, "no captured surface is newer than the pin — this test would prove nothing"
    assert newer <= set(records), f"captured but never decided: {sorted(newer - set(records))}"


# --- a verdict may not contradict its own evidence ----------------------------------


def test_an_adopt_verdict_is_refused_when_its_evidence_names_a_platform_break(tmp_path: Path) -> None:
    entry = _safe_entry(tmp_path, verdict=dec.ADOPT, revisit_after=None)
    entry["evidence"] = _write_evidence(tmp_path, PIN, "9.9.9", breaking=["SecurityType.Future"])
    with pytest.raises(dec.DecisionLedgerError, match="contradicts its evidence"):
        _load(tmp_path, [entry])


def test_a_blocked_verdict_is_refused_when_its_evidence_names_no_platform_break(tmp_path: Path) -> None:
    entry = _safe_entry(
        tmp_path,
        verdict=dec.BLOCKED,
        revisit_after=None,
        blocking_symbols=["SecurityType.Future"],
    )
    with pytest.raises(dec.DecisionLedgerError, match="contradicts its evidence"):
        _load(tmp_path, [entry])


def test_a_blocked_verdict_is_refused_when_a_blocking_symbol_is_absent_from_the_evidence(
    tmp_path: Path,
) -> None:
    entry = _safe_entry(tmp_path, verdict=dec.BLOCKED, revisit_after=None)
    entry["evidence"] = _write_evidence(tmp_path, PIN, "9.9.9", breaking=["SecurityType.Future"])
    entry["blocking_symbols"] = ["Shioaji.invented_symbol"]
    with pytest.raises(dec.DecisionLedgerError, match="not in"):
        _load(tmp_path, [entry])


def test_evidence_must_be_the_diff_from_the_pin_to_the_decided_version(tmp_path: Path) -> None:
    # A consecutive hop (1.7.2 -> 1.7.3) can be SAFE while the move from the pin
    # is BLOCKED, so it must not be usable as evidence for 1.7.3.
    entry = _safe_entry(tmp_path)
    entry["evidence"] = _write_evidence(tmp_path, "9.9.8", "9.9.9", breaking=[])
    with pytest.raises(dec.DecisionLedgerError, match="pin-to-candidate diff"):
        _load(tmp_path, [entry])


def test_evidence_that_does_not_exist_is_refused(tmp_path: Path) -> None:
    entry = _safe_entry(tmp_path, evidence=f"goldens/diff_{PIN}_to_9.9.9.json")
    (tmp_path / entry["evidence"]).unlink()
    with pytest.raises(dec.DecisionLedgerError, match="does not exist"):
        _load(tmp_path, [entry])


# --- a deferral must expire ---------------------------------------------------------


def test_a_defer_without_a_revisit_date_is_refused(tmp_path: Path) -> None:
    entry = _safe_entry(tmp_path, revisit_after=None)
    with pytest.raises(dec.DecisionLedgerError, match="revisit_after"):
        _load(tmp_path, [entry])


def test_a_revisit_date_on_or_before_the_decision_date_is_refused(tmp_path: Path) -> None:
    entry = _safe_entry(tmp_path, decided_on=date(2026, 8, 27), revisit_after=date(2026, 8, 27))
    with pytest.raises(dec.DecisionLedgerError, match="must be after decided_on"):
        _load(tmp_path, [entry])


def test_a_revisit_date_on_a_non_deferral_is_refused(tmp_path: Path) -> None:
    entry = _safe_entry(tmp_path, verdict=dec.ADOPT)
    with pytest.raises(dec.DecisionLedgerError, match="only meaningful on DEFER"):
        _load(tmp_path, [entry])


def test_a_deferral_is_current_before_its_revisit_date_and_expired_after(tmp_path: Path) -> None:
    _, records = _load(tmp_path, [_safe_entry(tmp_path)])
    decision = records["9.9.9"]
    assert decision.status(date(2026, 9, 25)) == dec.CURRENT
    assert decision.status(date(2026, 9, 27)) == dec.EXPIRED


# --- malformed ledgers fail closed ---------------------------------------------------


def test_a_misspelled_key_is_refused_rather_than_silently_dropped(tmp_path: Path) -> None:
    # `revist_after` would otherwise make a deferral permanent and invisible.
    entry = _safe_entry(tmp_path)
    entry["revist_after"] = date(2026, 9, 26)
    with pytest.raises(dec.DecisionLedgerError, match="unknown key"):
        _load(tmp_path, [entry])


def test_an_empty_rationale_is_refused(tmp_path: Path) -> None:
    entry = _safe_entry(tmp_path, rationale="   ")
    with pytest.raises(dec.DecisionLedgerError, match="rationale is empty"):
        _load(tmp_path, [entry])


def test_an_unknown_verdict_is_refused(tmp_path: Path) -> None:
    entry = _safe_entry(tmp_path, verdict="PROBABLY_FINE")
    with pytest.raises(dec.DecisionLedgerError, match="verdict must be one of"):
        _load(tmp_path, [entry])


def test_the_same_version_decided_twice_is_refused(tmp_path: Path) -> None:
    with pytest.raises(dec.DecisionLedgerError, match="decided twice"):
        _load(tmp_path, [_safe_entry(tmp_path), _safe_entry(tmp_path)])


def test_a_ledger_with_no_pin_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    path.write_text(yaml.safe_dump({"decisions": []}), encoding="utf-8")
    with pytest.raises(dec.DecisionLedgerError, match="no `pin`"):
        dec.load_decisions(path, repo_root=tmp_path)


def test_a_non_iso_decision_date_is_refused(tmp_path: Path) -> None:
    entry = _safe_entry(tmp_path, decided_on="27/08/2026")
    with pytest.raises(dec.DecisionLedgerError, match="not an ISO date"):
        _load(tmp_path, [entry])


# --- what the watch report does with a decision --------------------------------------


def _release(version: str, *, captured: bool, decision: Any = None) -> watch.Release:
    key = watch.parse_version(version)
    assert key is not None
    return watch.Release(version=version, key=key, gap=watch.PATCH, surface_captured=captured, decision=decision)


def test_a_release_with_a_surface_but_no_decision_still_needs_action(tmp_path: Path) -> None:
    # This is the hole the ledger exists to close: before it, capturing a
    # surface was enough to make the weekly watcher go quiet.
    report = watch.build_report(PIN, [_release("1.5.7", captured=True)], today=date(2026, 8, 27))
    assert report["releases"][0]["state"] == watch.UNDECIDED
    assert report["counts"]["undecided"] == 1
    assert report["counts"]["needs_action"] == 1


def test_a_release_with_a_current_decision_needs_no_action(tmp_path: Path) -> None:
    _, records = _load(tmp_path, [_safe_entry(tmp_path)])
    report = watch.build_report(
        PIN, [_release("9.9.9", captured=True, decision=records["9.9.9"])], today=date(2026, 9, 1)
    )
    assert report["releases"][0]["state"] == watch.SETTLED
    assert report["releases"][0]["verdict"] == dec.DEFER
    assert report["counts"]["needs_action"] == 0


def test_a_deferral_that_came_due_reopens_the_release(tmp_path: Path) -> None:
    _, records = _load(tmp_path, [_safe_entry(tmp_path)])
    row = [_release("9.9.9", captured=True, decision=records["9.9.9"])]
    assert watch.build_report(PIN, row, today=date(2026, 9, 25))["counts"]["needs_action"] == 0
    late = watch.build_report(PIN, row, today=date(2026, 9, 27))
    assert late["releases"][0]["state"] == watch.EXPIRED
    assert late["counts"]["expired"] == 1
    assert late["counts"]["needs_action"] == 1


def test_a_missing_surface_outranks_a_missing_decision_in_the_report(tmp_path: Path) -> None:
    report = watch.build_report(PIN, [_release("1.5.7", captured=False)], today=date(2026, 8, 27))
    assert report["releases"][0]["state"] == watch.NO_SURFACE
    assert report["counts"]["unassessed"] == 1
    assert report["counts"]["undecided"] == 0


def test_the_rendered_report_names_the_file_a_decision_goes_in(tmp_path: Path) -> None:
    report = watch.build_report(PIN, [_release("1.5.7", captured=True)], today=date(2026, 8, 27))
    text = watch.render_text(report)
    assert "No recorded decision for: 1.5.7" in text
    assert "docs/runbooks/shioaji-release-decisions.yaml" in text


def test_every_cited_diff_still_matches_what_the_committed_surfaces_produce() -> None:
    """A decision's evidence must be derivable, not hand-written.

    ``make shioaji-decision-evidence`` regenerates these from the surface
    snapshots. If a golden were edited by hand the verdict above it would rest
    on something no capture ever produced.
    """
    from scripts.shioaji_api_diff import report as report_mod

    ledger_pin, records = dec.load_decisions()
    for version, decision in sorted(records.items()):
        committed = json.loads((Path(dec.REPO_ROOT) / decision.evidence).read_text(encoding="utf-8"))
        rebuilt = report_mod.build_diff_doc(
            ledger_pin,
            version,
            json.loads((GOLDEN_DIR / f"surface_{ledger_pin}.json").read_text(encoding="utf-8")),
            json.loads((GOLDEN_DIR / f"surface_{version}.json").read_text(encoding="utf-8")),
        )
        assert committed == rebuilt, f"{decision.evidence} is not what the surfaces produce"
