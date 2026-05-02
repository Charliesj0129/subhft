"""Tests that audit.py passes Int64 nanoseconds (not datetime) to ClickHouse for ts column."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_gate_report(passed: bool = True) -> object:
    """Return a minimal GateReport-like object."""
    report = MagicMock()
    report.gate = "Gate A"
    report.passed = passed
    report.details = {}
    return report


def test_log_gate_result_ts_is_int_nanoseconds() -> None:
    """log_gate_result must pass an int in nanosecond range as ts to ClickHouse insert."""
    captured_rows: list = []

    mock_client = MagicMock()

    def capture_insert(table, rows, column_names):  # noqa: ANN001
        captured_rows.extend(rows)

    mock_client.insert.side_effect = capture_insert

    import hft_platform.alpha.audit as audit_mod

    with (
        patch.object(audit_mod, "_get_client", return_value=mock_client),
        patch.object(audit_mod, "_is_enabled", return_value=True),
    ):
        gate_report = _make_gate_report(passed=True)
        audit_mod.log_gate_result(
            alpha_id="test_alpha",
            run_id="run-001",
            gate_report=gate_report,  # type: ignore[arg-type]
            config_hash="abc123",
        )

    assert len(captured_rows) == 1, "Expected exactly one row inserted"
    row = captured_rows[0]
    ts_value = row[0]  # ts is first column

    assert isinstance(ts_value, int), f"ts must be int for ClickHouse Int64, got {type(ts_value).__name__}"
    # Nanosecond epoch for 2020-01-01 is ~1.577e18; sanity-check lower bound
    assert ts_value > 1_577_836_800_000_000_000, f"ts value {ts_value} is too small to be a nanosecond epoch"
