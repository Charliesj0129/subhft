"""Unit tests for ``hft_platform.replay.eligibility`` (loop_v1 L4).

Covers each branch of ``check_eligibility`` and the ``_count_live_intents``
helper directly with a fake CK client, so the L4 gate is exercised by the
unit-test coverage floor (the integration test path is gated out of
``coverage-domain``).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from hft_platform.replay.eligibility import (
    Eligible,
    IneligibleNoFixture,
    IneligiblePreRecorder,
    _count_live_intents,
    check_eligibility,
)


class _FakeCKResult:
    def __init__(self, rows: list[tuple]) -> None:
        self.result_rows = rows


class _FakeCKClient:
    def __init__(self, rows: list[tuple] | Exception) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict]] = []

    def query(self, q: str, parameters: dict | None = None) -> _FakeCKResult:
        self.calls.append((q, parameters or {}))
        if isinstance(self._rows, Exception):
            raise self._rows
        return _FakeCKResult(self._rows)


def test_count_live_intents_returns_zero_for_empty_rows() -> None:
    client = _FakeCKClient(rows=[])
    n = _count_live_intents(client, date(2026, 4, 21), "ECHO")
    assert n == 0
    # Verify the partition-aligned predicate was used.
    sql, params = client.calls[0]
    assert "ingest_ts" in sql
    assert params == {"d": "2026-04-21", "s": "ECHO"}


def test_count_live_intents_returns_first_column_of_first_row() -> None:
    client = _FakeCKClient(rows=[(7,)])
    assert _count_live_intents(client, date(2026, 5, 5), "R47_MAKER_TMF") == 7


def test_count_live_intents_handles_missing_result_rows_attribute() -> None:
    class _NoAttrResult:
        pass

    class _NoAttrClient:
        def query(self, q: str, parameters: dict | None = None) -> _NoAttrResult:
            return _NoAttrResult()

    assert _count_live_intents(_NoAttrClient(), date(2026, 5, 5), "X") == 0


def test_check_eligibility_no_fixture(tmp_path: Path) -> None:
    missing = tmp_path / "absent.tar.gz"
    result = check_eligibility(
        session_date=date(2026, 4, 21),
        strategy_id="ECHO",
        fixture_path=missing,
        ck_client=_FakeCKClient(rows=[(99,)]),
    )
    assert isinstance(result, IneligibleNoFixture)
    assert result.fixture_path == str(missing)


def test_check_eligibility_pre_recorder_when_count_is_zero(tmp_path: Path) -> None:
    fp = tmp_path / "wal.tar.gz"
    fp.write_bytes(b"placeholder-fixture")
    client = _FakeCKClient(rows=[(0,)])

    result = check_eligibility(
        session_date=date(2026, 4, 21),
        strategy_id="ECHO",
        fixture_path=fp,
        ck_client=client,
    )
    assert isinstance(result, IneligiblePreRecorder)
    assert "no_intents_recorded_for_2026-04-21" in result.reason
    assert "ECHO" in result.reason


def test_check_eligibility_eligible_when_rows_present(tmp_path: Path) -> None:
    fp = tmp_path / "wal.tar.gz"
    fp.write_bytes(b"x")
    client = _FakeCKClient(rows=[(42,)])

    result = check_eligibility(
        session_date=date(2026, 5, 5),
        strategy_id="R47_MAKER_TMF",
        fixture_path=fp,
        ck_client=client,
    )
    assert isinstance(result, Eligible)
    assert result.n_live_intents == 42


def test_check_eligibility_ck_failure_falls_back_to_pre_recorder(tmp_path: Path) -> None:
    fp = tmp_path / "wal.tar.gz"
    fp.write_bytes(b"x")
    client = _FakeCKClient(rows=RuntimeError("ck unreachable"))

    result = check_eligibility(
        session_date=date(2026, 5, 5),
        strategy_id="ECHO",
        fixture_path=fp,
        ck_client=client,
    )
    assert isinstance(result, IneligiblePreRecorder)
    assert "intent_recorder_query_failed" in result.reason
    assert "RuntimeError" in result.reason
