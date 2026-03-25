"""Tests for AutonomyEvidenceWriter extensions (trading date, callbacks, daily summary)."""

from __future__ import annotations

import json
from datetime import date

from hft_platform.ops.evidence import AutonomyEvidenceWriter


class TestTradingDate:
    def test_session_dir_uses_trading_date_when_set(self, tmp_path) -> None:
        writer = AutonomyEvidenceWriter(base_dir=tmp_path)
        td = date(2026, 3, 25)
        writer.set_trading_date(td)
        assert writer.session_dir == tmp_path / "20260325"

    def test_session_dir_uses_today_when_no_trading_date(self, tmp_path) -> None:
        writer = AutonomyEvidenceWriter(base_dir=tmp_path)
        # _trading_date is None by default
        session_dir = writer.session_dir
        # Should be a date-formatted directory name (YYYYMMDD)
        assert len(session_dir.name) == 8
        assert session_dir.name.isdigit()

    def test_init_has_on_transition_list(self, tmp_path) -> None:
        writer = AutonomyEvidenceWriter(base_dir=tmp_path)
        assert isinstance(writer.on_transition, list)
        assert len(writer.on_transition) == 0


class TestTransitionCallbacks:
    def test_callback_invoked_on_record_transition(self, tmp_path) -> None:
        writer = AutonomyEvidenceWriter(base_dir=tmp_path)
        writer.set_trading_date(date(2026, 1, 1))
        captured: list = []
        writer.on_transition.append(lambda rec: captured.append(rec))

        writer.record_transition(
            scope="platform",
            mode="HALT",
            reason="test",
            manual_rearm_required=False,
        )
        assert len(captured) == 1
        assert captured[0]["scope"] == "platform"
        assert captured[0]["mode"] == "HALT"

    def test_failing_callback_does_not_raise(self, tmp_path) -> None:
        writer = AutonomyEvidenceWriter(base_dir=tmp_path)
        writer.set_trading_date(date(2026, 1, 1))
        writer.on_transition.append(lambda _rec: 1 / 0)  # will raise ZeroDivisionError

        # Should not raise
        record = writer.record_transition(
            scope="platform", mode="NORMAL", reason="test", manual_rearm_required=False,
        )
        assert record["scope"] == "platform"


class TestDailySummary:
    def test_write_daily_summary_creates_file(self, tmp_path) -> None:
        writer = AutonomyEvidenceWriter(base_dir=tmp_path)
        writer.set_trading_date(date(2026, 3, 25))
        summary = {"transitions": 3, "halts": 1, "final_mode": "NORMAL"}
        writer.write_daily_summary(summary)

        path = tmp_path / "20260325" / "daily_summary.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["transitions"] == 3
        assert data["halts"] == 1
