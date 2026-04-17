"""Symbol classification helpers shared across execution paths.

Centralizes the heuristics that were previously duplicated in
``execution/reconciliation.py`` and ``execution/startup_recon.py`` so that
the definition of "futures symbol" cannot drift between modules.
"""
from __future__ import annotations

# TAIFEX futures/options symbol prefixes.
# Examples:
#   TMFE6, TMFD6, TMFR1 → TM (Mini-TAIEX futures)
#   TXFE6, TXFD6        → TX (TAIEX futures)
#   MXFE6, MXFD6        → MX (Mini-MSCI/small futures)
#   TEFE6               → TE (Electronic sector)
#   TFFE6               → TF (Finance sector)
#   "FD"/"FX" appear in some legacy near/far-month codes
#
# Note: "TM" was added to cover TMFE6 / TMFR1 which the original
# heuristic (shared between reconciliation and startup_recon) missed;
# without this, live Mini-TAIEX (TM*) contracts were treated as stocks
# for drift thresholds and auto-correct sizing.
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
