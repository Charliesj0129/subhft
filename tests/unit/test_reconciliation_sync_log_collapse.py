"""17,274 sync cycles a day, all of them describing an empty portfolio.

THESHOW's 24-hour log profile 2026-08-12: 108,302 info + 69,751 debug lines.
``Portfolio Sync`` alone accounts for ~86k of them — one info line per cycle at
the 5 s default interval, plus four debug lines, and the four *are* essentially
the entire debug channel (4 x 17,274 = 69,096 vs 69,751 debug lines total).

Every one of those cycles reported the same thing: ``positions={}``. The broker
has held no position for the whole window. So the debug channel — the thing you
turn on when something is wrong — is unusable, drowned by a routine that had
nothing to say.

Collapsing repeats has one hazard worth naming, because this repo has been
caught by it before (``theshow_quote_facade_stranded``: aggregate rates hid a
dead shard): silence must not become indistinguishable from a dead loop. So the
rule here is *change or heartbeat*, never change alone — an unchanging portfolio
still logs on a fixed cadence, the collapsed cycles are counted and reported on
the next line, and ``reconciliation_last_success_ts`` remains the metric-side
liveness signal. Warning and error paths are untouched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import ReconciliationService


class _Client:
    """Broker stub whose positions the test can change between cycles."""

    def __init__(self, positions: list[Any] | None = None) -> None:
        self.positions = positions if positions is not None else []

    def get_positions(self) -> list[Any]:
        return list(self.positions)


def _position(symbol: str, qty: int) -> Any:
    pos = MagicMock()
    pos.code = symbol
    pos.quantity = qty
    pos.direction = "Buy" if qty > 0 else "Sell"
    return pos


def _service(client: _Client, **recon_cfg: Any) -> ReconciliationService:
    cfg = {"reconciliation": {"check_interval_s": 5, **recon_cfg}}
    return ReconciliationService(
        client=client,
        position_store=PositionStore(),
        config=cfg,
        storm_guard=MagicMock(),
    )


async def _cycles(service: ReconciliationService, n: int) -> None:
    for _ in range(n):
        await service.sync_portfolio()


# --------------------------------------------------------------------------- #
# The noise                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unchanged_empty_portfolio_stops_repeating_itself() -> None:
    """The measured symptom: 20 cycles of nothing produced 20 identical
    completion lines."""
    service = _service(_Client(), sync_log_heartbeat_s=3600)

    with patch("hft_platform.execution.reconciliation.logger") as log:
        await _cycles(service, 20)

    completions = [c for c in log.info.call_args_list if c[0][0].startswith("Portfolio Sync Complete")]
    assert len(completions) == 1, f"expected one line for 20 identical cycles, got {len(completions)}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_per_cycle_debug_detail_collapses_too() -> None:
    """The debug lines are the larger half of the 86k and describe the same
    unchanged state."""
    service = _service(_Client(), sync_log_heartbeat_s=3600)

    with patch("hft_platform.execution.reconciliation.logger") as log:
        await _cycles(service, 20)

    for event in (
        "Portfolio Sync: Broker State",
        "Portfolio Sync: Local State",
        "Portfolio Sync: Per-strategy position breakdown",
    ):
        emitted = [c for c in log.debug.call_args_list if c[0][0] == event]
        assert len(emitted) == 1, f"{event} emitted {len(emitted)} times for 20 identical cycles"


# --------------------------------------------------------------------------- #
# …without becoming silence                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_changed_position_logs_immediately() -> None:
    """A collapse keyed on time alone would delay the one cycle an operator
    actually wants to see."""
    client = _Client()
    service = _service(client, sync_log_heartbeat_s=3600)

    with patch("hft_platform.execution.reconciliation.logger") as log:
        await _cycles(service, 5)
        client.positions = [_position("TMFH6", 1)]
        await service.sync_portfolio()

    states = [c for c in log.debug.call_args_list if c[0][0] == "Portfolio Sync: Broker State"]
    assert len(states) == 2
    assert states[-1][1]["positions"] == {"TMFH6": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_returning_position_is_not_mistaken_for_the_old_state() -> None:
    """Signature comparison is against the previous *logged* state, so a
    flip back to a state seen earlier still logs."""
    client = _Client()
    service = _service(client, sync_log_heartbeat_s=3600)

    with patch("hft_platform.execution.reconciliation.logger") as log:
        await service.sync_portfolio()
        client.positions = [_position("TMFH6", 1)]
        await service.sync_portfolio()
        client.positions = []
        await service.sync_portfolio()

    states = [c for c in log.debug.call_args_list if c[0][0] == "Portfolio Sync: Broker State"]
    assert [s[1]["positions"] for s in states] == [{}, {"TMFH6": 1}, {}]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unchanging_portfolio_still_logs_on_the_heartbeat() -> None:
    """The hazard being avoided: a collapsed log that goes quiet forever cannot
    be told apart from a loop that died."""
    service = _service(_Client(), sync_log_heartbeat_s=0.0)

    with patch("hft_platform.execution.reconciliation.logger") as log:
        await _cycles(service, 4)

    completions = [c for c in log.info.call_args_list if c[0][0].startswith("Portfolio Sync Complete")]
    assert len(completions) == 4, "a zero-length heartbeat must not suppress anything"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_line_says_how_many_cycles_it_stands_for() -> None:
    """A collapsed line has to declare what it collapsed, or the reader silently
    loses the cadence."""
    service = _service(_Client(), sync_log_heartbeat_s=3600)

    with patch("hft_platform.execution.reconciliation.logger") as log:
        await _cycles(service, 1)
        first = [c for c in log.info.call_args_list if c[0][0].startswith("Portfolio Sync Complete")][0]
        assert first[1]["suppressed_cycles"] == 0
        await _cycles(service, 9)
        service.sync_log_heartbeat_s = 0.0
        await service.sync_portfolio()

    completions = [c for c in log.info.call_args_list if c[0][0].startswith("Portfolio Sync Complete")]
    assert completions[-1][1]["suppressed_cycles"] == 9


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_suppressed_counter_resets_after_it_is_reported() -> None:
    service = _service(_Client(), sync_log_heartbeat_s=3600)

    with patch("hft_platform.execution.reconciliation.logger"):
        await _cycles(service, 5)
        service.sync_log_heartbeat_s = 0.0
        await service.sync_portfolio()

    assert service._suppressed_sync_logs == 0


# --------------------------------------------------------------------------- #
# What must never be collapsed                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_sync_still_updates_the_liveness_metric() -> None:
    """Metrics, not log volume, are what prove the loop ran. Collapsing lines
    must not collapse the evidence."""
    service = _service(_Client(), sync_log_heartbeat_s=3600)

    with (
        patch("hft_platform.execution.reconciliation.logger"),
        patch.object(ReconciliationService, "_update_last_success_ts") as touched,
        patch.object(ReconciliationService, "_record_sync_result") as recorded,
    ):
        await _cycles(service, 10)

    assert touched.call_count == 10
    assert recorded.call_count == 10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_discrepancy_is_reported_on_every_cycle_it_persists() -> None:
    """Suppression is for the quiet path only. A drift that repeats is exactly
    the thing the operator needs repeated."""
    client = _Client([_position("TMFH6", 3)])
    service = _service(client, sync_log_heartbeat_s=3600)

    with patch("hft_platform.execution.reconciliation.logger") as log:
        await _cycles(service, 3)

    assert log.warning.call_count >= 3, "drift warnings must not be collapsed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_sync_still_logs_every_time() -> None:
    class _Broken:
        def get_positions(self) -> Any:
            raise RuntimeError("broker unreachable")

    service = _service(_Broken())  # type: ignore[arg-type]

    with patch("hft_platform.execution.reconciliation.logger") as log:
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await service.sync_portfolio()

    assert log.error.call_count == 3
