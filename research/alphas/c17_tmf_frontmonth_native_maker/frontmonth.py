"""Front-month contract selection and rollover helpers for TMF (微台期).

Structurally identical to ``research.alphas.c14_txf_frontmonth_native_maker.frontmonth``
but for the TMF contract chain (1 pt = 10 NTD, vs TXF's 200 NTD/pt).

Rule: **volume-crossover** with calendar fallback, same as C14.

TMF rotation observed in the 58-day CK window:
  TMFB6: Jan-Feb front-month
  TMFC6: Feb 25 - Mar 18 front-month
  TMFD6: Mar 19 - Apr 14 front-month (same expiry code as TXFD6)

This module does NOT import C14's frontmonth to avoid cross-candidate
coupling — research artifacts are self-contained. The selector semantics
are identical; only the per-contract windows differ.

See also:
  - `research/alphas/c14_txf_frontmonth_native_maker/frontmonth.py` for the
    TXF precedent and full rationale (volume-crossover vs calendar).
  - R10-T1 §3 for per-contract window empirical dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ContractWindow:
    """Active-window record for a single TMF contract (start/end inclusive)."""

    symbol: str
    start_date: date
    end_date: date


# Active front-month windows for TMF contracts in the R10 CK dataset.
# Source: R10-T1 §3 (TMFB6 Jan-Feb, TMFC6 Feb-Mar, TMFD6 Mar+).
DEFAULT_TMF_FRONTMONTH_WINDOWS: tuple[ContractWindow, ...] = (
    ContractWindow(
        symbol="TMFB6",
        start_date=date(2026, 1, 26),
        end_date=date(2026, 2, 25),
    ),
    ContractWindow(
        symbol="TMFC6",
        start_date=date(2026, 2, 26),
        end_date=date(2026, 3, 18),
    ),
    ContractWindow(
        symbol="TMFD6",
        start_date=date(2026, 3, 19),
        end_date=date(2026, 4, 14),
    ),
)


class FrontMonthSelector:
    """Decides which TMF contract is front-month on a given date.

    Same semantics as C14's selector:
      - ``select_by_calendar(trade_date)`` — which window contains the date
      - ``select_by_volume(trade_date, volumes_by_symbol)`` — highest-volume
        eligible (calendar-gated) contract
      - ``select(trade_date, volumes_by_symbol=None)`` — volume-preferred,
        calendar fallback
    """

    __slots__ = ("_windows",)

    def __init__(
        self,
        windows: tuple[ContractWindow, ...] = DEFAULT_TMF_FRONTMONTH_WINDOWS,
    ) -> None:
        self._windows = windows

    @property
    def windows(self) -> tuple[ContractWindow, ...]:
        return self._windows

    def select_by_calendar(self, trade_date: date) -> str | None:
        for w in self._windows:
            if w.start_date <= trade_date <= w.end_date:
                return w.symbol
        return None

    def select_by_volume(
        self,
        trade_date: date,
        volumes_by_symbol: dict[str, int],
    ) -> str | None:
        if not volumes_by_symbol:
            return None
        eligible = {
            sym: vol
            for sym, vol in volumes_by_symbol.items()
            if any(
                w.symbol == sym and w.start_date <= trade_date <= w.end_date
                for w in self._windows
            )
        }
        if not eligible:
            return None
        return max(eligible.items(), key=lambda kv: kv[1])[0]

    def select(
        self,
        trade_date: date,
        volumes_by_symbol: dict[str, int] | None = None,
    ) -> str | None:
        if volumes_by_symbol:
            sym = self.select_by_volume(trade_date, volumes_by_symbol)
            if sym is not None:
                return sym
        return self.select_by_calendar(trade_date)


def iter_front_month_schedule(
    dates: list[date],
    selector: FrontMonthSelector | None = None,
) -> list[tuple[date, str]]:
    """Return [(trade_date, front_month_symbol), ...] using calendar rule."""
    sel = selector or FrontMonthSelector()
    out: list[tuple[date, str]] = []
    for d in dates:
        sym = sel.select_by_calendar(d)
        if sym is not None:
            out.append((d, sym))
    return out


def detect_rollover_days(
    schedule: list[tuple[date, str]],
) -> list[tuple[date, str, str]]:
    """Emit [(date, outgoing_sym, incoming_sym)] for rollover days."""
    out: list[tuple[date, str, str]] = []
    prev_sym: str | None = None
    for d, sym in schedule:
        if prev_sym is not None and sym != prev_sym:
            out.append((d, prev_sym, sym))
        prev_sym = sym
    return out
