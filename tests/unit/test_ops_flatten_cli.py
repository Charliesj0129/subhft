"""Tests for CLI flatten command integration with FlattenGate."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from hft_platform.cli._ops import _flatten_via_gate
from hft_platform.ops.flatten_gate import FlattenGate, FlattenStatus


@pytest.fixture()
def gate(tmp_path: Path) -> FlattenGate:
    return FlattenGate(path=tmp_path / "flatten_request.json")


class TestOpsFlattenCli:
    def test_cli_submits_flatten_request(self, gate: FlattenGate) -> None:
        """Verify that _flatten_via_gate creates a request file."""

        # Simulate engine completing the request immediately
        def engine_sim() -> None:
            for _ in range(20):
                req = gate.read_request()
                if req is not None and req.status == FlattenStatus.PENDING:
                    gate.claim()
                    gate.complete(fully_closed=3, partially_closed=0, failed=0)
                    return
                time.sleep(0.05)

        t = threading.Thread(target=engine_sim)
        t.start()

        result = _flatten_via_gate(
            scope="all",
            scope_id=None,
            deadline=120,
            gate=gate,
            poll_timeout_s=3.0,
        )
        t.join(timeout=5)

        assert result is not None
        assert result.status == FlattenStatus.COMPLETED
        assert result.fully_closed == 3

    def test_cli_reports_completed_result(self, gate: FlattenGate) -> None:
        """Verify CLI handles engine response with failed symbols."""

        def engine_sim() -> None:
            for _ in range(20):
                req = gate.read_request()
                if req is not None and req.status == FlattenStatus.PENDING:
                    gate.claim()
                    gate.complete(
                        fully_closed=2,
                        partially_closed=1,
                        failed=1,
                        failed_symbols=["2330"],
                    )
                    return
                time.sleep(0.05)

        t = threading.Thread(target=engine_sim)
        t.start()

        result = _flatten_via_gate(
            scope="strategy",
            scope_id="mm1",
            deadline=60,
            gate=gate,
            poll_timeout_s=3.0,
        )
        t.join(timeout=5)

        assert result is not None
        assert result.status == FlattenStatus.COMPLETED
        assert result.fully_closed == 2
        assert result.partially_closed == 1
        assert result.failed == 1
        assert result.failed_symbols == ["2330"]
