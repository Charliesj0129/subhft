"""Dual-sink CH writer: dedupe pre-check, jsonl fallback, replay idempotency."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace

from research.candidate_loop.ch_writer import (
    CANDIDATE_COLUMNS,
    CANDIDATES_TABLE,
    RESULT_COLUMNS,
    RESULTS_TABLE,
    ResultWriter,
    compute_result_id,
    replay_fallback,
)


class _StubCH:
    """In-memory CH double honoring the writer's pre-check + insert protocol."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.rows: dict[str, list[dict]] = {CANDIDATES_TABLE: [], RESULTS_TABLE: []}

    def query(self, sql: str, parameters: dict | None = None) -> SimpleNamespace:
        if self.fail:
            raise ConnectionError("ch down")
        parameters = parameters or {}
        if "result_id" in sql:
            count = sum(1 for r in self.rows[RESULTS_TABLE] if r["result_id"] == parameters["id"])
        else:
            count = sum(
                1
                for r in self.rows[CANDIDATES_TABLE]
                if r["alpha_id"] == parameters["a"]
                and r["run_id"] == parameters["r"]
                and r["status"] == parameters["s"]
            )
        return SimpleNamespace(result_rows=[[count]])

    def insert(self, table: str, rows: list[list], column_names: list[str]) -> None:
        if self.fail:
            raise ConnectionError("ch down")
        for values in rows:
            self.rows[table].append(dict(zip(column_names, values)))


def _result_row(alpha_id: str = "a1", run_id: str = "smoke_001") -> dict:
    return {
        "result_id": compute_result_id(alpha_id, run_id, "train", "dv", "ev", "sv"),
        "run_id": run_id,
        "alpha_id": alpha_id,
        "family": "microprice",
        "split": "train",
        "ic": 0.05,
        "gates_failed": [],
    }


def _candidate_row(alpha_id: str = "a1", status: str = "NEW") -> dict:
    return {"alpha_id": alpha_id, "run_id": "smoke_001", "name": "probe", "status": status}


class TestResultId:
    def test_deterministic(self) -> None:
        assert compute_result_id("a", "r", "train", "d", "e", "s") == compute_result_id(
            "a", "r", "train", "d", "e", "s"
        )

    def test_any_component_changes_id(self) -> None:
        base = compute_result_id("a", "r", "train", "d", "e", "s")
        assert base != compute_result_id("a", "r", "validation", "d", "e", "s")
        assert base != compute_result_id("a", "r", "train", "d2", "e", "s")


class TestChSink:
    def test_insert_then_duplicate(self, tmp_path: Path) -> None:
        ch = _StubCH()
        writer = ResultWriter(ch, tmp_path)
        assert writer.write_result_row(_result_row()) == "ch"
        assert writer.write_result_row(_result_row()) == "duplicate"
        assert len(ch.rows[RESULTS_TABLE]) == 1
        assert set(ch.rows[RESULTS_TABLE][0]) == set(RESULT_COLUMNS)

    def test_candidate_status_transitions_dedupe_per_status(self, tmp_path: Path) -> None:
        ch = _StubCH()
        writer = ResultWriter(ch, tmp_path)
        assert writer.write_candidate_row(_candidate_row(status="NEW")) == "ch"
        assert writer.write_candidate_row(_candidate_row(status="NEW")) == "duplicate"
        assert writer.write_candidate_row(_candidate_row(status="REJECTED")) == "ch"
        assert len(ch.rows[CANDIDATES_TABLE]) == 2

    def test_missing_columns_get_typed_defaults(self, tmp_path: Path) -> None:
        ch = _StubCH()
        ResultWriter(ch, tmp_path).write_result_row(_result_row())
        row = ch.rows[RESULTS_TABLE][0]
        assert row["gates_passed"] == []
        assert row["day_count"] == 0
        assert row["final_score"] == 0.0
        assert row["artifact_path"] == ""
        cand_ch = _StubCH()
        ResultWriter(cand_ch, tmp_path).write_candidate_row(_candidate_row())
        cand = cand_ch.rows[CANDIDATES_TABLE][0]
        assert cand["feature_formulas"] == []
        assert cand["uses_trade_imbalance"] == 0
        assert set(cand) == set(CANDIDATE_COLUMNS)


class TestDateColumns:
    def test_iso_string_dates_reach_ch_as_date_objects(self, tmp_path: Path) -> None:
        ch = _StubCH()
        row = _result_row()
        row["split_start"] = "2026-01-26"
        row["split_end"] = "2026-04-13"
        ResultWriter(ch, tmp_path).write_result_row(row)
        stored = ch.rows[RESULTS_TABLE][0]
        assert stored["split_start"] == datetime.date(2026, 1, 26)
        assert stored["split_end"] == datetime.date(2026, 4, 13)

    def test_missing_or_empty_dates_default_to_epoch(self, tmp_path: Path) -> None:
        ch = _StubCH()
        row = _result_row()
        row["split_start"] = ""
        ResultWriter(ch, tmp_path).write_result_row(row)
        stored = ch.rows[RESULTS_TABLE][0]
        assert stored["split_start"] == datetime.date(1970, 1, 1)
        assert stored["split_end"] == datetime.date(1970, 1, 1)

    def test_replay_coerces_fallback_iso_strings(self, tmp_path: Path) -> None:
        offline = ResultWriter(None, tmp_path)
        row = _result_row()
        row["split_start"] = datetime.date(2026, 1, 26)  # json.dumps default=str -> ISO
        offline.write_result_row(row)
        ch = _StubCH()
        counts = replay_fallback(ch, offline.fallback_path)
        assert counts["inserted"] == 1
        assert ch.rows[RESULTS_TABLE][0]["split_start"] == datetime.date(2026, 1, 26)


class TestJsonlFallback:
    def test_ch_failure_falls_back_to_jsonl(self, tmp_path: Path) -> None:
        writer = ResultWriter(_StubCH(fail=True), tmp_path)
        assert writer.write_result_row(_result_row()) == "jsonl"
        lines = writer.fallback_path.read_text().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["table"] == RESULTS_TABLE
        assert entry["row"]["alpha_id"] == "a1"

    def test_no_client_writes_jsonl_with_dedupe(self, tmp_path: Path) -> None:
        writer = ResultWriter(None, tmp_path)
        assert writer.write_result_row(_result_row()) == "jsonl"
        assert writer.write_result_row(_result_row()) == "duplicate"
        assert len(writer.fallback_path.read_text().splitlines()) == 1

    def test_dedupe_survives_new_writer_instance(self, tmp_path: Path) -> None:
        ResultWriter(None, tmp_path).write_result_row(_result_row())
        assert ResultWriter(None, tmp_path).write_result_row(_result_row()) == "duplicate"


class TestReplay:
    def test_replay_flushes_then_noops(self, tmp_path: Path) -> None:
        offline = ResultWriter(None, tmp_path)
        offline.write_result_row(_result_row("a1"))
        offline.write_result_row(_result_row("a2"))
        offline.write_candidate_row(_candidate_row("a1"))

        ch = _StubCH()
        counts = replay_fallback(ch, offline.fallback_path)
        assert counts == {"inserted": 3, "duplicate": 0, "failed": 0}
        assert len(ch.rows[RESULTS_TABLE]) == 2
        assert len(ch.rows[CANDIDATES_TABLE]) == 1

        counts_again = replay_fallback(ch, offline.fallback_path)
        assert counts_again == {"inserted": 0, "duplicate": 3, "failed": 0}

    def test_replay_missing_file_is_empty(self, tmp_path: Path) -> None:
        counts = replay_fallback(_StubCH(), tmp_path / "nope.jsonl")
        assert counts == {"inserted": 0, "duplicate": 0, "failed": 0}

    def test_replay_skips_corrupt_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "_results_fallback.jsonl"
        path.write_text("{not json}\n" + json.dumps({"table": "bogus.table", "row": {}}) + "\n")
        counts = replay_fallback(_StubCH(), path)
        assert counts["failed"] == 2
