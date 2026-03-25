"""Chaos tests for WAL replay drill.

Verifies WAL write integrity: row persistence, valid JSONL format,
multi-batch accumulation, and row count fidelity.
"""

import json
from pathlib import Path

import pytest

from hft_platform.recorder.wal import WALWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(i: int) -> dict:
    """Create a deterministic test row."""
    return {"id": i, "price": 1000000 + i, "qty": 10, "table": "test"}


def _count_jsonl_lines(wal_dir: Path) -> int:
    """Count total non-empty lines across all .jsonl files in wal_dir."""
    total = 0
    for f in sorted(wal_dir.glob("*.jsonl")):
        content = f.read_text()
        lines = [line for line in content.split("\n") if line.strip()]
        total += len(lines)
    return total


def _read_all_jsonl_lines(wal_dir: Path) -> list[str]:
    """Read all non-empty lines from all .jsonl files in wal_dir."""
    all_lines: list[str] = []
    for f in sorted(wal_dir.glob("*.jsonl")):
        content = f.read_text()
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped:
                all_lines.append(stripped)
    return all_lines


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.chaos
@pytest.mark.asyncio
async def test_wal_writes_all_rows(tmp_path: Path) -> None:
    """Write 100 rows, verify all persisted to .jsonl files."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()

    writer = WALWriter(str(wal_dir))
    writer._fsync_file_enabled = False

    rows = [_make_row(i) for i in range(100)]
    result = await writer.write("test_table", rows)

    assert result is True
    total = _count_jsonl_lines(wal_dir)
    assert total == 100, f"Expected 100 rows, got {total}"


@pytest.mark.chaos
@pytest.mark.asyncio
async def test_wal_files_are_valid_jsonl(tmp_path: Path) -> None:
    """Write 50 rows, verify every line is valid JSON."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()

    writer = WALWriter(str(wal_dir))
    writer._fsync_file_enabled = False

    rows = [_make_row(i) for i in range(50)]
    await writer.write("test_table", rows)

    all_lines = _read_all_jsonl_lines(wal_dir)
    assert len(all_lines) == 50

    for idx, line in enumerate(all_lines):
        try:
            parsed = json.loads(line)
            assert isinstance(parsed, dict), f"Line {idx} is not a dict: {type(parsed)}"
        except json.JSONDecodeError as exc:
            pytest.fail(f"Line {idx} is not valid JSON: {exc}\nContent: {line!r}")


@pytest.mark.chaos
@pytest.mark.asyncio
async def test_multiple_batches_accumulate(tmp_path: Path) -> None:
    """Write 5 batches of 10 rows, verify 50+ total lines."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()

    writer = WALWriter(str(wal_dir))
    writer._fsync_file_enabled = False

    for batch in range(5):
        rows = [_make_row(batch * 10 + i) for i in range(10)]
        result = await writer.write("test_table", rows)
        assert result is True

    total = _count_jsonl_lines(wal_dir)
    assert total >= 50, f"Expected >= 50 rows across batches, got {total}"


@pytest.mark.chaos
@pytest.mark.asyncio
async def test_wal_row_count_matches_input(tmp_path: Path) -> None:
    """Write 200 rows, verify output count >= input count."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()

    writer = WALWriter(str(wal_dir))
    writer._fsync_file_enabled = False

    rows = [_make_row(i) for i in range(200)]
    result = await writer.write("test_table", rows)

    assert result is True
    total = _count_jsonl_lines(wal_dir)
    assert total >= 200, f"Expected >= 200 rows, got {total}"
