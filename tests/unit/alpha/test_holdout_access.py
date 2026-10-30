"""Holdout access log (research v2 C3, plan scenario S7).

Every run that scores a Gate C out-of-sample holdout records the read under the
evidence root; a second read of the same holdout by the same alpha is flagged
(``reused``), never gated.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hft_platform.alpha._gate_c import _holdout_identity
from hft_platform.alpha._validation_types import GateReport, ValidationConfig
from hft_platform.alpha.evidence import HOLDOUT_ACCESS_RELPATH, EvidenceWriter, RunMode
from hft_platform.alpha.validation import _publish_governed_run, _record_holdout_access

_DAYS = ["2026-09-01", "2026-09-02", "2026-09-03"]


def _rows(days: list[str], tag: str = "e") -> list[dict]:
    return [{"date": d, "events_sha256": f"{tag}-{d}"} for d in days]


def _gate_c(key: str | None, *, reason: str | None = None) -> GateReport:
    holdout = {"key": key} if reason is None else {"key": key, "reason": reason}
    return GateReport(gate="Gate C", passed=False, details={"fill_backtest": {"holdout": holdout}})


def _config(run_mode: str = "exploratory") -> ValidationConfig:
    return ValidationConfig(alpha_id="alpha_x", data_paths=["d.npy"], run_mode=run_mode)


# ---------------------------------------------------------------------------
# Holdout identity
# ---------------------------------------------------------------------------


def test_holdout_identity_is_stable_for_the_same_days_and_events() -> None:
    first, second = _holdout_identity(_rows(_DAYS)), _holdout_identity(list(reversed(_rows(_DAYS))))
    assert first["key"] == second["key"] and first["key"]
    assert (first["n_days"], first["first_day"], first["last_day"]) == (3, "2026-09-01", "2026-09-03")


def test_holdout_identity_changes_with_the_days_or_the_events() -> None:
    base = _holdout_identity(_rows(_DAYS))["key"]
    assert _holdout_identity(_rows(_DAYS[:2]))["key"] != base
    assert _holdout_identity(_rows(_DAYS, tag="other"))["key"] != base


def _fill_eval_key(tmp_path: Path, train_payload: bytes, is_rows: list[dict]) -> str:
    from types import SimpleNamespace  # noqa: PLC0415

    from hft_platform.alpha._gate_c import _fill_backtest_eval  # noqa: PLC0415

    train = tmp_path / f"train_{len(train_payload)}.npz"
    train.write_bytes(train_payload)
    oos = tmp_path / "oos.npz"
    oos.write_bytes(b"oos events")
    rows = [*is_rows, *_rows(_DAYS)]
    for r in rows:
        r.update(net_pts=1.0, pnl_pts=1.0, n_contracts=0, fills=0)
    sim = SimpleNamespace(
        method="taker_ioc_touch_v1",
        daily_rows=lambda: rows,
        n_is_days=lambda split: len(is_rows),
        sharpe_split=lambda split: (0.0, 0.0),
    )
    _, details, _ = _fill_backtest_eval(
        lambda *a: sim,
        alpha=None,
        cfg=None,
        hbt_paths=[str(train), str(oos)],
        cost_model=SimpleNamespace(label="TEST"),
        is_oos_split=0.5,
        sharpe_oos_min=0.0,
    )
    return details["holdout"]["key"]


def test_holdout_key_survives_a_change_to_the_training_data(tmp_path: Path) -> None:
    before = _fill_eval_key(tmp_path, b"train v1", _rows(["2026-08-28"], tag="train-v1"))
    after = _fill_eval_key(tmp_path, b"train v2 revised", _rows(["2026-08-27", "2026-08-28"], tag="train-v2"))
    assert before and before == after


def test_events_hash_names_the_rows_not_the_container(tmp_path: Path) -> None:
    from research.backtest.taker_sim import events_sha256  # noqa: PLC0415

    np.savez_compressed(tmp_path / "a.npz", data=np.arange(6, dtype=np.int64))
    np.savez(tmp_path / "b.npz", data=np.arange(6, dtype=np.int64))
    np.savez_compressed(tmp_path / "c.npz", data=np.arange(7, dtype=np.int64))
    assert events_sha256(str(tmp_path / "a.npz")) == events_sha256(str(tmp_path / "b.npz"))
    assert events_sha256(str(tmp_path / "a.npz")) != events_sha256(str(tmp_path / "c.npz"))


def test_holdout_identity_without_days_or_hashes_cannot_be_recorded() -> None:
    assert _holdout_identity([])["key"] is None
    unhashed = _holdout_identity([{"date": d} for d in _DAYS])
    assert unhashed["key"] is None and "no event hash" in unhashed["reason"]


# ---------------------------------------------------------------------------
# Evidence log
# ---------------------------------------------------------------------------


def test_first_read_is_not_reused(tmp_path: Path) -> None:
    access = EvidenceWriter(tmp_path).record_holdout_access(
        holdout_key="k1", alpha_id="alpha_x", run_id="r1", mode=RunMode.GOVERNED
    )
    assert access == {
        "recorded": True,
        "holdout_key": "k1",
        "prior_reads": 0,
        "prior_reads_any_alpha": 0,
        "prior_run_ids": [],
        "reused": False,
    }


def test_second_read_of_the_same_holdout_is_flagged(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    writer.record_holdout_access(holdout_key="k1", alpha_id="alpha_x", run_id="r1", mode=RunMode.EXPLORATORY)
    access = writer.record_holdout_access(holdout_key="k1", alpha_id="alpha_x", run_id="r2", mode=RunMode.GOVERNED)
    assert access["reused"] is True
    assert access["prior_reads"] == 1
    assert access["prior_run_ids"] == ["r1"]
    rows = list(writer.read_holdout_accesses())
    assert [(r["run_id"], r["mode"]) for r in rows] == [("r1", "exploratory"), ("r2", "governed")]


def test_another_alpha_or_holdout_is_not_this_alphas_reuse(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    writer.record_holdout_access(holdout_key="k1", alpha_id="alpha_y", run_id="r1", mode=RunMode.EXPLORATORY)
    writer.record_holdout_access(holdout_key="k2", alpha_id="alpha_x", run_id="r2", mode=RunMode.EXPLORATORY)
    access = writer.record_holdout_access(holdout_key="k1", alpha_id="alpha_x", run_id="r3", mode=RunMode.EXPLORATORY)
    assert access["reused"] is False
    assert access["prior_reads_any_alpha"] == 1


def test_holdout_access_needs_a_key_and_an_alpha(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="holdout_key and alpha_id"):
        EvidenceWriter(tmp_path).record_holdout_access(
            holdout_key="", alpha_id="alpha_x", run_id=None, mode=RunMode.EXPLORATORY
        )


# ---------------------------------------------------------------------------
# Validation wiring
# ---------------------------------------------------------------------------


def test_governed_run_records_under_its_writer(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path / "evidence")
    _record_holdout_access(writer, _config("governed"), _gate_c("k1"), "r1")
    access = _record_holdout_access(writer, _config("governed"), _gate_c("k1"), "r2")
    assert access["reused"] is True
    assert (tmp_path / "evidence" / HOLDOUT_ACCESS_RELPATH).is_file()


def test_exploratory_run_records_when_the_evidence_root_exists(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    monkeypatch.setenv("HFT_RESEARCH_EVIDENCE_ROOT", str(root))
    _record_holdout_access(None, _config(), _gate_c("k1"), "r1")
    access = _record_holdout_access(None, _config(), _gate_c("k1"), "r2")
    assert access["reused"] is True
    assert [r["mode"] for r in EvidenceWriter(root).read_holdout_accesses()] == ["exploratory", "exploratory"]


def test_exploratory_run_without_an_evidence_root_says_it_was_not_recorded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HFT_RESEARCH_EVIDENCE_ROOT", str(tmp_path / "absent"))
    access = _record_holdout_access(None, _config(), _gate_c("k1"), "r1")
    assert access["recorded"] is False and "no evidence root" in access["reason"]
    assert not (tmp_path / "absent").exists()


def test_relative_evidence_root_is_not_recorded(monkeypatch) -> None:
    monkeypatch.setenv("HFT_RESEARCH_EVIDENCE_ROOT", "relative/evidence")
    access = _record_holdout_access(None, _config(), _gate_c("k1"), "r1")
    assert access["recorded"] is False and "relative" in access["reason"]


def test_unscored_holdout_is_not_recorded(tmp_path: Path) -> None:
    writer = EvidenceWriter(tmp_path)
    assert _record_holdout_access(writer, _config("governed"), _gate_c(None, reason="no out-of-sample days"), "r1") == {
        "recorded": False,
        "reason": "no out-of-sample days",
    }
    no_fill = GateReport(gate="Gate C", passed=False, details={})
    assert _record_holdout_access(writer, _config("governed"), no_fill, "r1")["recorded"] is False
    assert not (tmp_path / HOLDOUT_ACCESS_RELPATH).exists()


def test_governed_ledger_event_carries_the_reuse_flag(tmp_path: Path, monkeypatch) -> None:
    writer = EvidenceWriter(tmp_path / "evidence")
    validation_dir = tmp_path / "evidence" / "validations" / "alpha_x" / "s1"
    validation_dir.mkdir(parents=True)
    (validation_dir / "backtest_report.json").write_text("{}", encoding="utf-8")
    gate_c = _gate_c("k1")
    gate_c.details["verdict"] = {"verdict": "KILL"}
    writer.record_holdout_access(holdout_key="k1", alpha_id="alpha_x", run_id="r0", mode=RunMode.EXPLORATORY)
    gate_c.details["holdout_access"] = _record_holdout_access(writer, _config("governed"), gate_c, "r1")
    monkeypatch.setattr("hft_platform.alpha.validation.git_state", lambda _root: ("abc123", False))
    _publish_governed_run(
        writer,
        config=_config("governed"),
        root=tmp_path,
        validation_dir=validation_dir,
        reports={"gate_c": gate_c},
        run_id="r1",
        experiment_meta_path=None,
        start_commit="abc123",
    )
    (event,) = list(writer.read_verdicts())
    assert event["holdout"] == {"holdout_key": "k1", "prior_reads": 1, "reused": True}
