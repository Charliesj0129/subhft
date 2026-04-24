"""C3: startup_verifier.recover() must complete BEFORE exec_service.run()
starts so that execution-router does not double-apply fills on top of
the canonical broker-authoritative position baseline.

Root cause: ``HFTSystem.run()`` started exec_router (line 345, pre-fix)
before ``startup_verifier.recover()`` (line 356). exec_router begins
consuming raw_exec_queue and mutating position_store; recovery then
overwrites position_store with broker snapshot, silently dropping the
fills exec_router already applied.

Fix: move ``recover()`` (and the fill backfill recon) ahead of the
exec_router ``_start_service`` call; fills arriving during recovery
pile up in raw_exec_queue (bounded, with overflow buffer) and are
consumed only once the canonical baseline is loaded.

The test asserts the ordering guarantee at the source level by reading
``services/system.py`` and checking the line index of ``recover()``
relative to ``_start_service("exec_router", ...)``. Source-ordering
assertion is intentional — mocking the entire run() path to observe
call order is brittle and invites false greens.
"""

from __future__ import annotations

from pathlib import Path

SYSTEM_SRC = (
    Path(__file__).resolve().parents[2] / "src" / "hft_platform" / "services" / "system.py"
)


def test_recover_precedes_exec_router_start():
    source = SYSTEM_SRC.read_text()
    lines = source.splitlines()

    # Locate the first exec_router start and the first recover() call
    # inside the run() coroutine. The full block is inside a single
    # try/except inside run(); scanning the whole file is sufficient
    # because no other location uses those tokens.
    def first_line(token: str) -> int:
        for i, line in enumerate(lines):
            if token in line:
                return i
        raise AssertionError(f"token {token!r} not found in system.py")

    exec_router_line = first_line('_start_service("exec_router"')
    recover_line = first_line("self.startup_verifier.recover(")

    assert recover_line < exec_router_line, (
        f"C3 invariant: recover() (line {recover_line + 1}) must execute "
        f"BEFORE exec_router start (line {exec_router_line + 1})"
    )
