"""Tests for DailyReportService — triggered on SessionGovernor CLOSED callback."""

from __future__ import annotations

import asyncio
from typing import Any

from hft_platform.ops.session_governor import SessionPhase

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeNotificationDispatcher:
    """Records notify_daily_report calls without sending anything."""

    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def notify_daily_report(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class FakeEvidenceWriter:
    """Records write_daily_summary calls."""

    __slots__ = ("summaries",)

    def __init__(self) -> None:
        self.summaries: list[dict[str, Any]] = []

    def write_daily_summary(self, summary: dict[str, Any]) -> str:
        self.summaries.append(summary)
        return "/fake/evidence/daily_summary.json"


class _FakePosition:
    """Minimal Position stand-in with net_qty."""

    __slots__ = ("net_qty",)

    def __init__(self, net_qty: int) -> None:
        self.net_qty = net_qty


class FakePositionStore:
    """Returns canned positions via snapshot_positions() + total_pnl."""

    __slots__ = ("_positions", "_total_pnl")

    def __init__(
        self,
        positions: dict[str, int] | None = None,
        total_pnl: int = 0,
    ) -> None:
        self._positions = {k: _FakePosition(v) for k, v in (positions or {}).items()}
        self._total_pnl = total_pnl

    def snapshot_positions(self) -> dict[str, Any]:
        return dict(self._positions)

    @property
    def total_pnl(self) -> int:
        return self._total_pnl


class FakeStormGuard:
    """Returns a canned state."""

    __slots__ = ("state",)

    def __init__(self, state_name: str = "NORMAL") -> None:
        self.state = type("_FakeState", (), {"name": state_name})()


class FakeCHClient:
    """Fake ClickHouse client that returns canned query results."""

    __slots__ = ("_rows", "_should_fail")

    def __init__(
        self,
        rows: list[tuple[Any, ...]] | None = None,
        *,
        should_fail: bool = False,
    ) -> None:
        self._rows = rows or []
        self._should_fail = should_fail

    def command(self, query: str, parameters: dict[str, Any] | None = None) -> Any:
        if self._should_fail:
            raise ConnectionError("ClickHouse unavailable")
        if not self._rows:
            return None
        # Return single row for aggregate queries
        return self._rows[0]

    def query(self, query: str, parameters: dict[str, Any] | None = None) -> Any:
        if self._should_fail:
            raise ConnectionError("ClickHouse unavailable")
        return type("_Result", (), {"result_rows": self._rows})()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    *,
    ch_client: Any = None,
    notification_dispatcher: Any = None,
    evidence_writer: Any = None,
    position_store: Any = None,
    storm_guard: Any = None,
) -> Any:
    from hft_platform.services.daily_report import DailyReportService

    return DailyReportService(
        ch_client=ch_client or FakeCHClient(),
        notification_dispatcher=notification_dispatcher or FakeNotificationDispatcher(),
        evidence_writer=evidence_writer or FakeEvidenceWriter(),
        position_store=position_store or FakePositionStore(),
        storm_guard=storm_guard or FakeStormGuard(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDailyReportService:
    """DailyReportService unit tests."""

    def test_generate_and_send_report(self) -> None:
        """CLOSED callback triggers report with correct aggregates from CH + PnL from store."""
        # Aggregate row: (buy_count, sell_count, fill_count, sum_fee)
        ch = FakeCHClient(rows=[(10, 8, 18, 3200)])
        dispatcher = FakeNotificationDispatcher()
        evidence = FakeEvidenceWriter()
        # total_pnl in scaled int (x10000): 500000 → 50 NTD
        position_store = FakePositionStore(total_pnl=500000)
        svc = _make_service(
            ch_client=ch,
            notification_dispatcher=dispatcher,
            evidence_writer=evidence,
            position_store=position_store,
        )

        asyncio.run(svc.on_session_closed("equity"))

        assert len(dispatcher.calls) == 1
        call = dispatcher.calls[0]
        assert call["pnl_ntd"] == 50  # 500000 // 10000
        assert call["buys"] == 10
        assert call["sells"] == 8
        assert call["fills"] == 18

    def test_evidence_written_on_close(self) -> None:
        """Evidence summary JSON is written alongside the notification."""
        ch = FakeCHClient(rows=[(5, 3, 8, 100)])
        evidence = FakeEvidenceWriter()
        position_store = FakePositionStore(total_pnl=10000000)  # 1000 NTD
        svc = _make_service(ch_client=ch, evidence_writer=evidence, position_store=position_store)

        asyncio.run(svc.on_session_closed("equity"))

        assert len(evidence.summaries) == 1
        summary = evidence.summaries[0]
        assert summary["pnl_ntd"] == 1000
        assert summary["fills"] == 8
        assert "date" in summary

    def test_no_crash_on_empty_data(self) -> None:
        """Report handles zero-fill days gracefully (empty CH result)."""
        ch = FakeCHClient(rows=[])
        dispatcher = FakeNotificationDispatcher()
        svc = _make_service(ch_client=ch, notification_dispatcher=dispatcher)

        asyncio.run(svc.on_session_closed("equity"))

        assert len(dispatcher.calls) == 1
        call = dispatcher.calls[0]
        assert call["pnl_ntd"] == 0
        assert call["fills"] == 0

    def test_ch_failure_sends_zeroed_report(self) -> None:
        """ClickHouse query failure sends report with zero values."""
        ch = FakeCHClient(should_fail=True)
        dispatcher = FakeNotificationDispatcher()
        svc = _make_service(ch_client=ch, notification_dispatcher=dispatcher)

        asyncio.run(svc.on_session_closed("equity"))

        assert len(dispatcher.calls) == 1
        call = dispatcher.calls[0]
        assert call["pnl_ntd"] == 0
        assert call["buys"] == 0
        assert call["sells"] == 0
        assert call["fills"] == 0

    def test_phase_callback_filters_non_closed(self) -> None:
        """Phase callback only triggers report on CLOSED phase."""
        dispatcher = FakeNotificationDispatcher()
        svc = _make_service(notification_dispatcher=dispatcher)

        # Non-CLOSED phases should not trigger
        svc.on_phase_transition("equity", SessionPhase.OPEN, SessionPhase.CLOSE_ONLY)

        assert len(dispatcher.calls) == 0

    def test_phase_callback_triggers_on_closed(self) -> None:
        """Phase callback triggers report when phase is CLOSED."""
        ch = FakeCHClient(rows=[(0, 0, 0, 0)])
        dispatcher = FakeNotificationDispatcher()
        svc = _make_service(ch_client=ch, notification_dispatcher=dispatcher)

        svc.on_phase_transition("equity", SessionPhase.FORCE_FLAT, SessionPhase.CLOSED)

        assert len(dispatcher.calls) == 1

    def test_position_status_flat(self) -> None:
        """Position status reports 'flat' when no open positions."""
        svc = _make_service(position_store=FakePositionStore({}))
        status = svc._get_position_status()
        assert status == "flat"

    def test_position_status_open(self) -> None:
        """Position status reports count when non-zero positions exist."""
        svc = _make_service(position_store=FakePositionStore({"acct:s:2330": 100, "acct:s:2317": -50}))
        status = svc._get_position_status()
        assert "2 open" in status

    def test_phase_callback_handles_string_phase(self) -> None:
        """Phase callback handles string phase names (not just enum)."""
        ch = FakeCHClient(rows=[(0, 0, 0, 0)])
        dispatcher = FakeNotificationDispatcher()
        svc = _make_service(ch_client=ch, notification_dispatcher=dispatcher)

        # Simulate string phase (defensive handling)
        svc.on_phase_transition("equity", "FORCE_FLAT", "CLOSED")

        assert len(dispatcher.calls) == 1
