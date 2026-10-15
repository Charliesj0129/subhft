"""Does the option chain the engine is subscribed to reach the money?

The research audit (``option_chain_atm_coverage``, PR #554) found that the recorded TXO chain stopped below the futures
price on most days, so skew, term structure and put-call-parity work cannot be done on it. That check reads what was
recorded, a day later. This reads what the engine is subscribed to, so the same condition is visible when the
subscription is made.

"Covered" is the research definition: the expiry's listed strikes reach both ``F - 500`` and ``F + 500`` (points).
A chain that stops below ``F`` holds only deep in-the-money calls and deep out-of-the-money puts, and no implied
volatility can be read off it. The function is pure; the caller supplies the subscribed codes, the contract rows and
the futures price.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from hft_platform.config._symbols_types import parse_date_key

ATM_HALF_WIDTH_POINTS = 500.0

# Gauge values. -1 is not a third state of the chain, it is "could not be judged": no underlying price.
COVERED = 1
NOT_COVERED = 0
UNKNOWN = -1

_OPTION_KINDS = frozenset({"option", "opt", "options"})


@dataclass(frozen=True, slots=True)
class ExpiryCoverage:
    """One expiry of the subscribed chain."""

    expiry: str  # YYYY-MM-DD
    contracts: int
    min_strike: float
    max_strike: float
    reference: float | None
    covered: bool | None  # None: no underlying price, so the chain cannot be judged

    @property
    def gauge_value(self) -> int:
        if self.covered is None:
            return UNKNOWN
        return COVERED if self.covered else NOT_COVERED


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _iso_expiry(row: Mapping[str, Any]) -> str | None:
    for key in ("delivery_date", "expiry", "due_date", "maturity_date"):
        parsed = parse_date_key(row.get(key))
        if parsed:
            text = str(parsed)
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return None


def chain_coverage(
    subscribed_codes: Iterable[str],
    contracts_by_code: Mapping[str, Mapping[str, Any]],
    reference: float | None,
    *,
    root: str = "TXO",
    half_width: float = ATM_HALF_WIDTH_POINTS,
) -> list[ExpiryCoverage]:
    """Per-expiry coverage of the subscribed ``root`` options, nearest expiry first.

    Codes the contract cache does not know, rows that are not options of ``root``, and rows without a positive strike
    or a parseable expiry are left out: they say nothing about where the chain sits. ``reference`` is the underlying
    future's price in the same units as the strikes; without one every expiry is ``covered=None``.
    """
    ref = _positive_float(reference)
    span: dict[str, list[float]] = {}  # expiry -> [min_strike, max_strike, contracts]
    for code in dict.fromkeys(subscribed_codes):  # a code listed twice is one contract
        row = contracts_by_code.get(code)
        if row is None:
            continue
        kind = str(row.get("type") or row.get("security_type") or "").lower()
        if kind not in _OPTION_KINDS or str(row.get("root") or "").upper() != root.upper():
            continue
        strike = _positive_float(row.get("strike"))
        expiry = _iso_expiry(row)
        if strike is None or expiry is None:
            continue
        entry = span.get(expiry)
        if entry is None:
            span[expiry] = [strike, strike, 1]
        else:
            entry[0] = min(entry[0], strike)
            entry[1] = max(entry[1], strike)
            entry[2] += 1

    result: list[ExpiryCoverage] = []
    for expiry in sorted(span):
        low, high, count = span[expiry]
        covered = None if ref is None else (low <= ref - half_width and high >= ref + half_width)
        result.append(ExpiryCoverage(expiry, int(count), low, high, ref, covered))
    return result
