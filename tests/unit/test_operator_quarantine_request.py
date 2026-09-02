"""An operator must be able to STOP a strategy without stopping the engine.

Measured 2026-09-03: containing ``R47_MAKER_TMF`` by setting ``enabled: false``
in ``config/live/strategies.yaml`` crash-looped the engine (RestartCount 0 ->
10, ~3.5 min down mid-session), because ``config/loader.py``
``_assert_strategy_enabled`` refuses to start while the loop binds a disabled
strategy. There was no runtime lever either: ``StrategyHealthGovernor``
``quarantine()`` / ``quarantine_async()`` existed with zero operator-reachable
callers, and ``hft ops`` offered only re-arm, autonomy-status and flatten.

So the only way to stop the platform's only strategy was to stop the engine.
These tests pin the channel that closes that gap, and the properties that make
it safe to leave in the supervisor loop.
"""

from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.core import timebase
from hft_platform.ops import quarantine_requests
from hft_platform.services.system import HFTSystem


def _publish(base: Path, strategy_id: str = "R47_MAKER_TMF", reason: str = "operator_request") -> str:
    request_id = f"req{len(quarantine_requests.pending(base))}"
    quarantine_requests.publish(base, strategy_id=strategy_id, reason=reason, request_id=request_id)
    return request_id


def _system(base: Path, governor: MagicMock) -> HFTSystem:
    """A bare rig: the consumer only needs the runner, governor and gate."""
    system = HFTSystem.__new__(HFTSystem)
    system.strategy_runner = types.SimpleNamespace(strategy_governor=governor)
    system.manual_rearm_gate = types.SimpleNamespace(state_path=base / "runtime_state.json")
    return system


def _governor(*, already_quarantined: bool = False) -> MagicMock:
    governor = MagicMock()
    governor.quarantine_token.return_value = "run-1:R47_MAKER_TMF:1" if already_quarantined else None
    governor.quarantine_async = AsyncMock()
    return governor


# --------------------------------------------------------------------------- #
# The channel                                                                  #
# --------------------------------------------------------------------------- #


def test_publish_then_pending_round_trips_the_request(tmp_path):
    _publish(tmp_path, reason="containment_2026_09_03")
    found = quarantine_requests.pending(tmp_path)

    assert len(found) == 1
    assert found[0].strategy_id == "R47_MAKER_TMF"
    assert found[0].reason == "containment_2026_09_03"
    assert found[0].requested_at_ns > 0


def test_duplicate_request_id_fails_instead_of_overwriting(tmp_path):
    quarantine_requests.publish(tmp_path, strategy_id="S", reason="r", request_id="dup")

    with pytest.raises(FileExistsError):
        quarantine_requests.publish(tmp_path, strategy_id="S", reason="r", request_id="dup")


def test_pending_is_empty_when_no_directory_exists(tmp_path):
    assert quarantine_requests.pending(tmp_path) == []


def test_a_request_without_a_usable_timestamp_is_expired(tmp_path):
    request = quarantine_requests.QuarantineRequest(
        path=tmp_path / "x.json",
        request_id="x",
        strategy_id="S",
        reason="r",
        requested_at_ns=0,
    )
    assert quarantine_requests.is_expired(request) is True


def test_an_old_request_is_expired(tmp_path):
    _publish(tmp_path)
    request = quarantine_requests.pending(tmp_path)[0]
    aged = request._replace(requested_at_ns=timebase.now_ns() - int(2 * 86_400 * 1e9))

    assert quarantine_requests.is_expired(aged) is True
    assert quarantine_requests.is_expired(request) is False


# --------------------------------------------------------------------------- #
# The engine-side consumer                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pending_request_quarantines_the_strategy_and_is_consumed(tmp_path):
    governor = _governor()
    system = _system(tmp_path, governor)
    _publish(tmp_path, reason="containment")

    await system._consume_strategy_quarantine_requests()

    governor.quarantine_async.assert_awaited_once_with("R47_MAKER_TMF", reason="containment")
    assert quarantine_requests.pending(tmp_path) == [], "an applied request must not replay"


@pytest.mark.asyncio
async def test_an_expired_request_is_retired_without_quarantining(tmp_path):
    governor = _governor()
    system = _system(tmp_path, governor)
    quarantine_requests.publish(tmp_path, strategy_id="S", reason="r", request_id="old")
    path = quarantine_requests.request_dir(tmp_path) / "old.json"
    path.write_text('{"request_id":"old","strategy_id":"S","reason":"r","requested_at_ns":1}')

    await system._consume_strategy_quarantine_requests()

    governor.quarantine_async.assert_not_awaited()
    assert quarantine_requests.pending(tmp_path) == []


@pytest.mark.asyncio
async def test_an_already_quarantined_strategy_is_not_relatched(tmp_path):
    """Re-latching would mint a new token and orphan a pending re-arm request."""
    governor = _governor(already_quarantined=True)
    system = _system(tmp_path, governor)
    _publish(tmp_path)

    await system._consume_strategy_quarantine_requests()

    governor.quarantine_async.assert_not_awaited()
    assert quarantine_requests.pending(tmp_path) == []


@pytest.mark.asyncio
async def test_a_failing_quarantine_leaves_the_request_for_the_next_tick(tmp_path):
    """Fail closed: losing the stop is worse than applying it twice."""
    governor = _governor()
    governor.quarantine_async = AsyncMock(side_effect=RuntimeError("disk full"))
    system = _system(tmp_path, governor)
    _publish(tmp_path)

    await system._consume_strategy_quarantine_requests()

    assert len(quarantine_requests.pending(tmp_path)) == 1, "the stop must survive to be retried"


@pytest.mark.asyncio
async def test_a_scan_failure_does_not_kill_the_supervisor(tmp_path, monkeypatch):
    governor = _governor()
    system = _system(tmp_path, governor)

    def _boom(_base):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(quarantine_requests, "pending", _boom)

    await system._consume_strategy_quarantine_requests()  # must not raise

    governor.quarantine_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_governor_is_a_no_op(tmp_path):
    system = HFTSystem.__new__(HFTSystem)
    system.strategy_runner = types.SimpleNamespace(strategy_governor=None)
    system.manual_rearm_gate = types.SimpleNamespace(state_path=tmp_path / "runtime_state.json")
    _publish(tmp_path)

    await system._consume_strategy_quarantine_requests()

    assert len(quarantine_requests.pending(tmp_path)) == 1


def test_consume_is_idempotent(tmp_path):
    _publish(tmp_path)
    request = quarantine_requests.pending(tmp_path)[0]

    quarantine_requests.consume(request)
    quarantine_requests.consume(request)  # must not raise

    assert quarantine_requests.pending(tmp_path) == []


@pytest.mark.asyncio
async def test_supervisor_applies_quarantine_after_rearm_in_the_same_tick(monkeypatch):
    """If an operator has both pending, the tick must end quarantined.

    Drives the real ``_update_platform_degrade_state`` rather than asserting an
    order the test itself arranged.
    """
    order: list[str] = []
    system = HFTSystem.__new__(HFTSystem)
    system.platform_degrade_controller = None
    system.platform_degrade_inputs = None

    async def _rearm(_state=None):
        order.append("rearm")

    async def _quarantine():
        order.append("quarantine")

    async def _platform_rearm(_state, _reasons):
        return None

    def _read_state():
        return {}

    system._read_rearm_state = _read_state
    system._consume_platform_rearm_request_async = _platform_rearm
    system._consume_strategy_rearm_requests = _rearm
    system._consume_strategy_quarantine_requests = _quarantine

    await system._update_platform_degrade_state()

    assert order == ["rearm", "quarantine"], (
        "a quarantine consumed before the re-arm would let the re-arm undo it in the same tick"
    )
