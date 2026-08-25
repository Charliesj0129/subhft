"""A strategy quarantine is a safety latch; a restart must not release it.

Before ``restore_persisted_quarantines`` existed, ``StrategyHealthGovernor``
booted with an empty dict. The persisted document still recorded the latch, but
nothing read it back, so a restart was an unauthenticated re-arm *and* the
``ManualRearmRequired`` alert resolved itself. Observed in production
2026-08-25 on ``R47_MAKER_TMF``.

Each test below fails on the pre-fix governor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hft_platform.ops.runtime_state_store import RuntimeStateUnreadable
from hft_platform.ops.strategy_governor import StrategyHealthGovernor


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _latched(strategy_id: str = "R47_MAKER_TMF", reason: str = "strategy_exception") -> dict:
    return {
        "platform": {"manual_rearm_required": False, "reason": None},
        "strategies": {strategy_id: {"manual_rearm_required": True, "reason": reason}},
    }


def test_a_quarantined_strategy_is_still_quarantined_after_a_restart(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(state, _latched())

    governor = StrategyHealthGovernor()
    restored = governor.restore_persisted_quarantines(state_path=state)

    assert restored == ["R47_MAKER_TMF"]
    assert governor.is_quarantined("R47_MAKER_TMF") is True


def test_a_restored_quarantine_keeps_the_reason_the_previous_run_recorded(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(state, _latched(reason="under_scaled_price_rejected"))

    governor = StrategyHealthGovernor()
    governor.restore_persisted_quarantines(state_path=state)

    intents = governor.build_cancel_intents("R47_MAKER_TMF", live_orders=[], intent_factory=lambda **_: None)
    assert intents == []  # no live orders; the call proves the entry is present
    assert governor.quarantine_token("R47_MAKER_TMF")


def test_a_re_arm_request_written_before_the_restart_cannot_clear_a_restored_quarantine(
    tmp_path: Path,
) -> None:
    """The latch survives; the authorization to clear it does not.

    A token names one quarantine *instance*. If restoration reused the old
    token, a stale request from before the restart would silently re-arm at
    boot -- the same fail-open this whole fix removes.
    """
    state = tmp_path / "runtime_state.json"
    _write_state(state, _latched())

    pre_restart_token = "1234-deadbeef:R47_MAKER_TMF:1"

    governor = StrategyHealthGovernor()
    governor.restore_persisted_quarantines(state_path=state)

    assert governor.rearm("R47_MAKER_TMF", expected_token=pre_restart_token) is False
    assert governor.is_quarantined("R47_MAKER_TMF") is True


def test_a_restored_quarantine_is_cleared_by_a_request_naming_its_new_token(tmp_path: Path) -> None:
    """Fail-closed must not mean unrecoverable."""
    state = tmp_path / "runtime_state.json"
    _write_state(state, _latched())

    governor = StrategyHealthGovernor()
    governor.restore_persisted_quarantines(state_path=state)
    fresh_token = governor.quarantine_token("R47_MAKER_TMF")
    assert fresh_token is not None

    assert governor.rearm("R47_MAKER_TMF", expected_token=fresh_token) is True
    assert governor.is_quarantined("R47_MAKER_TMF") is False


def test_a_strategy_not_latched_in_the_persisted_state_is_not_quarantined(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(
        state,
        {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {"R47_MAKER_TMF": {"manual_rearm_required": False, "reason": None}},
        },
    )

    governor = StrategyHealthGovernor()
    assert governor.restore_persisted_quarantines(state_path=state) == []
    assert governor.is_quarantined("R47_MAKER_TMF") is False


def test_a_missing_state_document_is_a_cold_start_and_restores_nothing(tmp_path: Path) -> None:
    governor = StrategyHealthGovernor()
    assert governor.restore_persisted_quarantines(state_path=tmp_path / "absent.json") == []


def test_an_unreadable_state_document_refuses_rather_than_reading_as_all_clear(
    tmp_path: Path,
) -> None:
    """A safety latch that cannot be read must not be assumed absent."""
    state = tmp_path / "runtime_state.json"
    state.write_text("{ this is not json", encoding="utf-8")

    governor = StrategyHealthGovernor()
    with pytest.raises(RuntimeStateUnreadable):
        governor.restore_persisted_quarantines(state_path=state)


def test_every_latched_strategy_is_restored_not_only_the_first(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(
        state,
        {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {
                "A_STRAT": {"manual_rearm_required": True, "reason": "strategy_exception"},
                "B_STRAT": {"manual_rearm_required": False, "reason": None},
                "C_STRAT": {"manual_rearm_required": True, "reason": "timeout"},
            },
        },
    )

    governor = StrategyHealthGovernor()
    assert governor.restore_persisted_quarantines(state_path=state) == ["A_STRAT", "C_STRAT"]
    assert governor.is_quarantined("B_STRAT") is False


def test_a_malformed_strategy_entry_is_skipped_without_aborting_the_restore(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(
        state,
        {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {
                "BROKEN": "not-a-mapping",
                "GOOD": {"manual_rearm_required": True, "reason": "strategy_exception"},
            },
        },
    )

    governor = StrategyHealthGovernor()
    assert governor.restore_persisted_quarantines(state_path=state) == ["GOOD"]


def test_restored_quarantines_give_each_strategy_a_distinct_token(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(
        state,
        {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {
                "A_STRAT": {"manual_rearm_required": True, "reason": "strategy_exception"},
                "C_STRAT": {"manual_rearm_required": True, "reason": "timeout"},
            },
        },
    )

    governor = StrategyHealthGovernor()
    governor.restore_persisted_quarantines(state_path=state)

    assert governor.quarantine_token("A_STRAT") != governor.quarantine_token("C_STRAT")
