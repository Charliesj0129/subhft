"""A drawdown STORM must not freeze the number it is reading.

The drawdown is measured against a high-watermark of realised PnL, so the only
input that can lower it is a fill -- and STORM permits nothing but a
position-reducing intent, which a flat strategy can never produce. Without a
release the gate holds itself shut forever:

    drawdown >= storm_drawdown_bps -> STORM
        -> flat, so no intent reduces -> all blocked
        -> no fills -> realised PnL frozen -> drawdown frozen
        -> X  no exit

Observed on THESHOW 2026-09-08: 8 h 29 min, 2,346 rejections, one drawdown
value to four decimal places throughout.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.risk.storm_guard import StormGuard, StormGuardState

_STRATEGY = "R47_MAKER_TMF"
_SYMBOL = "TMFI6"


@pytest.fixture()
def guard():
    metrics = MagicMock()
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=metrics):
        g = StormGuard()
    g.metrics = metrics
    return g


def _intent(side: Side = Side.BUY, qty: int = 1) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=_STRATEGY,
        symbol=_SYMBOL,
        intent_type=IntentType.NEW,
        side=side,
        price=5_000_000,
        qty=qty,
    )


def _enter_drawdown_storm(guard, position: int = 0, elapsed_s: float = 10_000.0) -> None:
    """Drive the guard into a drawdown STORM with the cooldown already elapsed."""
    guard.set_position_provider(lambda symbol, strategy_id: position)
    guard.update(drawdown_bps=-150)
    assert guard.state is StormGuardState.STORM
    # Age the STORM past drawdown_flat_release_after_s without sleeping.
    guard._storm_entry_ts -= elapsed_s


def test_flat_strategy_is_released_from_a_drawdown_storm(guard):
    """The regression: flat, drawdown-pinned, and nothing else can free it."""
    _enter_drawdown_storm(guard, position=0)

    allowed, reason = guard.validate(_intent())

    assert allowed is True
    assert reason == "STORM_DRAWDOWN_FLAT_RELEASE"
    guard.metrics.stormguard_drawdown_flat_release_total.inc.assert_called_once()


def test_an_exposed_strategy_is_still_blocked_from_adding(guard):
    """Release is bounded: once exposed, an adding order is blocked again."""
    _enter_drawdown_storm(guard, position=1)

    # BUY on a long position adds -- not a reduction, and not flat.
    allowed, reason = guard.validate(_intent(side=Side.BUY))

    assert allowed is False
    assert reason == "STORMGUARD_STORM_BLOCKED"


def test_an_exposed_strategy_can_still_reduce(guard):
    """The pre-existing reduce-only path must be untouched."""
    _enter_drawdown_storm(guard, position=1)

    allowed, reason = guard.validate(_intent(side=Side.SELL))

    assert allowed is True
    assert reason == "STORM_REDUCE_ONLY"


def test_a_feed_gap_storm_does_not_release_a_flat_strategy(guard):
    """Only drawdown outlives its own evidence; an unhealthy platform does not."""
    guard.set_position_provider(lambda symbol, strategy_id: 0)
    guard.set_session_active(True)
    guard.update(feed_gap_s=5.0)
    assert guard.state is StormGuardState.STORM
    guard._storm_entry_ts -= 10_000.0

    allowed, reason = guard.validate(_intent())

    assert allowed is False
    assert reason == "STORMGUARD_STORM_BLOCKED"


def test_a_latency_storm_does_not_release_a_flat_strategy(guard):
    guard.set_position_provider(lambda symbol, strategy_id: 0)
    guard.update(latency_us=50_000)
    assert guard.state is StormGuardState.STORM
    guard._storm_entry_ts -= 10_000.0

    allowed, reason = guard.validate(_intent())

    assert allowed is False
    assert reason == "STORMGUARD_STORM_BLOCKED"


def test_release_waits_out_the_cooldown(guard):
    """A transient drawdown must still stop trading for the cooldown."""
    guard.set_position_provider(lambda symbol, strategy_id: 0)
    guard.update(drawdown_bps=-150)
    assert guard.state is StormGuardState.STORM

    # Cooldown has not elapsed.
    allowed, reason = guard.validate(_intent())
    assert allowed is False
    assert reason == "STORMGUARD_STORM_BLOCKED"

    guard._storm_entry_ts -= guard.thresholds.drawdown_flat_release_after_s + 1.0
    allowed, reason = guard.validate(_intent())
    assert allowed is True
    assert reason == "STORM_DRAWDOWN_FLAT_RELEASE"


def test_release_is_disabled_when_the_threshold_is_zero(guard):
    """0 restores the pre-2026-09-08 behaviour exactly."""
    guard.thresholds.drawdown_flat_release_after_s = 0.0
    _enter_drawdown_storm(guard, position=0)

    allowed, reason = guard.validate(_intent())

    assert allowed is False
    assert reason == "STORMGUARD_STORM_BLOCKED"


def test_release_fails_closed_without_a_position_provider(guard):
    """Flatness cannot be proven, so it is not assumed."""
    guard.update(drawdown_bps=-150)
    assert guard.state is StormGuardState.STORM
    guard._storm_entry_ts -= 10_000.0
    guard.set_position_provider(None)

    allowed, reason = guard.validate(_intent())

    assert allowed is False
    assert reason == "STORMGUARD_STORM_BLOCKED"


def test_release_fails_closed_when_the_position_provider_raises(guard):
    def _boom(symbol, strategy_id):
        raise RuntimeError("position store unavailable")

    guard.update(drawdown_bps=-150)
    assert guard.state is StormGuardState.STORM
    guard._storm_entry_ts -= 10_000.0
    guard.set_position_provider(_boom)

    allowed, reason = guard.validate(_intent())

    assert allowed is False
    assert reason == "STORMGUARD_STORM_BLOCKED"


def test_halt_is_not_released(guard):
    """HALT is a harder stop and this change must not reach it."""
    guard.set_position_provider(lambda symbol, strategy_id: 0)
    guard.update(drawdown_bps=-250)
    assert guard.state is StormGuardState.HALT
    guard._storm_entry_ts -= 10_000.0

    allowed, reason = guard.validate(_intent())

    assert allowed is False
    assert reason == "STORMGUARD_HALT"


def test_the_reason_tracked_is_the_live_one_not_the_entry_one(guard):
    """A STORM entered on latency and now held by drawdown must release.

    ``_target_state_reason`` is refreshed every ``update()``; the transition log
    records only why STORM was *entered*, which can be a different input.
    """
    guard.set_position_provider(lambda symbol, strategy_id: 0)
    guard.update(latency_us=50_000)
    assert guard.state is StormGuardState.STORM

    # Latency clears, drawdown is now what holds STORM.
    guard.update(drawdown_bps=-150, latency_us=0)
    assert guard.state is StormGuardState.STORM
    guard._storm_entry_ts -= 10_000.0

    allowed, reason = guard.validate(_intent())

    assert allowed is True
    assert reason == "STORM_DRAWDOWN_FLAT_RELEASE"
