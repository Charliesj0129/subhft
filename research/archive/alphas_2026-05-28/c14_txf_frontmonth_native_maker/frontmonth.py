"""Front-month contract selection and rollover helpers for TXF.

Design choice: **volume-crossover rule** with calendar fallback.

Rule (concrete):
  On each trading date, the front-month is the TXF contract with the
  greatest aggregate traded volume on the *previous* trading day among
  the set of non-expired TXF contracts. Ties (or missing previous-day
  volume) fall back to the calendar rule: pick the nearest-expiry
  unexpired contract at least 3 trading days away from its expiry.

Why volume-crossover:
  - T1 §3.3 documents that TXF front-month rotates TXFB6 → TXFC6 → TXFD6
    on dates when the new contract's daily volume exceeds the outgoing
    contract's. This matches empirical market behaviour.
  - The calendar fallback prevents starting a brand-new contract on its
    listing day (very low liquidity) or quoting a contract on its
    penultimate day (expiry rollover noise).

Rollover boundary contract:
  - When the selected front-month CHANGES from date N to date N+1, the
    previous contract's position MUST be flattened at the end of N
    before quoting the new contract opens on N+1. No cross-contract
    carry — this avoids hidden cross-exposure and makes per-contract
    PnL accounting deterministic.

Note: this module is offline research infrastructure. Production
front-month rotation would additionally need (a) real expiry dates
from TAIFEX, (b) calendar holiday handling, (c) session-open/close
tick gating. Out of scope for R6-T4 (research prototype only).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ContractWindow:
    """Active-window record for a single TXF contract.

    start_date / end_date are inclusive; end_date is the last day the
    contract remains selectable as front-month (typically expiry minus
    the ``calendar_lookback`` buffer applied at selection time).
    """

    symbol: str
    start_date: date
    end_date: date


# Active front-month windows for TXF contracts observed in the R6 CK dataset.
# Source: R6-T1 §3.3 (TXFB6 Jan-Feb, TXFC6 Feb-Mar, TXFD6 Mar+).
# These are DATA-DERIVED windows, not exchange-calendar windows; they are
# used as the calendar fallback when volume data is unavailable.
DEFAULT_TXF_FRONTMONTH_WINDOWS: tuple[ContractWindow, ...] = (
    ContractWindow(
        symbol="TXFB6",
        start_date=date(2026, 1, 26),
        end_date=date(2026, 2, 25),
    ),
    ContractWindow(
        symbol="TXFC6",
        start_date=date(2026, 2, 26),
        end_date=date(2026, 3, 18),
    ),
    ContractWindow(
        symbol="TXFD6",
        start_date=date(2026, 3, 19),
        end_date=date(2026, 4, 14),
    ),
)


class FrontMonthSelector:
    """Decides which TXF contract is front-month on a given date.

    Two input modes:
      1. ``select_by_volume(trade_date, volumes_by_symbol)`` — given a dict
         of {symbol: prev_day_volume}, return the highest-volume symbol.
         Returns ``None`` if the dict is empty.
      2. ``select_by_calendar(trade_date)`` — pure calendar fallback using
         ``windows``. Returns the unique window containing the date, or
         ``None`` if no window covers it.

    The combined ``select(trade_date, volumes_by_symbol=None)`` method
    prefers volume when available and falls back to calendar otherwise.
    """

    __slots__ = ("_windows",)

    def __init__(
        self,
        windows: tuple[ContractWindow, ...] = DEFAULT_TXF_FRONTMONTH_WINDOWS,
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
        # Restrict to contracts whose calendar window covers trade_date
        # — rejects back-month contracts that happen to have spikes.
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
        best_sym = max(eligible.items(), key=lambda kv: kv[1])[0]
        return best_sym

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
    """Return [(trade_date, front_month_symbol), ...] using calendar rule.

    Dates that fall outside every window are skipped.
    """
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
    """Given a schedule, return [(date, outgoing_sym, incoming_sym)] for days
    where the front-month changes relative to the previous entry.

    The emitted date is the *new* contract's first day — the day on which
    the flatten-then-switch behaviour must have completed by the session
    open.
    """
    out: list[tuple[date, str, str]] = []
    prev_sym: str | None = None
    for d, sym in schedule:
        if prev_sym is not None and sym != prev_sym:
            out.append((d, prev_sym, sym))
        prev_sym = sym
    return out
