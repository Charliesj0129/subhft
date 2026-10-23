from __future__ import annotations

import numpy as np
import pytest

from research.data_pipeline import asof

S = asof.NS_PER_SECOND


def _arr(*items: int) -> np.ndarray:
    return np.array(items, dtype=np.int64)


def test_a_fresh_quote_is_returned() -> None:
    index = asof.asof_index(_arr(10 * S, 20 * S), _arr(21 * S))

    assert index.tolist() == [1]


def test_a_stale_quote_is_refused_instead_of_carried_forward() -> None:
    index = asof.asof_index(_arr(10 * S), _arr(10 * S + 3 * S))

    assert index.tolist() == [asof.NO_QUOTE]


def test_a_quote_exactly_at_the_limit_is_still_accepted() -> None:
    index = asof.asof_index(_arr(10 * S), _arr(12 * S))

    assert index.tolist() == [0]


def test_a_query_before_the_first_quote_has_none() -> None:
    index = asof.asof_index(_arr(10 * S), _arr(9 * S))

    assert index.tolist() == [asof.NO_QUOTE]


def test_a_quote_at_the_query_time_counts_as_available() -> None:
    index = asof.asof_index(_arr(10 * S), _arr(10 * S))

    assert index.tolist() == [0]


def test_a_longer_limit_admits_the_older_quote() -> None:
    index = asof.asof_index(_arr(10 * S), _arr(15 * S), max_age_ns=10 * S)

    assert index.tolist() == [0]


def test_an_empty_quote_stream_has_no_quote_for_any_query() -> None:
    index = asof.asof_index(_arr(), _arr(1 * S, 2 * S))

    assert index.tolist() == [asof.NO_QUOTE, asof.NO_QUOTE]


def test_values_are_nan_where_the_quote_is_missing_or_stale() -> None:
    times = _arr(10 * S, 20 * S)
    values = np.array([100.0, 200.0])

    out = asof.asof_values(times, values, _arr(9 * S, 11 * S, 15 * S, 21 * S))

    assert np.isnan(out[0])
    assert out[1] == 100.0
    assert np.isnan(out[2])
    assert out[3] == 200.0


def test_a_negative_limit_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        asof.asof_index(_arr(1), _arr(2), max_age_ns=-1)
