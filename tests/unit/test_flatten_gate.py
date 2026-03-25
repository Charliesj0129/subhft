"""Tests for FlattenGate file-based IPC."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hft_platform.ops.flatten_gate import FlattenGate, FlattenRequest, FlattenStatus


@pytest.fixture()
def gate(tmp_path: Path) -> FlattenGate:
    return FlattenGate(path=tmp_path / "flatten_request.json")


class TestFlattenGate:
    def test_submit_request_creates_file(self, gate: FlattenGate) -> None:
        req = gate.submit(scope="all", deadline_s=60)
        assert gate.path.exists()
        data = json.loads(gate.path.read_text(encoding="utf-8"))
        assert data["scope"] == "all"
        assert data["status"] == "PENDING"
        assert data["deadline_s"] == 60
        assert req.status == FlattenStatus.PENDING

    def test_read_request_returns_none_when_no_file(self, gate: FlattenGate) -> None:
        assert gate.read_request() is None

    def test_claim_request_transitions_to_processing(self, gate: FlattenGate) -> None:
        gate.submit(scope="strategy", scope_id="mm1", deadline_s=30)
        claimed = gate.claim()
        assert claimed is not None
        assert claimed.status == FlattenStatus.PROCESSING
        assert claimed.scope == "strategy"
        assert claimed.scope_id == "mm1"
        # File on disk should also reflect PROCESSING
        on_disk = gate.read_request()
        assert on_disk is not None
        assert on_disk.status == FlattenStatus.PROCESSING

    def test_complete_request_records_result(self, gate: FlattenGate) -> None:
        gate.submit(scope="all", deadline_s=120)
        gate.claim()
        gate.complete(
            fully_closed=5,
            partially_closed=1,
            failed=2,
            failed_symbols=["2330", "2317"],
        )
        req = gate.read_request()
        assert req is not None
        assert req.status == FlattenStatus.COMPLETED
        assert req.fully_closed == 5
        assert req.partially_closed == 1
        assert req.failed == 2
        assert req.failed_symbols == ["2330", "2317"]

    def test_fail_request_records_error(self, gate: FlattenGate) -> None:
        gate.submit(scope="track", scope_id="t1", deadline_s=60)
        gate.claim()
        gate.fail("broker_timeout")
        req = gate.read_request()
        assert req is not None
        assert req.status == FlattenStatus.FAILED
        assert req.error == "broker_timeout"

    def test_claim_returns_none_if_not_pending(self, gate: FlattenGate) -> None:
        # No file at all
        assert gate.claim() is None
        # Already processing
        gate.submit(scope="all", deadline_s=120)
        gate.claim()
        assert gate.claim() is None

    def test_atomic_write_survives_concurrent_read(self, gate: FlattenGate) -> None:
        """Submit from one thread, read from another — no corruption."""
        gate.submit(scope="all", deadline_s=120)
        results: list[FlattenRequest | None] = []
        errors: list[Exception] = []

        def reader() -> None:
            try:
                for _ in range(50):
                    req = gate.read_request()
                    results.append(req)
            except Exception as exc:
                errors.append(exc)

        def writer() -> None:
            try:
                for _ in range(50):
                    gate.submit(scope="all", deadline_s=120)
            except Exception as exc:
                errors.append(exc)

        t_read = threading.Thread(target=reader)
        t_write = threading.Thread(target=writer)
        t_read.start()
        t_write.start()
        t_read.join(timeout=5)
        t_write.join(timeout=5)

        assert not errors, f"Concurrent read/write errors: {errors}"
        # All successful reads should return valid requests (or None during brief rename)
        for req in results:
            if req is not None:
                assert req.scope == "all"
                assert req.status in (FlattenStatus.PENDING, FlattenStatus.PROCESSING)

    def test_clear_removes_file(self, gate: FlattenGate) -> None:
        gate.submit(scope="all", deadline_s=120)
        assert gate.path.exists()
        gate.clear()
        assert not gate.path.exists()
        assert gate.read_request() is None
