"""``runtime_state.json`` is written by two processes; neither may lose the other's update.

The engine (``AutonomyEvidenceWriter``) and the operator CLI
(``ManualRearmGate``) both read the whole document, mutate one section, and
replace it. Making each replacement atomic stops a torn file; it does not stop a
lost update. Reproduced deterministically on 2026-08-25: the CLI's write-back
erased a platform latch the engine had persisted in between, and startup
restores platform reduce-only only from that flag -- so a later restart would
have booted NORMAL with no operator re-arm.
"""

from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from hft_platform.ops.evidence import AutonomyEvidenceWriter
from hft_platform.ops.manual_rearm import ManualRearmGate
from hft_platform.ops.runtime_state_store import locked_state, read_state

STRATEGY = "S1"


def _seed(base: Path) -> AutonomyEvidenceWriter:
    writer = AutonomyEvidenceWriter(base_dir=base)
    writer.record_transition(
        scope="strategy",
        mode="STRATEGY_QUARANTINED",
        reason="handler_exception",
        metadata={"strategy_id": STRATEGY, "quarantine_token": "run:S1:1"},
    )
    return writer


def _cli_worker(base_str: str, ready: mp.Barrier) -> None:  # pragma: no cover - child process
    gate = ManualRearmGate(state_path=Path(base_str) / "runtime_state.json")
    ready.wait(timeout=10)
    gate.rearm_strategy(STRATEGY)


def _engine_worker(base_str: str, ready: mp.Barrier) -> None:  # pragma: no cover - child process
    writer = AutonomyEvidenceWriter(base_dir=Path(base_str))
    ready.wait(timeout=10)
    writer.record_transition(
        scope="platform",
        mode="PLATFORM_REDUCE_ONLY",
        reason="clickhouse_unhealthy",
    )


def test_concurrent_cli_and_engine_writes_both_survive(tmp_path):
    """Both updates must be present regardless of which writer wins the lock."""
    _seed(tmp_path)
    ctx = mp.get_context("fork")
    ready = ctx.Barrier(2)
    procs = [
        ctx.Process(target=_cli_worker, args=(str(tmp_path), ready)),
        ctx.Process(target=_engine_worker, args=(str(tmp_path), ready)),
    ]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=20)
        assert proc.exitcode == 0, f"worker failed: {proc.exitcode}"

    state = read_state(tmp_path / "runtime_state.json")
    assert state["platform"]["manual_rearm_required"] is True, "engine's platform latch was lost"
    assert state["platform"]["reason"] == "clickhouse_unhealthy"
    assert state["strategies"][STRATEGY]["rearm_request"]["quarantine_token"] == "run:S1:1", (
        "CLI's re-arm request was lost"
    )


def test_the_lock_spans_the_whole_read_modify_write(tmp_path):
    """A reader entering mid-transaction must not observe a partial mutation."""
    path = tmp_path / "runtime_state.json"
    with locked_state(path) as state:
        state["platform"]["manual_rearm_required"] = True
        state["platform"]["reason"] = "in_flight"
        # Nothing is published while the block is open.
        assert not path.exists() or read_state(path)["platform"]["manual_rearm_required"] is False

    assert read_state(path)["platform"]["manual_rearm_required"] is True


def test_an_exception_inside_the_transaction_publishes_nothing(tmp_path):
    path = tmp_path / "runtime_state.json"
    with locked_state(path) as state:
        state["platform"]["manual_rearm_required"] = True

    with pytest.raises(RuntimeError):
        with locked_state(path) as state:
            state["platform"]["reason"] = "half_written"
            raise RuntimeError("boom")

    survived = read_state(path)
    assert survived["platform"]["manual_rearm_required"] is True
    assert survived["platform"]["reason"] is None


def test_no_temp_or_lock_files_leak_into_the_state_dir(tmp_path):
    path = tmp_path / "runtime_state.json"
    for _ in range(3):
        with locked_state(path) as state:
            state["strategies"]["x"] = {"manual_rearm_required": True, "reason": "r"}

    assert list(tmp_path.glob("*.tmp")) == []
