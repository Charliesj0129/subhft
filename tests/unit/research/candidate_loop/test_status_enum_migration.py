"""Every status the loop writes must be a label of the ClickHouse ``status`` Enum8.

An insert with an unknown Enum8 label fails, and the writer then falls back to
jsonl for the whole row: SHORTLISTED rows would silently stop reaching
``research.alpha_candidates``.
"""

from __future__ import annotations

import re
from pathlib import Path

from research.candidate_loop.schema import LEGACY_STATUS_LABELS, Status

MIGRATIONS = Path(__file__).resolve().parents[4] / "src" / "hft_platform" / "migrations" / "clickhouse"
_STATUS_ENUM = re.compile(r"status\s+Enum8\(([^)]*)\)", re.S)


def _latest_status_labels() -> set[str]:
    definitions: list[str] = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text().split("-- Down", 1)[0]
        if "research.alpha_candidates" in text:
            definitions.extend(_STATUS_ENUM.findall(text))
    assert definitions, "no status Enum8 definition for research.alpha_candidates"
    return set(re.findall(r"'([A-Z_]+)'\s*=", definitions[-1]))


def test_every_status_is_a_clickhouse_enum_label() -> None:
    assert {status.value for status in Status} <= _latest_status_labels()


def test_legacy_labels_stay_in_the_enum_so_old_rows_still_read() -> None:
    assert set(LEGACY_STATUS_LABELS) <= _latest_status_labels()
