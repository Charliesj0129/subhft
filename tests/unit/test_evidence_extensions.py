"""Tests for AutonomyEvidenceWriter extensions: set_trading_date, on_transition, write_daily_summary."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from hft_platform.ops.evidence import AutonomyEvidenceWriter


@pytest.fixture()
def writer(tmp_path: Path) -> AutonomyEvidenceWriter:
    return AutonomyEvidenceWriter(base_dir=tmp_path)


class TestSetTradingDate:
    def test_session_dir_uses_trading_date(self, writer: AutonomyEvidenceWriter, tmp_path: Path) -> None:
        d = date(2026, 3, 25)
        writer.set_trading_date(d)
        assert writer.session_dir == tmp_path / "20260325"

    def test_session_dir_defaults_to_today(self, tmp_path: Path) -> None:
        w = AutonomyEvidenceWriter(base_dir=tmp_path)
        # Should not raise and should return a date-formatted dir
        session_dir = w.session_dir
        assert session_dir.name.isdigit()
        assert len(session_dir.name) == 8


class TestOnTransitionCallback:
    def test_callback_invoked_on_record_transition(self, writer: AutonomyEvidenceWriter) -> None:
        captured: list[dict] = []
        writer.on_transition(lambda record: captured.append(record))

        writer.record_transition(
            scope="platform",
            mode="PLATFORM_REDUCE_ONLY",
            reason="test_reason",
            manual_rearm_required=False,
        )

        assert len(captured) == 1
        assert captured[0]["scope"] == "platform"
        assert captured[0]["reason"] == "test_reason"

    def test_multiple_callbacks_all_invoked(self, writer: AutonomyEvidenceWriter) -> None:
        counts = [0, 0]
        writer.on_transition(lambda _: counts.__setitem__(0, counts[0] + 1))
        writer.on_transition(lambda _: counts.__setitem__(1, counts[1] + 1))

        writer.record_transition(scope="platform", mode="HALT", reason="r")

        assert counts == [1, 1]

    def test_callback_exception_does_not_propagate(self, writer: AutonomyEvidenceWriter) -> None:
        def bad_callback(_record: dict) -> None:
            raise RuntimeError("boom")

        writer.on_transition(bad_callback)
        # Should not raise
        record = writer.record_transition(scope="platform", mode="HALT", reason="r")
        assert record["scope"] == "platform"


class TestWriteDailySummary:
    def test_writes_json_file(self, writer: AutonomyEvidenceWriter) -> None:
        writer.set_trading_date(date(2026, 3, 25))
        summary = {"pnl_ntd": 1000, "fills": 42}
        path = writer.write_daily_summary(summary)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["pnl_ntd"] == 1000
        assert data["fills"] == 42
