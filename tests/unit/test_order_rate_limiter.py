"""Unit tests for hft_platform.order.rate_limiter (backed by core.rate_limiter).

Covers RateLimiter and PerSymbolRateLimiter including:
- under/at/over soft and hard caps
- window expiry
- update() partial updates
- unknown symbol (OK shortcut)
- cardinality bound
- _evict_idle triggering
- env-var defaults
"""
import os
from collections import deque
from unittest.mock import patch

from hft_platform.core.rate_limiter import (
    PerSymbolRateLimiter,
    PerSymbolRateResult,
    RateLimiter,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# The classes live in core.rate_limiter; patch timebase there.
_TIMEBASE_NOW_S = "hft_platform.core.rate_limiter.timebase.now_s"


def _mock_now(value: float):
    """Return a context-manager that patches timebase.now_s to a constant."""
    return patch(_TIMEBASE_NOW_S, return_value=value)


# ─────────────────────────────────────────────────────────────────────────────
# RateLimiter
# ─────────────────────────────────────────────────────────────────────────────

class TestRateLimiter:
    def test_check_returns_true_below_soft_cap(self):
        rl = RateLimiter(soft_cap=5, hard_cap=10, window_s=60)
        with _mock_now(1000.0):
            assert rl.check() is True

    def test_check_returns_true_at_soft_cap(self):
        rl = RateLimiter(soft_cap=3, hard_cap=10, window_s=60)
        # Insert 3 events (== soft_cap)
        rl.rate_window.extend([999.0, 999.0, 999.0])
        with _mock_now(1000.0):
            result = rl.check()
        assert result is True

    def test_check_returns_false_at_hard_cap(self):
        rl = RateLimiter(soft_cap=3, hard_cap=5, window_s=60)
        rl.rate_window.extend([999.0] * 5)  # == hard_cap
        with _mock_now(1000.0):
            result = rl.check()
        assert result is False

    def test_check_returns_false_above_hard_cap(self):
        rl = RateLimiter(soft_cap=3, hard_cap=5, window_s=60)
        rl.rate_window.extend([999.0] * 7)  # > hard_cap
        with _mock_now(1000.0):
            result = rl.check()
        assert result is False

    def test_check_expires_old_entries_within_window(self):
        rl = RateLimiter(soft_cap=3, hard_cap=5, window_s=60)
        # Entries from 120 s ago — all outside the 60 s window
        rl.rate_window.extend([800.0, 800.0, 800.0, 800.0, 800.0])
        with _mock_now(1000.0):
            result = rl.check()
        assert result is True
        assert len(rl.rate_window) == 0

    def test_check_keeps_entries_inside_window(self):
        rl = RateLimiter(soft_cap=10, hard_cap=20, window_s=60)
        rl.rate_window.extend([980.0, 990.0])  # within 60 s of t=1000
        with _mock_now(1000.0):
            result = rl.check()
        assert result is True
        assert len(rl.rate_window) == 2

    def test_record_appends_timestamp(self):
        rl = RateLimiter(soft_cap=5, hard_cap=10, window_s=60)
        with _mock_now(1234.5):
            rl.record()
        assert len(rl.rate_window) == 1
        assert rl.rate_window[0] == 1234.5

    def test_update_soft_cap_only(self):
        rl = RateLimiter(soft_cap=5, hard_cap=10, window_s=60)
        rl.update(soft_cap=7)
        assert rl.soft_cap == 7
        assert rl.hard_cap == 10
        assert rl.window_s == 60

    def test_update_hard_cap_only(self):
        rl = RateLimiter(soft_cap=5, hard_cap=10, window_s=60)
        rl.update(hard_cap=20)
        assert rl.hard_cap == 20
        assert rl.soft_cap == 5

    def test_update_window_s_only(self):
        rl = RateLimiter(soft_cap=5, hard_cap=10, window_s=60)
        rl.update(window_s=120)
        assert rl.window_s == 120
        assert rl.soft_cap == 5

    def test_update_all_params(self):
        rl = RateLimiter(soft_cap=5, hard_cap=10, window_s=60)
        rl.update(soft_cap=8, hard_cap=15, window_s=30)
        assert rl.soft_cap == 8
        assert rl.hard_cap == 15
        assert rl.window_s == 30

    def test_update_none_values_leave_params_unchanged(self):
        rl = RateLimiter(soft_cap=5, hard_cap=10, window_s=60)
        rl.update()
        assert rl.soft_cap == 5
        assert rl.hard_cap == 10
        assert rl.window_s == 60


# ─────────────────────────────────────────────────────────────────────────────
# PerSymbolRateLimiter
# ─────────────────────────────────────────────────────────────────────────────

class TestPerSymbolRateLimiter:
    def test_check_unknown_symbol_returns_ok(self):
        prl = PerSymbolRateLimiter(soft_limit=10, hard_limit=20, window_s=60.0)
        with _mock_now(1000.0):
            result = prl.check("2330")
        assert result is PerSymbolRateResult.OK

    def test_check_below_soft_limit_returns_ok(self):
        prl = PerSymbolRateLimiter(soft_limit=10, hard_limit=20, window_s=60.0)
        prl._windows["2330"] = deque([999.0, 999.0])  # 2 < 10
        with _mock_now(1000.0):
            result = prl.check("2330")
        assert result is PerSymbolRateResult.OK

    def test_check_at_soft_limit_returns_soft(self):
        prl = PerSymbolRateLimiter(soft_limit=3, hard_limit=10, window_s=60.0)
        prl._windows["2330"] = deque([999.0, 999.0, 999.0])  # == soft_limit
        with _mock_now(1000.0):
            result = prl.check("2330")
        assert result is PerSymbolRateResult.SOFT

    def test_check_at_hard_limit_returns_hard(self):
        prl = PerSymbolRateLimiter(soft_limit=3, hard_limit=5, window_s=60.0)
        prl._windows["2330"] = deque([999.0] * 5)  # == hard_limit
        with _mock_now(1000.0):
            result = prl.check("2330")
        assert result is PerSymbolRateResult.HARD

    def test_check_expires_stale_entries_before_evaluating(self):
        prl = PerSymbolRateLimiter(soft_limit=3, hard_limit=5, window_s=60.0)
        # 6 entries older than window — all should be evicted, result is OK
        prl._windows["2330"] = deque([800.0] * 6)
        with _mock_now(1000.0):
            result = prl.check("2330")
        assert result is PerSymbolRateResult.OK
        assert len(prl._windows["2330"]) == 0

    def test_record_creates_window_for_new_symbol(self):
        prl = PerSymbolRateLimiter(soft_limit=10, hard_limit=20, window_s=60.0)
        with _mock_now(500.0):
            prl.record("TXFD6")
        assert "TXFD6" in prl._windows
        assert len(prl._windows["TXFD6"]) == 1

    def test_record_appends_to_existing_window(self):
        prl = PerSymbolRateLimiter(soft_limit=10, hard_limit=20, window_s=60.0)
        with _mock_now(500.0):
            prl.record("TXFD6")
            prl.record("TXFD6")
        assert len(prl._windows["TXFD6"]) == 2

    def test_record_cardinality_limit_drops_new_symbol(self):
        prl = PerSymbolRateLimiter(soft_limit=10, hard_limit=20, window_s=60.0, max_symbols=2)
        with _mock_now(500.0):
            prl.record("SYM_A")
            prl.record("SYM_B")
            # Third symbol should be silently dropped
            prl.record("SYM_C")
        assert "SYM_C" not in prl._windows
        assert len(prl._windows) == 2

    def test_record_cardinality_limit_allows_existing_symbol(self):
        prl = PerSymbolRateLimiter(soft_limit=10, hard_limit=20, window_s=60.0, max_symbols=2)
        with _mock_now(500.0):
            prl.record("SYM_A")
            prl.record("SYM_B")
            # SYM_A already exists — should still be recorded
            prl.record("SYM_A")
        assert len(prl._windows["SYM_A"]) == 2

    def test_evict_idle_triggered_every_100_calls(self):
        prl = PerSymbolRateLimiter(soft_limit=10, hard_limit=20, window_s=60.0)
        # Pre-populate a window with expired entries
        prl._windows["STALE"] = deque([1.0, 2.0])  # old timestamps
        # Drive _call_count to 99 so the 100th record triggers _evict_idle
        prl._call_count = 99
        with _mock_now(1000.0):
            prl.record("ANY_SYMBOL")
        # STALE's entries were outside window; _evict_idle should have removed it
        assert "STALE" not in prl._windows

    def test_evict_idle_removes_empty_windows_only(self):
        prl = PerSymbolRateLimiter(soft_limit=10, hard_limit=20, window_s=60.0)
        prl._windows["LIVE"] = deque([990.0, 995.0])   # recent — should survive
        prl._windows["STALE"] = deque([1.0])            # expired — should be removed
        prl._call_count = 99
        with _mock_now(1000.0):
            prl.record("ANY_SYMBOL")
        assert "LIVE" in prl._windows
        assert "STALE" not in prl._windows

    def test_soft_and_hard_limit_properties(self):
        prl = PerSymbolRateLimiter(soft_limit=15, hard_limit=25, window_s=30.0)
        assert prl.soft_limit == 15
        assert prl.hard_limit == 25

    def test_env_var_defaults_are_respected(self):
        env = {
            "HFT_PER_SYMBOL_RATE_SOFT": "42",
            "HFT_PER_SYMBOL_RATE_HARD": "99",
            "HFT_PER_SYMBOL_RATE_WINDOW": "120",
        }
        with patch.dict(os.environ, env, clear=False):
            prl = PerSymbolRateLimiter()
        assert prl.soft_limit == 42
        assert prl.hard_limit == 99
        assert prl._window_s == 120.0
