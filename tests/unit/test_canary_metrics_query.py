"""Tests for CanaryMetricsQuery — CK-backed canary performance metrics.

TDD: tests written before implementation.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.alpha.canary_metrics import CanaryMetricsQuery

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(
    slippage_rows: list | None = None,
    drawdown_rows: list | None = None,
    error_rows: list | None = None,
    sessions_rows: list | None = None,
) -> MagicMock:
    """Return a mock CK client whose query() returns preset result_rows."""
    client = MagicMock()
    responses = [
        _mock_result(slippage_rows if slippage_rows is not None else [(2.5,)]),
        _mock_result(drawdown_rows if drawdown_rows is not None else [(100,), (90,), (80,)]),
        _mock_result(error_rows if error_rows is not None else [(0.05,)]),
        _mock_result(sessions_rows if sessions_rows is not None else [(3,)]),
    ]
    client.query.side_effect = responses
    return client


def _mock_result(rows: list) -> MagicMock:
    result = MagicMock()
    result.result_rows = rows
    return result


# ---------------------------------------------------------------------------
# Test 1: Normal response → dict with 4 keys and correct types
# ---------------------------------------------------------------------------


def test_fetch_returns_all_four_metrics() -> None:
    client = _make_client()
    factory = MagicMock(return_value=client)

    q = CanaryMetricsQuery(client_factory=factory)
    result = q.fetch(alpha_id="alpha_001", strategy_id="strat_01", since_ns=0)

    assert result is not None
    assert set(result.keys()) == {"slippage_bps", "drawdown", "error_rate", "sessions"}
    assert isinstance(result["slippage_bps"], float)
    assert isinstance(result["drawdown"], float)
    assert isinstance(result["error_rate"], float)
    assert isinstance(result["sessions"], int)
    assert result["slippage_bps"] == pytest.approx(2.5)
    assert result["sessions"] == 3


# ---------------------------------------------------------------------------
# Test 2: client_factory raises → fetch returns None
# ---------------------------------------------------------------------------


def test_fetch_returns_none_on_ck_error() -> None:
    factory = MagicMock(side_effect=RuntimeError("cannot connect"))

    q = CanaryMetricsQuery(client_factory=factory)
    result = q.fetch(alpha_id="alpha_001", strategy_id="strat_01", since_ns=0)

    assert result is None


# ---------------------------------------------------------------------------
# Test 3: client.query raises → fetch returns None
# ---------------------------------------------------------------------------


def test_fetch_returns_none_on_query_error() -> None:
    client = MagicMock()
    client.query.side_effect = Exception("CK query timeout")
    factory = MagicMock(return_value=client)

    q = CanaryMetricsQuery(client_factory=factory)
    result = q.fetch(alpha_id="alpha_001", strategy_id="strat_01", since_ns=0)

    assert result is None


# ---------------------------------------------------------------------------
# Test 4: SQL contains strategy_id string
# ---------------------------------------------------------------------------


def test_slippage_query_filters_by_strategy() -> None:
    client = _make_client()
    factory = MagicMock(return_value=client)

    q = CanaryMetricsQuery(client_factory=factory)
    q.fetch(alpha_id="alpha_001", strategy_id="my_strategy_42", since_ns=0)

    all_parameters = [call.kwargs.get("parameters", {}) for call in client.query.call_args_list]
    assert any(params.get("strategy_id") == "my_strategy_42" for params in all_parameters)


# ---------------------------------------------------------------------------
# Test 5: since_ns filter applied to SQL
# ---------------------------------------------------------------------------


def test_since_ns_filter_applied() -> None:
    client = _make_client()
    factory = MagicMock(return_value=client)
    since_ns = 1700000000000000000

    q = CanaryMetricsQuery(client_factory=factory)
    q.fetch(alpha_id="alpha_001", strategy_id="strat_01", since_ns=since_ns)

    all_parameters = [call.kwargs.get("parameters", {}) for call in client.query.call_args_list]
    assert any(params.get("since_ns") == since_ns for params in all_parameters)


# ---------------------------------------------------------------------------
# Test 6: zero total orders → error_rate = 0.0 (no ZeroDivisionError)
# ---------------------------------------------------------------------------


def test_error_rate_division_by_zero() -> None:
    client = _make_client(error_rows=[(0,)])
    factory = MagicMock(return_value=client)

    q = CanaryMetricsQuery(client_factory=factory)
    result = q.fetch(alpha_id="alpha_001", strategy_id="strat_01", since_ns=0)

    assert result is not None
    assert result["error_rate"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Test 7: empty fills for drawdown → drawdown = 0.0
# ---------------------------------------------------------------------------


def test_drawdown_with_no_fills() -> None:
    client = _make_client(drawdown_rows=[])
    factory = MagicMock(return_value=client)

    q = CanaryMetricsQuery(client_factory=factory)
    result = q.fetch(alpha_id="alpha_001", strategy_id="strat_01", since_ns=0)

    assert result is not None
    assert result["drawdown"] == pytest.approx(0.0)
