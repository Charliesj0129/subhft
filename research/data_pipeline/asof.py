"""As-of lookups that refuse stale quotes.

A plain as-of join returns the last row at or before the query time however old it is. On a
quiet contract, or across a feed gap, that row is minutes old and silently becomes "the price now"
(the round-1 backtests trusted such quotes). Every lookup here carries a ``max_age_ns`` and
reports "no quote" instead of an old one.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

NS_PER_SECOND = 1_000_000_000
DEFAULT_MAX_AGE_NS = 2 * NS_PER_SECOND
NO_QUOTE = -1


def asof_index(
    times: NDArray[np.int64],
    queries: NDArray[np.int64],
    *,
    max_age_ns: int = DEFAULT_MAX_AGE_NS,
) -> NDArray[np.int64]:
    """Index of the last ``times`` entry at or before each query, or ``NO_QUOTE`` when it is too old.

    ``times`` must be sorted ascending (not checked: callers hold sorted event streams). A quote
    exactly ``max_age_ns`` old is still accepted; a query before the first quote has none.
    """
    if max_age_ns < 0:
        raise ValueError("max_age_ns must be non-negative")
    times = np.asarray(times, dtype=np.int64)
    queries = np.asarray(queries, dtype=np.int64)
    positions = np.searchsorted(times, queries, side="right") - 1
    if times.size == 0:
        return np.full(queries.shape, NO_QUOTE, dtype=np.int64)
    found = positions >= 0
    age = queries - times[np.where(found, positions, 0)]
    fresh = found & (age <= max_age_ns)
    return np.where(fresh, positions, NO_QUOTE).astype(np.int64)


def asof_values(
    times: NDArray[np.int64],
    values: NDArray[np.float64],
    queries: NDArray[np.int64],
    *,
    max_age_ns: int = DEFAULT_MAX_AGE_NS,
) -> NDArray[np.float64]:
    """``values`` at the as-of row of each query, ``NaN`` where the quote is missing or stale."""
    index = asof_index(times, queries, max_age_ns=max_age_ns)
    values = np.asarray(values, dtype=np.float64)
    out = np.full(index.shape, np.nan, dtype=np.float64)
    ok = index != NO_QUOTE
    out[ok] = values[index[ok]]
    return out
