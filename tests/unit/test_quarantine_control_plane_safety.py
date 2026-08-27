"""Three ways the quarantine control plane could hurt the engine.

Each was raised by a Codex review of the re-arm work and reproduced here first:

* a re-arm acknowledgement minted for one quarantine could erase a *different,
  newer* quarantine, because the branch that clears the persisted latch never
  compared the token the branch that sets it goes to the trouble of recording;
* a failed ``unlink`` of a consumed request propagated out of the supervisor and
  took ``HFTSystem.run()`` down -- and since the request survived, so did the
  next boot, and the next: a crash loop out of a housekeeping step;
* the durable write ran on the event loop. ``locked_state`` polls ``flock`` with
  ``time.sleep`` up to a two-second deadline and then reads and rewrites JSON,
  so a CLI ``rearm-strategy`` holding that lock could stall feed, risk and order
  processing for two seconds -- against a 1 ms budget, while a strategy failed;
* that write was atomic but not *durable*: no ``fsync`` of the payload or of the
  directory the rename creates an entry in. Startup reads an absent latch as an
  all-clear, so a write lost to a host crash is an unauthenticated re-arm;
* and the latch was read from disk exactly once, at boot. Another engine can
  persist a quarantine a moment later -- session ownership is a non-blocking
  preflight -- and nothing ever looked again, so the strategy kept trading.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hft_platform.ops import rearm_requests
from hft_platform.ops.evidence import AutonomyEvidenceWriter
from hft_platform.ops.manual_rearm import ManualRearmGate
from hft_platform.ops.strategy_governor import StrategyHealthGovernor
from hft_platform.services.system import HFTSystem

STRATEGY = "R47_MAKER_TMF"


@pytest.fixture()
def rig(tmp_path: Path) -> SimpleNamespace:
    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    governor = StrategyHealthGovernor(evidence_writer=writer)
    gate = ManualRearmGate(state_path=tmp_path / "runtime_state.json")
    system = HFTSystem.__new__(HFTSystem)
    system.manual_rearm_gate = gate
    system.strategy_runner = SimpleNamespace(strategy_governor=governor)
    return SimpleNamespace(governor=governor, gate=gate, system=system, writer=writer, base=tmp_path)


def _persisted(rig: SimpleNamespace) -> dict[str, Any]:
    entry = json.loads((rig.base / "runtime_state.json").read_text(encoding="utf-8"))
    return entry["strategies"].get(STRATEGY) or {}


def _ack(rig: SimpleNamespace, *, token: str) -> None:
    """The record ``StrategyHealthGovernor.rearm`` writes when it clears a latch."""
    rig.writer.record_transition(
        scope="strategy",
        mode="normal",
        reason="manual_rearm",
        manual_rearm_required=False,
        metadata={"strategy_id": STRATEGY, "quarantine_token": token, "request_id": "req-1"},
    )


# ---------------------------------------------------------------------------
# A stale acknowledgement must not clear a newer latch
# ---------------------------------------------------------------------------


def test_a_stale_rearm_ack_does_not_clear_a_newer_quarantine(rig: SimpleNamespace) -> None:
    """Two engines can overlap: session ownership is advisory.

    Engine A is completing a re-arm for T1 while engine B persists a fresh T2
    quarantine. A's acknowledgement must not clear B's latch, or the next
    restart hydrates nothing and resumes a strategy nobody authorized.
    """
    rig.governor.quarantine(STRATEGY, reason="first")
    stale_token = str(_persisted(rig)["quarantine_token"])
    rig.governor.quarantine(STRATEGY, reason="second")
    newer_token = str(_persisted(rig)["quarantine_token"])
    assert newer_token != stale_token, "fixture did not mint a second token"

    _ack(rig, token=stale_token)

    entry = _persisted(rig)
    assert entry["manual_rearm_required"] is True
    assert entry["quarantine_token"] == newer_token


def test_a_matching_rearm_ack_clears_the_latch(rig: SimpleNamespace) -> None:
    """The guard must not refuse the legitimate case."""
    rig.governor.quarantine(STRATEGY, reason="first")
    token = str(_persisted(rig)["quarantine_token"])

    _ack(rig, token=token)

    assert _persisted(rig)["manual_rearm_required"] is False


def test_a_rearm_ack_clears_a_latch_that_predates_tokens(rig: SimpleNamespace) -> None:
    """A latch with no persisted token must not become permanently unclearable."""
    state = rig.base / "runtime_state.json"
    state.write_text(
        json.dumps(
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {STRATEGY: {"manual_rearm_required": True, "reason": "legacy"}},
            }
        ),
        encoding="utf-8",
    )

    _ack(rig, token="whatever-the-operator-named")

    assert _persisted(rig)["manual_rearm_required"] is False


# ---------------------------------------------------------------------------
# A failed delete must not stop supervision
# ---------------------------------------------------------------------------


def test_a_failed_request_delete_does_not_stop_supervision(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)

    def _explode(request: Any) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(rearm_requests, "consume", _explode)

    rig.system._consume_strategy_rearm_requests()  # must not raise

    # Fail-closed: the latch is still held and the request is still on disk, so
    # the next tick retries rather than the engine dying and the request
    # re-killing it on every boot.
    assert rig.governor.is_quarantined(STRATEGY)
    assert rearm_requests.pending(rig.gate.state_path.parent)


def test_the_request_is_applied_once_the_delete_succeeds_again(rig: SimpleNamespace) -> None:
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)

    rig.system._consume_strategy_rearm_requests()

    assert not rig.governor.is_quarantined(STRATEGY)
    assert not rearm_requests.pending(rig.gate.state_path.parent)


# ---------------------------------------------------------------------------
# The durable write must not run on the event loop
# ---------------------------------------------------------------------------


class _ThreadRecordingWriter:
    """Records which thread the durable write happened on."""

    def __init__(self) -> None:
        self.thread_ident: int | None = None

    def record_transition(self, **_kwargs: Any) -> dict[str, Any]:
        self.thread_ident = threading.get_ident()
        return {}


def test_quarantine_async_writes_the_durable_record_off_the_event_loop() -> None:
    writer = _ThreadRecordingWriter()
    governor = StrategyHealthGovernor(evidence_writer=writer)

    async def _run() -> int:
        await governor.quarantine_async(STRATEGY, reason="strategy_exception")
        return threading.get_ident()

    loop_thread = asyncio.run(_run())

    assert writer.thread_ident is not None, "the durable write never ran"
    assert writer.thread_ident != loop_thread


def test_quarantine_async_latches_before_the_durable_write() -> None:
    """The latch is what stops the strategy, so it may not wait on the write."""
    governor = StrategyHealthGovernor()
    latched_during_write: list[bool] = []

    class _Observer:
        def record_transition(self, **_kwargs: Any) -> dict[str, Any]:
            latched_during_write.append(governor.is_quarantined(STRATEGY))
            return {}

    governor.evidence_writer = _Observer()

    asyncio.run(governor.quarantine_async(STRATEGY, reason="strategy_exception"))

    assert latched_during_write == [True]
    assert governor.is_quarantined(STRATEGY)


def test_a_failed_durable_write_surfaces_to_the_caller() -> None:
    """Awaited, not fired and forgotten: a lost latch must not be silent."""

    class _Failing:
        def record_transition(self, **_kwargs: Any) -> dict[str, Any]:
            raise OSError("runtime_state.json is unwritable")

    governor = StrategyHealthGovernor(evidence_writer=_Failing())

    with pytest.raises(OSError):
        asyncio.run(governor.quarantine_async(STRATEGY, reason="strategy_exception"))

    # The in-memory latch still holds: the strategy is stopped either way.
    assert governor.is_quarantined(STRATEGY)


# ---------------------------------------------------------------------------
# The durable write must survive a crash, not just a concurrent reader
# ---------------------------------------------------------------------------


def test_the_payload_is_fsynced_before_the_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``os.replace`` orders nothing against the disk; the fsync does."""
    from hft_platform.ops import runtime_state_store

    calls: list[str] = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def _fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def _replace(self: Path, target: Any) -> Path:
        calls.append("replace")
        return real_replace(self, target)

    monkeypatch.setattr(runtime_state_store.os, "fsync", _fsync)
    monkeypatch.setattr(Path, "replace", _replace)

    runtime_state_store._atomic_write(tmp_path / "runtime_state.json", {"platform": {}, "strategies": {}})

    assert calls[:2] == ["fsync", "replace"], f"payload was not durable before the rename: {calls}"


def test_the_directory_entry_is_fsynced_after_the_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The rename is directory metadata and needs its own flush."""
    from hft_platform.ops import runtime_state_store

    calls: list[str] = []
    real_fsync = os.fsync
    real_replace = Path.replace

    def _fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def _replace(self: Path, target: Any) -> Path:
        calls.append("replace")
        return real_replace(self, target)

    monkeypatch.setattr(runtime_state_store.os, "fsync", _fsync)
    monkeypatch.setattr(Path, "replace", _replace)

    runtime_state_store._atomic_write(tmp_path / "runtime_state.json", {"platform": {}, "strategies": {}})

    assert calls == ["fsync", "replace", "fsync"], f"the rename was not flushed: {calls}"


def test_a_directory_that_refuses_fsync_does_not_fail_the_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some filesystems refuse it; losing the write would be the worse outcome."""
    from hft_platform.ops import runtime_state_store

    monkeypatch.setattr(runtime_state_store.os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    path = tmp_path / "runtime_state.json"

    runtime_state_store._atomic_write(path, {"platform": {}, "strategies": {}})

    assert json.loads(path.read_text(encoding="utf-8"))["strategies"] == {}


# ---------------------------------------------------------------------------
# A latch written after boot must still be adopted
# ---------------------------------------------------------------------------


def _document(*, token: str | None, latched: bool = True) -> dict[str, Any]:
    entry: dict[str, Any] = {"manual_rearm_required": latched, "reason": "written_elsewhere"}
    if token is not None:
        entry["quarantine_token"] = token
    return {"platform": {"manual_rearm_required": False, "reason": None}, "strategies": {STRATEGY: entry}}


def test_a_latch_written_after_boot_is_adopted_on_the_next_tick(rig: SimpleNamespace) -> None:
    """The boot restore reads once; another live engine can write afterwards."""
    assert not rig.governor.is_quarantined(STRATEGY)

    adopted = rig.governor.reconcile_persisted_quarantines(_document(token="other-engine:1"))

    assert adopted == [STRATEGY]
    assert rig.governor.quarantine_token(STRATEGY) == "other-engine:1"


def test_reconcile_does_not_overwrite_a_live_latch(rig: SimpleNamespace) -> None:
    """``quarantine_async`` latches before its write lands.

    A tick in that window sees the *previous* token on disk. Replacing the live
    one with it would break the protocol the token exists for.
    """
    rig.governor.quarantine(STRATEGY, reason="local")
    live_token = rig.governor.quarantine_token(STRATEGY)

    adopted = rig.governor.reconcile_persisted_quarantines(_document(token="an-older-token"))

    assert adopted == []
    assert rig.governor.quarantine_token(STRATEGY) == live_token


def test_a_tokenless_legacy_latch_is_not_adopted_on_a_tick(rig: SimpleNamespace) -> None:
    """Minting an identity is a write, and the boot restore owns that migration."""
    assert rig.governor.reconcile_persisted_quarantines(_document(token=None)) == []


def test_a_cleared_entry_is_not_adopted(rig: SimpleNamespace) -> None:
    assert rig.governor.reconcile_persisted_quarantines(_document(token="t", latched=False)) == []


def test_an_unparseable_document_does_not_stop_supervision(
    rig: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot is where an unreadable safety document fails closed, not a tick."""

    def _explode(_snapshot: Any) -> list[str]:
        raise ValueError("strategies section is corrupt")

    monkeypatch.setattr(rig.governor, "reconcile_persisted_quarantines", _explode)
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)

    rig.system._consume_strategy_rearm_requests({"platform": {}, "strategies": {}})  # must not raise

    # The tick adopted nothing, but everything after the reconcile still ran.
    assert not rig.governor.is_quarantined(STRATEGY)
