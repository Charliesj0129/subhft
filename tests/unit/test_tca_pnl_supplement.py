"""Tests for TCA + PnL supplement template and dispatcher method."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# Template tests
# ---------------------------------------------------------------------------


def test_render_tca_pnl_supplement_contains_both_sections() -> None:
    from hft_platform.notifications.templates import render_tca_pnl_supplement

    result = render_tca_pnl_supplement(
        tca_section="TCA: avg slippage 1.2 bps",
        pnl_section="PnL: +1,234 NTD",
    )

    assert "TCA: avg slippage 1.2 bps" in result
    assert "PnL: +1,234 NTD" in result


def test_render_tca_pnl_supplement_has_header() -> None:
    from hft_platform.notifications.templates import render_tca_pnl_supplement

    result = render_tca_pnl_supplement(tca_section="tca", pnl_section="pnl")

    assert "TCA & PnL Supplement" in result


def test_render_tca_pnl_supplement_sections_separated() -> None:
    from hft_platform.notifications.templates import render_tca_pnl_supplement

    result = render_tca_pnl_supplement(tca_section="SECTION_A", pnl_section="SECTION_B")

    # Sections must both appear and be separated by blank line
    a_pos = result.index("SECTION_A")
    b_pos = result.index("SECTION_B")
    between = result[a_pos + len("SECTION_A") : b_pos]
    assert "\n" in between


def test_render_tca_pnl_supplement_empty_sections() -> None:
    from hft_platform.notifications.templates import render_tca_pnl_supplement

    result = render_tca_pnl_supplement(tca_section="", pnl_section="")

    assert isinstance(result, str)
    assert "TCA & PnL Supplement" in result


# ---------------------------------------------------------------------------
# Dispatcher tests
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sender() -> AsyncMock:
    sender = AsyncMock()
    sender.send = AsyncMock(return_value=True)
    sender.enabled = True
    return sender


@pytest.fixture
def dispatcher(mock_sender: AsyncMock):
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    return NotificationDispatcher(sender=mock_sender)


@pytest.mark.asyncio
async def test_notify_tca_pnl_supplement_calls_sender(dispatcher, mock_sender) -> None:
    await dispatcher.notify_tca_pnl_supplement(
        tca_section="TCA data",
        pnl_section="PnL data",
    )

    mock_sender.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_notify_tca_pnl_supplement_uses_critical_false(dispatcher, mock_sender) -> None:
    await dispatcher.notify_tca_pnl_supplement(
        tca_section="TCA data",
        pnl_section="PnL data",
    )

    call_kwargs = mock_sender.send.call_args
    assert call_kwargs.kwargs["critical"] is False


@pytest.mark.asyncio
async def test_notify_tca_pnl_supplement_message_contains_sections(dispatcher, mock_sender) -> None:
    await dispatcher.notify_tca_pnl_supplement(
        tca_section="avg fill cost 2.1 bps",
        pnl_section="gross +5,000 NTD net +4,200 NTD",
    )

    message = mock_sender.send.call_args.args[0]
    assert "avg fill cost 2.1 bps" in message
    assert "gross +5,000 NTD net +4,200 NTD" in message
