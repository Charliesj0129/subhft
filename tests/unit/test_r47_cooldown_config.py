"""D2 (2026-04-21 incident): quote_cooldown_ms must be configurable and
default ≥ observed broker RTT.

Today's broker (Shioaji) P95 RTT is ~800 ms. Hardcoded cooldown of 200 ms
allows up to 4 same-side orders to be in flight before the first SUBMITTED
callback arrives; ``_active_*_oid`` is a single slot and gets overwritten,
orphaning earlier in-flight orders at the exchange.

Fix: expose ``quote_cooldown_ms`` as an __init__ parameter with default 1000
(covers P95 Shioaji RTT with safety margin). Operators can tune down for
low-latency brokers or tune up for congested conditions.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def strategy_cls():
    from hft_platform.strategies.r47_maker import R47MakerStrategy

    return R47MakerStrategy


class TestQuoteCooldownConfigurable:
    def test_default_cooldown_is_1000_ms(self, strategy_cls):
        s = strategy_cls(strategy_id="r47_test", spread_threshold_pts=5, max_pos=1)
        # Shioaji P95 RTT ~800ms — default must cover it.
        assert s._QUOTE_COOLDOWN_NS == 1_000_000_000, (
            f"D2: default quote cooldown must be 1000 ms to exceed Shioaji RTT; "
            f"got {s._QUOTE_COOLDOWN_NS} ns (= {s._QUOTE_COOLDOWN_NS / 1_000_000} ms)"
        )

    def test_explicit_cooldown_respected(self, strategy_cls):
        s = strategy_cls(strategy_id="r47_test", spread_threshold_pts=5, max_pos=1, quote_cooldown_ms=500)
        assert s._QUOTE_COOLDOWN_NS == 500_000_000

    def test_zero_cooldown_disables(self, strategy_cls):
        s = strategy_cls(strategy_id="r47_test", spread_threshold_pts=5, max_pos=1, quote_cooldown_ms=0)
        assert s._QUOTE_COOLDOWN_NS == 0

    def test_high_cooldown_accepted(self, strategy_cls):
        # Operator tune-up for congested conditions (e.g. 5 s).
        s = strategy_cls(strategy_id="r47_test", spread_threshold_pts=5, max_pos=1, quote_cooldown_ms=5000)
        assert s._QUOTE_COOLDOWN_NS == 5_000_000_000
