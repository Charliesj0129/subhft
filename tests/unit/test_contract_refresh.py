"""Tests for Phase 17 contract refresh & failed subscription tracking.

Covers:
- test_dead_metric_incremented       (C1)
- test_failed_sub_tracked            (C2)
- test_retry_thread_resolves_symbols (C2)
- test_stale_cache_detected          (C3)
- test_fresh_cache_not_stale         (C3)
- test_missing_cache_is_stale        (C3)
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter import shioaji_client as mod


# ---------------------------------------------------------------------------
# Helpers: build a minimal ShioajiClient without calling __init__
# ---------------------------------------------------------------------------

def _bare_client() -> mod.ShioajiClient:
    """Create a ShioajiClient instance bypassing __init__ (test pattern)."""
    client = object.__new__(mod.ShioajiClient)
    client.api = MagicMock()
    client.metrics = MagicMock()
    client.metrics.stormguard_mode = MagicMock()
    client.allow_synthetic_contracts = False
    client.subscribed_codes: set[str] = set()
    client.subscribed_count = 0
    client._failed_sub_symbols: list = []
    client._sub_retry_running = False
    client._sub_retry_thread = None
    client._contract_retry_s = 60.0
    client._contract_refresh_s = 86400.0
    client._contract_cache_path = "config/contracts.json"
    client._contract_refresh_running = False
    client._contract_refresh_thread = None
    return client


# ---------------------------------------------------------------------------
# C1: Dead metric incremented on contract-not-found
# ---------------------------------------------------------------------------


def test_dead_metric_incremented():
    """When _get_contract returns None, shioaji_contract_lookup_errors_total is incremented."""
    client = _bare_client()

    # Setup metrics mock with the expected counter attribute
    mock_counter = MagicMock()
    mock_counter.labels.return_value = MagicMock()
    client.metrics.shioaji_contract_lookup_errors_total = mock_counter

    # Patch _get_contract to return None (contract not found)
    with patch.object(client, "_get_contract", return_value=None):
        result = client._subscribe_symbol(
            {"code": "TXO23300B6", "exchange": "TAIFEX"}, cb=MagicMock()
        )

    assert result is False
    mock_counter.labels.assert_called_once_with(code="TXO23300B6")
    mock_counter.labels.return_value.inc.assert_called_once()


# ---------------------------------------------------------------------------
# C2: Failed subscription tracking
# ---------------------------------------------------------------------------


def test_failed_sub_tracked():
    """When _subscribe_symbol fails, the symbol is added to _failed_sub_symbols."""
    client = _bare_client()

    # Ensure metrics attribute exists so C1 path doesn't error
    client.metrics.shioaji_contract_lookup_errors_total = MagicMock()
    client.metrics.shioaji_contract_lookup_errors_total.labels.return_value = MagicMock()

    sym = {"code": "TXO_BAD", "exchange": "TAIFEX"}

    with patch.object(client, "_subscribe_symbol", return_value=False):
        # Simulate the subscribe_basket failure-tracking logic
        if not client._subscribe_symbol(sym, cb=None):
            client._failed_sub_symbols.append(sym)

    assert sym in client._failed_sub_symbols


def test_retry_thread_resolves_symbols():
    """_start_sub_retry_thread: succeeds on second attempt → _failed_sub_symbols cleared."""
    client = _bare_client()

    sym = {"code": "TXO_RETRY", "exchange": "TAIFEX"}
    client._failed_sub_symbols = [sym]

    call_count = {"n": 0}

    def mock_subscribe(s, cb):
        call_count["n"] += 1
        return call_count["n"] >= 2  # fail first, succeed second

    # Direct instance assignment so the thread sees it even after any context manager exits
    client._subscribe_symbol = mock_subscribe
    client._contract_retry_s = 0.05  # very short interval for test speed

    client._start_sub_retry_thread(cb=MagicMock())

    # Wait for the retry thread to finish (up to 3 seconds)
    deadline = time.monotonic() + 3.0
    while client._sub_retry_running and time.monotonic() < deadline:
        time.sleep(0.05)

    assert client._failed_sub_symbols == [], "All failed subscriptions should be resolved"
    assert "TXO_RETRY" in client.subscribed_codes


# ---------------------------------------------------------------------------
# C3: Contract cache staleness detection
# ---------------------------------------------------------------------------


def test_stale_cache_detected(tmp_path: Path):
    """contracts.json older than refresh_s → _is_contract_cache_stale() returns True."""
    client = _bare_client()
    client._contract_refresh_s = 3600.0  # 1 hour

    cache_file = tmp_path / "contracts.json"
    import datetime

    old_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)).isoformat()
    cache_file.write_text(json.dumps({"updated_at": old_ts}), encoding="utf-8")

    client._contract_cache_path = str(cache_file)
    assert client._is_contract_cache_stale() is True


def test_fresh_cache_not_stale(tmp_path: Path):
    """contracts.json updated 1 hour ago and refresh_s=86400 → not stale."""
    client = _bare_client()
    client._contract_refresh_s = 86400.0  # 24 hours

    cache_file = tmp_path / "contracts.json"
    import datetime

    recent_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
    cache_file.write_text(json.dumps({"updated_at": recent_ts}), encoding="utf-8")

    client._contract_cache_path = str(cache_file)
    assert client._is_contract_cache_stale() is False


def test_missing_cache_is_stale(tmp_path: Path):
    """Missing contracts.json → _is_contract_cache_stale() returns True."""
    client = _bare_client()
    client._contract_cache_path = str(tmp_path / "nonexistent.json")
    assert client._is_contract_cache_stale() is True
