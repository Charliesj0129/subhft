"""Symbol classification helpers shared across execution paths.

Centralizes the heuristics that were previously duplicated in
``execution/reconciliation.py`` and ``execution/startup_recon.py`` so that
the definition of "futures symbol" cannot drift between modules.
"""

from __future__ import annotations

_FUTURES_PREFIXES: tuple[str, ...] = ("FD", "FX", "TX", "MX", "TE", "TF", "TM")


def is_futures_symbol(symbol: str) -> bool:
    """Return True if *symbol* looks like a TAIFEX futures/options contract.

    Heuristic only: matches when any of the known prefixes appears
    anywhere in the uppercased symbol. Non-futures symbols (e.g. 4-digit
    TWSE stock codes like "2330") return False.
    """
    if not symbol:
        return False
    upper = symbol.upper()
    return any(prefix in upper for prefix in _FUTURES_PREFIXES)
