"""Regression: the symbols builder must measure expiry on the exchange's clock.

``contract_dte_days`` used ``datetime.now(UTC).date()`` while the rest of the
platform runs on ``TZ=Asia/Taipei`` (``Dockerfile``) — including the
stale-instrument connect gate, which refuses to start when any subscribed
contract has ``delivery_date < today``.

Those two disagree between Taipei midnight and 08:00, and that window is
exactly the documented remedy window for a contract roll: an operator runs
``make rebuild-symbols-yaml`` after the night-session close (05:00 Taipei) and
before the day pre-open. Run there, the builder kept contracts that had already
expired the previous session, the rebuilt ``symbols.yaml`` looked healthy, and
the next boot was refused by the gate with no market data at all.

The tests below freeze the clock inside that window.
"""

from __future__ import annotations

import datetime as dt

import pytest

from hft_platform.config import _symbols_types as symbols_types
from hft_platform.config._symbols_types import (
    ContractIndex,
    contract_dte_days,
    exchange_today,
)
from hft_platform.config.symbols import build_symbols

# Taipei 2026-08-20 06:00 — after the night close, before the day pre-open.
# UTC is still 2026-08-19 here, which is what made the two clocks disagree.
ROLL_WINDOW_UTC = dt.datetime(2026, 8, 19, 22, 0, tzinfo=dt.UTC)
EXPIRED_DELIVERY_DATE = "2026/08/19"


@pytest.fixture
def frozen_roll_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the builder's clock inside the post-roll remedy window."""

    class _FrozenDatetime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:  # type: ignore[override]
            if tz is None:
                return ROLL_WINDOW_UTC.replace(tzinfo=None)
            return ROLL_WINDOW_UTC.astimezone(tz)

    monkeypatch.setattr(symbols_types, "datetime", _FrozenDatetime)


def test_exchange_today_is_the_taipei_date_not_the_utc_date(frozen_roll_window: None) -> None:
    assert exchange_today() == dt.date(2026, 8, 20)
    assert ROLL_WINDOW_UTC.date() == dt.date(2026, 8, 19)


def test_yesterdays_expiry_is_negative_dte_inside_the_roll_window(frozen_roll_window: None) -> None:
    """A contract that expired at the previous session's close is expired.

    Under the old UTC clock this returned 0 — same-day, therefore kept.
    """
    dte = contract_dte_days({"delivery_date": EXPIRED_DELIVERY_DATE})
    assert dte is not None
    assert dte < 0, "contract that settled on the previous exchange day must read as expired"


def test_todays_expiry_is_still_same_day_inside_the_roll_window(frozen_roll_window: None) -> None:
    """The gate permits same-day expiry, so the builder must not drop it early."""
    assert contract_dte_days({"delivery_date": "2026/08/20"}) == 0


def _future(code: str, delivery: str) -> dict[str, object]:
    return {
        "code": code,
        "exchange": "TAIFEX",
        "type": "future",
        "root": "TXF",
        "delivery_date": delivery,
        "tick_size": 1.0,
        "price_scale": 10000,
    }


def test_rebuild_in_the_roll_window_drops_the_settled_front_month(frozen_roll_window: None, tmp_path) -> None:
    """End-to-end: ``TXF@front`` must roll to September, not re-emit the dead H6.

    This is the operator's actual remedy — regenerating symbols.yaml after the
    roll. Before the fix it emitted ``TXFH6``, which the connect gate then
    refused, taking the whole feed down.
    """
    list_path = tmp_path / "symbols.list"
    list_path.write_text(
        "TXF@front exchange=FUT tags=futures|front_month|txf\n",
        encoding="utf-8",
    )
    index = ContractIndex(
        contracts=[
            _future("TXFH6", EXPIRED_DELIVERY_DATE),
            _future("TXFI6", "2026/09/16"),
        ]
    )

    result = build_symbols(str(list_path), index)

    codes = {str(entry.get("code")) for entry in result.symbols}
    assert "TXFH6" not in codes, "settled contract must not reach symbols.yaml"
    assert codes == {"TXFI6"}


def test_preview_summary_counts_the_validation_errors_it_prints() -> None:
    """``errors=0`` must not head a preview that then prints a fatal error.

    The subscription-limit failure comes from ``validate_symbols``, not from
    the build itself, so the summary line used to report ``errors=0`` directly
    above an ``Errors:`` section that aborts the rebuild.
    """
    from hft_platform.config._symbols_contracts import preview_lines
    from hft_platform.config._symbols_types import SymbolBuildResult

    build = SymbolBuildResult(symbols=[{"code": "TXFI6", "exchange": "TAIFEX"}])
    validation = SymbolBuildResult(errors=["Symbol count exceeds subscription limit: 482 > 480"])

    summary = [line for line in preview_lines(build, validation=validation) if line.startswith("errors=")]

    assert summary == ["errors=1 warnings=0"]
