import time

from hft_platform.order.rate_limiter import RateLimiter


def test_rate_limiter_soft_cap_and_purge(monkeypatch) -> None:
    rl = RateLimiter(soft_cap=2, hard_cap=3, window_s=1)
    now = 0.0
    monkeypatch.setattr(time, "time", lambda: now)

    rl.record()
    rl.record()
    assert rl.check() is True
    assert len(rl.rate_window) == 2

    now = 2.0
    assert rl.check() is True
    assert len(rl.rate_window) == 0


def test_rate_limiter_hard_cap(monkeypatch) -> None:
    rl = RateLimiter(soft_cap=2, hard_cap=3, window_s=10)
    now = 100.0
    monkeypatch.setattr(time, "time", lambda: now)

    rl.record()
    rl.record()
    rl.record()
    assert rl.check() is False


def test_rate_limiter_update() -> None:
    rl = RateLimiter(soft_cap=1, hard_cap=2, window_s=1)
    rl.update(soft_cap=3, hard_cap=4, window_s=5)
    assert rl.soft_cap == 3
    assert rl.hard_cap == 4
    assert rl.window_s == 5

