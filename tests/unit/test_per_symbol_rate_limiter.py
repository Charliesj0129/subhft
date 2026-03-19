"Tests for WU-06: Per-Symbol Rate Limiter."
from unittest.mock import patch
from hft_platform.order.rate_limiter import PerSymbolRateLimiter, PerSymbolRateResult

class TestPerSymbolRateResult:
    def test_enum_values(self):
        assert PerSymbolRateResult.OK.value == "ok"
        assert PerSymbolRateResult.SOFT.value == "soft"
        assert PerSymbolRateResult.HARD.value == "hard"

class TestPerSymbolRateLimiterDefaults:
    def test_defaults(self):
        limiter = PerSymbolRateLimiter()
        assert limiter.soft_limit == 30
        assert limiter.hard_limit == 50
    def test_env_defaults(self, monkeypatch):
        monkeypatch.setenv("HFT_PER_SYMBOL_RATE_SOFT", "15")
        monkeypatch.setenv("HFT_PER_SYMBOL_RATE_HARD", "25")
        limiter = PerSymbolRateLimiter()
        assert limiter.soft_limit == 15
        assert limiter.hard_limit == 25

class TestPerSymbolRateLimiterCheck:
    def test_ok(self):
        limiter = PerSymbolRateLimiter(soft_limit=5, hard_limit=10)
        assert limiter.check("2330") == PerSymbolRateResult.OK
    def test_soft(self):
        limiter = PerSymbolRateLimiter(soft_limit=3, hard_limit=10, window_s=60.0)
        for _ in range(3):
            limiter.record("2330")
        assert limiter.check("2330") == PerSymbolRateResult.SOFT
    def test_hard(self):
        limiter = PerSymbolRateLimiter(soft_limit=3, hard_limit=5, window_s=60.0)
        for _ in range(5):
            limiter.record("2330")
        assert limiter.check("2330") == PerSymbolRateResult.HARD
    def test_independent(self):
        limiter = PerSymbolRateLimiter(soft_limit=3, hard_limit=5, window_s=60.0)
        for _ in range(5):
            limiter.record("2330")
        assert limiter.check("2317") == PerSymbolRateResult.OK
    def test_window_expiry(self):
        limiter = PerSymbolRateLimiter(soft_limit=3, hard_limit=5, window_s=1.0)
        with patch("hft_platform.order.rate_limiter.timebase") as m:
            m.now_s.return_value = 100.0
            for _ in range(5):
                limiter.record("2330")
            m.now_s.return_value = 102.0
            assert limiter.check("2330") == PerSymbolRateResult.OK

class TestPerSymbolCardinality:
    def test_bound(self):
        limiter = PerSymbolRateLimiter(soft_limit=3, hard_limit=5, max_symbols=3)
        limiter.record("A")
        limiter.record("B")
        limiter.record("C")
        limiter.record("D")
        assert "D" not in limiter._windows
