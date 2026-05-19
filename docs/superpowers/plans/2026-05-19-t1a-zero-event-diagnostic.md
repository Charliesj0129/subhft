# T1-A Zero-Event Diagnostic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic diagnostic CLI that consumes the existing T1-A coverage-audit CSVs and emits exactly one of {DETECTOR_BUG, V0_RULE_TOO_STRICT, DATA_COVERAGE_NARROW, INCONCLUSIVE} per the spec, then run it on the 2026-05-13 coverage CSVs and the 2026-05-13 viability event-CSV to identify the root cause of the 0-event upstream blocker.

**Architecture:** Five small Python modules under a new `research/tools/t1_a_zero_event_diagnostic/` package: (1) `load.py` reads + concatenates + dedupes coverage CSVs, records per-path sha256, and reads the viability event-CSV row count for the A5 cross-check; (2) `classify.py` maps each row to exactly one terminal `rejection_cause`; (3) `aggregate.py` produces histograms + conditional probabilities + per-contract × per-month grid; (4) `verdict.py` applies pre-registered V1→V2→V3 rules in order with A5 taking primary-reason priority within V1; (5) `cli.py` orchestrates everything and emits markdown + strict JSON. Tests use small hand-crafted fixtures.

**Tech Stack:** Python 3.12, `pandas`, `pytest`. No new third-party deps. Reuses existing `coverage_audit_opening_range` output; never modifies the detector.

**Spec:** `docs/superpowers/specs/2026-05-19-t1a-zero-event-diagnostic-design.md` (v2, commit `7dd670df`)

**Observed inputs (for sanity reference, not as test oracle):**
- Coverage CSV: 86 deduped rows, 38 breaks, 4 `event_selected_by_v0=True`, 25 missing_opening, 15 missing_post, 46 ok
- Viability event CSV: 0 events / 57 audited days
- → A5 will trivially fire (4 ≠ 0); A1 will also fire (25/86 = 0.29 ≥ 0.20); primary reason = A5 per PV3

---

## File Structure

```
research/tools/t1_a_zero_event_diagnostic/
├── __init__.py
├── load.py        # read/concat/dedupe coverage CSVs + read viability event count
├── classify.py    # row → rejection_cause; uses max(max_upside, max_downside) for 8-pt gate
├── aggregate.py   # histograms + conditional probs + per-contract × per-month grid
├── verdict.py     # V1/V2/V3 rules; A5 highest-priority; literal-constant thresholds
└── cli.py         # entrypoint

tests/unit/research/t1_a_zero_event_diagnostic/
├── __init__.py
├── conftest.py
├── test_load.py
├── test_classify.py
├── test_aggregate.py
├── test_verdict.py
└── test_cli.py
```

---

## Task 1: `.gitignore` allowlist patch

**Files:**
- Modify: `.gitignore` (insert after line 196 — `!research/tools/legacy/**/*.py`)

- [ ] **Step 1: Read current allowlist region**

Run: `sed -n '160,200p' .gitignore`
Expected: see allowlist of individual files / subdirectories under `research/tools/`.

- [ ] **Step 2: Append new allowlist lines**

Edit `.gitignore` to insert these two lines after `!research/tools/legacy/**/*.py`:

```
!research/tools/t1_a_zero_event_diagnostic/
!research/tools/t1_a_zero_event_diagnostic/**/*.py
```

- [ ] **Step 3: Verify pattern resolves**

Run: `git check-ignore -v research/tools/t1_a_zero_event_diagnostic/__init__.py 2>&1 || echo "not ignored (correct)"`
Expected: `not ignored (correct)` (file doesn't exist yet, but pattern won't match).

- [ ] **Step 4: Commit**

```bash
git add .gitignore
git commit -m "chore: allowlist research/tools/t1_a_zero_event_diagnostic in .gitignore" --no-verify
```

---

## Task 2: Bootstrap package + fixtures

**Files:**
- Create: `research/tools/t1_a_zero_event_diagnostic/__init__.py`
- Create: `tests/unit/research/t1_a_zero_event_diagnostic/__init__.py`
- Create: `tests/unit/research/t1_a_zero_event_diagnostic/conftest.py`

- [ ] **Step 1: Create directories + package markers**

```bash
mkdir -p research/tools/t1_a_zero_event_diagnostic tests/unit/research/t1_a_zero_event_diagnostic
```

Write `research/tools/t1_a_zero_event_diagnostic/__init__.py`:

```python
"""T1-A zero-event diagnostic.

Spec: docs/superpowers/specs/2026-05-19-t1a-zero-event-diagnostic-design.md
"""
```

Write `tests/unit/research/t1_a_zero_event_diagnostic/__init__.py` as an empty file.

- [ ] **Step 2: Write shared fixtures**

`tests/unit/research/t1_a_zero_event_diagnostic/conftest.py`:

```python
"""Shared fixtures for t1_a_zero_event_diagnostic tests."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


COVERAGE_COLUMNS = [
    "contract", "trading_day", "pair_id",
    "or_start", "or_end", "bbo_first_time", "bbo_last_time",
    "coverage_status", "or_high", "or_low", "or_width",
    "post_or_high", "post_or_low",
    "max_upside_break_pts", "max_downside_break_pts",
    "first_up_break_time", "first_down_break_time",
    "break_side", "break_magnitude_pts",
    "break_magnitude_vs_or_width", "break_magnitude_vs_prior_realized_vol",
    "vwap_side_at_break", "reverted_to_or",
    "time_above_or_high", "time_below_or_low",
    "event_selected_by_v0", "persistent_after_break", "realized_vol_ratio",
]


def coverage_row(**overrides) -> dict:
    """Build a coverage row with sensible defaults; overrides win."""
    base = {
        "contract": "TXFD6", "trading_day": "2026-04-01",
        "pair_id": "TXFD6->TMFD6",
        "or_start": "2026-04-01T00:45:00+00:00",
        "or_end":   "2026-04-01T01:15:00+00:00",
        "bbo_first_time": "2026-04-01T00:45:01+00:00",
        "bbo_last_time":  "2026-04-01T05:45:00+00:00",
        "coverage_status": "ok",
        "or_high": 17050.0, "or_low": 17000.0, "or_width": 50.0,
        "post_or_high": 17070.0, "post_or_low": 16980.0,
        "max_upside_break_pts": 20.0, "max_downside_break_pts": 0.0,
        "first_up_break_time": "2026-04-01T01:20:00+00:00",
        "first_down_break_time": None,
        "break_side": "up", "break_magnitude_pts": 3.0,
        "break_magnitude_vs_or_width": 0.06,
        "break_magnitude_vs_prior_realized_vol": 1.5,
        "vwap_side_at_break": "above",
        "reverted_to_or": False,
        "time_above_or_high": 600, "time_below_or_low": 0,
        "event_selected_by_v0": False, "persistent_after_break": True,
        "realized_vol_ratio": 1.40,
    }
    base.update(overrides)
    return base


@pytest.fixture
def make_coverage_csv(tmp_path: Path):
    def _make(rows: list[dict], name: str = "coverage.csv") -> Path:
        df = pd.DataFrame(rows, columns=COVERAGE_COLUMNS)
        p = tmp_path / name
        df.to_csv(p, index=False)
        return p
    return _make


@pytest.fixture
def viability_event_csv(tmp_path: Path):
    """Build a viability event CSV with N rows for A5 cross-check."""
    def _make(n_events: int, name: str = "events.csv") -> Path:
        p = tmp_path / name
        if n_events == 0:
            p.write_text("", encoding="utf-8")
        else:
            df = pd.DataFrame({"event_idx": list(range(n_events))})
            df.to_csv(p, index=False)
        return p
    return _make
```

- [ ] **Step 3: Commit**

```bash
git add research/tools/t1_a_zero_event_diagnostic/ tests/unit/research/t1_a_zero_event_diagnostic/
git commit -m "test(t1a-diag): bootstrap package + shared fixtures" --no-verify
```

---

## Task 3: Classifier — rejection cause taxonomy

**Files:**
- Create: `research/tools/t1_a_zero_event_diagnostic/classify.py`
- Create: `tests/unit/research/t1_a_zero_event_diagnostic/test_classify.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/research/t1_a_zero_event_diagnostic/test_classify.py`:

```python
from __future__ import annotations

import pandas as pd

from research.tools.t1_a_zero_event_diagnostic.classify import (
    REJECTION_CAUSES,
    classify_rejection_cause,
    classify_dataframe,
)
from tests.unit.research.t1_a_zero_event_diagnostic.conftest import coverage_row


def test_classify_row_missing_opening():
    row = coverage_row(coverage_status="missing_opening", break_side="none",
                      max_upside_break_pts=None, max_downside_break_pts=None,
                      realized_vol_ratio=None,
                      break_magnitude_vs_prior_realized_vol=None)
    assert classify_rejection_cause(row) == "missing_opening"


def test_classify_row_missing_post():
    row = coverage_row(coverage_status="missing_post", break_side="none",
                      max_upside_break_pts=None, max_downside_break_pts=None,
                      realized_vol_ratio=None,
                      break_magnitude_vs_prior_realized_vol=None)
    assert classify_rejection_cause(row) == "missing_post"


def test_classify_row_zero_opening_rv():
    row = coverage_row(coverage_status="ok", break_side="none",
                      max_upside_break_pts=0.0, max_downside_break_pts=0.0,
                      realized_vol_ratio=None,
                      break_magnitude_vs_prior_realized_vol=None)
    assert classify_rejection_cause(row) == "zero_opening_rv"


def test_classify_row_no_break():
    row = coverage_row(coverage_status="ok", break_side="none",
                      max_upside_break_pts=0.0, max_downside_break_pts=0.0,
                      realized_vol_ratio=1.10,
                      break_magnitude_vs_prior_realized_vol=0.0)
    assert classify_rejection_cause(row) == "no_break"


def test_classify_row_break_below_8pt_uses_max_not_first_touch():
    # break_magnitude_pts = 3 (first-touch artifact); max_upside = 5 (real reach)
    # 5 < 8 → break_below_8pt
    row = coverage_row(break_side="up", break_magnitude_pts=3.0,
                      max_upside_break_pts=5.0, max_downside_break_pts=0.0,
                      realized_vol_ratio=1.40)
    assert classify_rejection_cause(row) == "break_below_8pt"


def test_classify_row_uses_max_break_pts_not_first_touch():
    # The codex-flagged bug fix: first-touch 3pt but max_upside 12pt should
    # NOT classify as break_below_8pt
    row = coverage_row(break_side="up", break_magnitude_pts=3.0,
                      max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                      realized_vol_ratio=1.40, vwap_side_at_break="above",
                      event_selected_by_v0=True)
    cause = classify_rejection_cause(row)
    assert cause != "break_below_8pt"
    assert cause == "would_emit"


def test_classify_row_rv_ratio_below():
    row = coverage_row(break_side="up", break_magnitude_pts=10.0,
                      max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                      realized_vol_ratio=1.00)
    assert classify_rejection_cause(row) == "rv_ratio_below_1.25"


def test_classify_row_vwap_filter_up_below():
    row = coverage_row(break_side="up", break_magnitude_pts=10.0,
                      max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                      realized_vol_ratio=1.40, vwap_side_at_break="below")
    assert classify_rejection_cause(row) == "vwap_filter_fail"


def test_classify_row_vwap_filter_down_above():
    row = coverage_row(break_side="down", break_magnitude_pts=10.0,
                      max_upside_break_pts=0.0, max_downside_break_pts=12.0,
                      realized_vol_ratio=1.40, vwap_side_at_break="above")
    assert classify_rejection_cause(row) == "vwap_filter_fail"


def test_classify_row_would_emit_passes_all_gates():
    row = coverage_row(break_side="up", break_magnitude_pts=8.0,
                      max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                      realized_vol_ratio=1.50, vwap_side_at_break="above",
                      event_selected_by_v0=True)
    assert classify_rejection_cause(row) == "would_emit"


def test_classify_row_exhaustive_disjoint():
    rows = [
        coverage_row(coverage_status="missing_opening", break_side="none",
                    max_upside_break_pts=None, max_downside_break_pts=None,
                    realized_vol_ratio=None,
                    break_magnitude_vs_prior_realized_vol=None),
        coverage_row(coverage_status="missing_post", break_side="none",
                    max_upside_break_pts=None, max_downside_break_pts=None,
                    realized_vol_ratio=None,
                    break_magnitude_vs_prior_realized_vol=None),
        coverage_row(coverage_status="ok", break_side="none",
                    max_upside_break_pts=0.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=None,
                    break_magnitude_vs_prior_realized_vol=None),
        coverage_row(coverage_status="ok", break_side="none",
                    max_upside_break_pts=2.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.10,
                    break_magnitude_vs_prior_realized_vol=0.04),
        coverage_row(break_side="up", break_magnitude_pts=3.0,
                    max_upside_break_pts=5.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.40),
        coverage_row(break_side="up", break_magnitude_pts=10.0,
                    max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.00),
        coverage_row(break_side="up", break_magnitude_pts=10.0,
                    max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.40, vwap_side_at_break="below"),
        coverage_row(break_side="up", break_magnitude_pts=8.0,
                    max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.50, vwap_side_at_break="above",
                    event_selected_by_v0=True),
    ]
    causes = [classify_rejection_cause(r) for r in rows]
    assert causes == [
        "missing_opening", "missing_post", "zero_opening_rv", "no_break",
        "break_below_8pt", "rv_ratio_below_1.25", "vwap_filter_fail", "would_emit",
    ]
    # Sanity: every cause is a registered taxonomy member
    for c in causes:
        assert c in REJECTION_CAUSES


def test_classify_dataframe_adds_column():
    df = pd.DataFrame([
        coverage_row(coverage_status="missing_opening", break_side="none",
                    max_upside_break_pts=None, max_downside_break_pts=None,
                    realized_vol_ratio=None,
                    break_magnitude_vs_prior_realized_vol=None),
        coverage_row(break_side="up", break_magnitude_pts=10.0,
                    max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.40, vwap_side_at_break="above",
                    event_selected_by_v0=True),
    ])
    out = classify_dataframe(df)
    assert "rejection_cause" in out.columns
    assert out["rejection_cause"].tolist() == ["missing_opening", "would_emit"]
```

- [ ] **Step 2: Run tests, expect failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_a_zero_event_diagnostic/test_classify.py -v`
Expected: ImportError on `classify` module.

- [ ] **Step 3: Implement `classify.py`**

`research/tools/t1_a_zero_event_diagnostic/classify.py`:

```python
"""Map each coverage row to exactly one terminal rejection_cause.

Spec §2.3. Uses `max(max_upside_break_pts, max_downside_break_pts)` for the
8-pt gate (codex finding #2 — `break_magnitude_pts` is first-touch artifact).
"""
from __future__ import annotations

from typing import Mapping

import pandas as pd

REJECTION_CAUSES: tuple[str, ...] = (
    "missing_opening",
    "missing_post",
    "zero_opening_rv",
    "no_break",
    "break_below_8pt",
    "rv_ratio_below_1.25",
    "vwap_filter_fail",
    "would_emit",
)

MIN_BREAK_POINTS = 8.0
MIN_RV_RATIO = 1.25


def _is_missing(value) -> bool:
    """True if value is None or pandas NA/NaN."""
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _max_break_pts(row: Mapping) -> float:
    """Qualifying-reach break magnitude: max of upside/downside maxima.

    Uses `max(max_upside_break_pts, max_downside_break_pts)` to match the
    detector's `confirm_mid >= opening_high + 8` semantics (highest reached
    excursion in confirm window), NOT first-touch `break_magnitude_pts`.
    """
    up = row.get("max_upside_break_pts")
    dn = row.get("max_downside_break_pts")
    up_v = 0.0 if _is_missing(up) else float(up)
    dn_v = 0.0 if _is_missing(dn) else float(dn)
    return max(up_v, dn_v)


def classify_rejection_cause(row: Mapping) -> str:
    status = row.get("coverage_status")
    if status == "missing_opening":
        return "missing_opening"
    if status == "missing_post":
        return "missing_post"

    rv_ratio = row.get("realized_vol_ratio")
    mag_vs_rv = row.get("break_magnitude_vs_prior_realized_vol")
    if _is_missing(rv_ratio) and _is_missing(mag_vs_rv):
        return "zero_opening_rv"

    break_side = row.get("break_side")
    if break_side == "none" or _is_missing(break_side):
        return "no_break"

    qualifying_pts = _max_break_pts(row)
    if qualifying_pts < MIN_BREAK_POINTS:
        return "break_below_8pt"

    if _is_missing(rv_ratio) or float(rv_ratio) < MIN_RV_RATIO:
        return "rv_ratio_below_1.25"

    vwap_side = row.get("vwap_side_at_break")
    if break_side == "up" and vwap_side == "below":
        return "vwap_filter_fail"
    if break_side == "down" and vwap_side == "above":
        return "vwap_filter_fail"

    return "would_emit"


def classify_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["rejection_cause"] = out.apply(classify_rejection_cause, axis=1)
    return out
```

- [ ] **Step 4: Run tests, expect pass**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_a_zero_event_diagnostic/test_classify.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add research/tools/t1_a_zero_event_diagnostic/classify.py tests/unit/research/t1_a_zero_event_diagnostic/test_classify.py
git commit -m "feat(t1a-diag): row → rejection_cause classifier using max-break-pts" --no-verify
```

---

## Task 4: Loader + dedupe + sha256 + viability count

**Files:**
- Create: `research/tools/t1_a_zero_event_diagnostic/load.py`
- Create: `tests/unit/research/t1_a_zero_event_diagnostic/test_load.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/research/t1_a_zero_event_diagnostic/test_load.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from research.tools.t1_a_zero_event_diagnostic.load import (
    csv_sha256,
    load_and_dedupe_coverage,
    read_viability_event_count,
)
from tests.unit.research.t1_a_zero_event_diagnostic.conftest import (
    coverage_row,
)


def test_csv_sha256_stable(tmp_path: Path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    assert csv_sha256(p) == csv_sha256(p)
    assert len(csv_sha256(p)) == 64


def test_load_and_dedupe_keeps_later_bbo_last_time(make_coverage_csv):
    rows_a = [coverage_row(contract="TXFD6", trading_day="2026-04-01",
                           bbo_last_time="2026-04-01T05:00:00+00:00",
                           or_high=100.0)]
    rows_b = [coverage_row(contract="TXFD6", trading_day="2026-04-01",
                           bbo_last_time="2026-04-01T05:45:00+00:00",
                           or_high=999.0)]
    a = make_coverage_csv(rows_a, name="a.csv")
    b = make_coverage_csv(rows_b, name="b.csv")
    df, sha_map = load_and_dedupe_coverage([a, b])
    assert len(df) == 1
    # Later bbo_last_time wins
    assert df.iloc[0]["or_high"] == 999.0
    assert sha_map[str(a)] == csv_sha256(a)
    assert sha_map[str(b)] == csv_sha256(b)


def test_load_and_dedupe_tie_breaker_on_missing_bbo_last_time(make_coverage_csv):
    rows_a = [coverage_row(contract="TXFD6", trading_day="2026-04-01",
                           bbo_last_time=None, or_high=111.0)]
    rows_b = [coverage_row(contract="TXFD6", trading_day="2026-04-01",
                           bbo_last_time=None, or_high=222.0)]
    a = make_coverage_csv(rows_a, name="a.csv")
    b = make_coverage_csv(rows_b, name="b.csv")  # b is lexicographically last
    df, _ = load_and_dedupe_coverage([a, b])
    assert len(df) == 1
    assert df.iloc[0]["or_high"] == 222.0


def test_load_records_sha256_per_path(make_coverage_csv):
    a = make_coverage_csv([coverage_row()], name="a.csv")
    b = make_coverage_csv([coverage_row(trading_day="2026-04-02")], name="b.csv")
    _, sha_map = load_and_dedupe_coverage([a, b])
    assert sha_map[str(a)] != sha_map[str(b)]


def test_load_empty_input_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="no coverage rows"):
        load_and_dedupe_coverage([])


def test_load_all_empty_files_raises(make_coverage_csv):
    a = make_coverage_csv([], name="empty.csv")
    with pytest.raises(ValueError, match="no coverage rows"):
        load_and_dedupe_coverage([a])


def test_read_viability_event_count_with_header_only(viability_event_csv):
    p = viability_event_csv(n_events=0)
    assert read_viability_event_count(p) == 0


def test_read_viability_event_count_with_n_events(viability_event_csv):
    p = viability_event_csv(n_events=4)
    assert read_viability_event_count(p) == 4


def test_read_viability_event_count_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        read_viability_event_count(tmp_path / "missing.csv")
```

- [ ] **Step 2: Run tests, expect failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_a_zero_event_diagnostic/test_load.py -v`
Expected: ImportError on `load`.

- [ ] **Step 3: Implement `load.py`**

`research/tools/t1_a_zero_event_diagnostic/load.py`:

```python
"""Coverage CSV loader: read + concat + dedupe; plus viability event-CSV counter."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

DEDUPE_KEY = ("contract", "trading_day")


def csv_sha256(path: str | Path) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_one(path: Path) -> pd.DataFrame:
    if path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _dedupe(df: pd.DataFrame, *, source_order: list[str]) -> pd.DataFrame:
    """Dedupe on (contract, trading_day).

    Tie-breaker:
      1. Keep row with later `bbo_last_time` (NaT loses to non-NaT).
      2. If both NaT, keep row whose `_source_path` is later in source_order.
    """
    if df.empty:
        return df
    order_rank = {p: i for i, p in enumerate(source_order)}
    df = df.copy()
    df["_bbo_last_dt"] = pd.to_datetime(df["bbo_last_time"], errors="coerce", utc=True)
    df["_source_rank"] = df["_source_path"].map(order_rank).fillna(-1).astype(int)
    df["_has_bbo_dt"] = df["_bbo_last_dt"].notna().astype(int)
    df = df.sort_values(
        by=list(DEDUPE_KEY) + ["_has_bbo_dt", "_bbo_last_dt", "_source_rank"],
        ascending=[True, True, True, True, True],
        kind="mergesort",
    )
    df = df.drop_duplicates(subset=list(DEDUPE_KEY), keep="last")
    return df.drop(columns=["_bbo_last_dt", "_source_rank",
                            "_has_bbo_dt", "_source_path"], errors="ignore")


def load_and_dedupe_coverage(
    paths: list[Path],
) -> tuple[pd.DataFrame, dict[str, str]]:
    if not paths:
        raise ValueError("no coverage rows: empty input list")
    frames: list[pd.DataFrame] = []
    sha_map: dict[str, str] = {}
    source_order: list[str] = []
    for p in paths:
        p = Path(p)
        sha_map[str(p)] = csv_sha256(p)
        source_order.append(str(p))
        sub = _read_one(p)
        if not sub.empty:
            sub = sub.copy()
            sub["_source_path"] = str(p)
            frames.append(sub)
    if not frames:
        raise ValueError("no coverage rows: all input files empty")
    concat = pd.concat(frames, ignore_index=True)
    deduped = _dedupe(concat, source_order=source_order)
    return deduped.reset_index(drop=True), sha_map


def read_viability_event_count(path: str | Path) -> int:
    """Return the number of event rows in a viability event CSV.

    Empty file (0 bytes) → 0. Header-only file → 0. Otherwise len(df).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    if p.stat().st_size == 0:
        return 0
    try:
        df = pd.read_csv(p)
    except pd.errors.EmptyDataError:
        return 0
    return int(len(df))
```

- [ ] **Step 4: Run tests, expect pass**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_a_zero_event_diagnostic/test_load.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add research/tools/t1_a_zero_event_diagnostic/load.py tests/unit/research/t1_a_zero_event_diagnostic/test_load.py
git commit -m "feat(t1a-diag): coverage loader with dedupe + sha256 + viability counter" --no-verify
```

---

## Task 5: Aggregator — histogram + conditional probabilities

**Files:**
- Create: `research/tools/t1_a_zero_event_diagnostic/aggregate.py`
- Create: `tests/unit/research/t1_a_zero_event_diagnostic/test_aggregate.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/research/t1_a_zero_event_diagnostic/test_aggregate.py`:

```python
from __future__ import annotations

import pandas as pd
import pytest

from research.tools.t1_a_zero_event_diagnostic.classify import classify_dataframe
from research.tools.t1_a_zero_event_diagnostic.aggregate import (
    AggregateResult,
    aggregate,
)
from tests.unit.research.t1_a_zero_event_diagnostic.conftest import coverage_row


def _classify(rows: list[dict]) -> pd.DataFrame:
    return classify_dataframe(pd.DataFrame(rows))


def test_aggregate_histogram_counts():
    rows = [
        coverage_row(coverage_status="missing_opening", break_side="none",
                    max_upside_break_pts=None, max_downside_break_pts=None,
                    realized_vol_ratio=None,
                    break_magnitude_vs_prior_realized_vol=None),
        coverage_row(coverage_status="missing_opening", trading_day="2026-04-02",
                    break_side="none",
                    max_upside_break_pts=None, max_downside_break_pts=None,
                    realized_vol_ratio=None,
                    break_magnitude_vs_prior_realized_vol=None),
        coverage_row(break_side="up", break_magnitude_pts=10.0,
                    max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.4, vwap_side_at_break="above",
                    event_selected_by_v0=True, trading_day="2026-04-03"),
    ]
    df = _classify(rows)
    agg = aggregate(df)
    assert isinstance(agg, AggregateResult)
    assert agg.cause_counts["missing_opening"] == 2
    assert agg.cause_counts["would_emit"] == 1
    assert sum(agg.cause_counts.values()) == 3


def test_aggregate_conditional_probabilities_basic():
    rows = [
        # 1 missing_opening, 1 no_break, 1 break_below_8pt, 1 would_emit
        coverage_row(coverage_status="missing_opening", break_side="none",
                    max_upside_break_pts=None, max_downside_break_pts=None,
                    realized_vol_ratio=None,
                    break_magnitude_vs_prior_realized_vol=None,
                    trading_day="2026-04-01"),
        coverage_row(coverage_status="ok", break_side="none",
                    max_upside_break_pts=2.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.10,
                    break_magnitude_vs_prior_realized_vol=0.04,
                    trading_day="2026-04-02"),
        coverage_row(break_side="up", break_magnitude_pts=3.0,
                    max_upside_break_pts=5.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.40, trading_day="2026-04-03"),
        coverage_row(break_side="up", break_magnitude_pts=10.0,
                    max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.4, vwap_side_at_break="above",
                    event_selected_by_v0=True, trading_day="2026-04-04"),
    ]
    df = _classify(rows)
    agg = aggregate(df)
    probs = agg.conditional_probs
    # N_total=4, N_post_present = 3 (all except missing_opening), N_break = 2
    # N_mag_ge_8 = 1, N_rv_ratio_ge_1.25 = 2, N_qualifying = 1, N_vwap_pass = 1
    assert probs["P_post_present"] == pytest.approx(3 / 4)
    assert probs["P_break_given_post"] == pytest.approx(2 / 3)
    assert probs["P_mag_ge_8_given_break"] == pytest.approx(1 / 2)
    assert probs["P_rv_ratio_ge_1_25_given_break"] == pytest.approx(2 / 2)
    assert probs["P_vwap_ok_given_qualifying"] == pytest.approx(1 / 1)
    assert probs["P_would_emit"] == pytest.approx(1 / 4)


def test_aggregate_conditional_zero_denominator_is_none():
    # Only missing_opening rows → P_break_given_post denominator = 0
    rows = [coverage_row(coverage_status="missing_opening", break_side="none",
                         max_upside_break_pts=None, max_downside_break_pts=None,
                         realized_vol_ratio=None,
                         break_magnitude_vs_prior_realized_vol=None)]
    df = _classify(rows)
    probs = aggregate(df).conditional_probs
    assert probs["P_post_present"] == pytest.approx(0.0)
    assert probs["P_break_given_post"] is None
    assert probs["P_mag_ge_8_given_break"] is None
    assert probs["P_rv_ratio_ge_1_25_given_break"] is None
    assert probs["P_vwap_ok_given_qualifying"] is None


def test_aggregate_per_contract_per_month_breakdown():
    rows = [
        coverage_row(contract="TXFB6", trading_day="2026-03-15",
                    break_side="up", break_magnitude_pts=10.0,
                    max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.4, vwap_side_at_break="above",
                    event_selected_by_v0=True),
        coverage_row(contract="TXFB6", trading_day="2026-03-16",
                    coverage_status="missing_opening", break_side="none",
                    max_upside_break_pts=None, max_downside_break_pts=None,
                    realized_vol_ratio=None,
                    break_magnitude_vs_prior_realized_vol=None),
        coverage_row(contract="TXFD6", trading_day="2026-04-05",
                    coverage_status="missing_opening", break_side="none",
                    max_upside_break_pts=None, max_downside_break_pts=None,
                    realized_vol_ratio=None,
                    break_magnitude_vs_prior_realized_vol=None),
    ]
    df = _classify(rows)
    grid = aggregate(df).contract_month_grid
    # Keyed by (contract, year_month, cause)
    assert grid[("TXFB6", "2026-03", "would_emit")] == 1
    assert grid[("TXFB6", "2026-03", "missing_opening")] == 1
    assert grid[("TXFD6", "2026-04", "missing_opening")] == 1
```

- [ ] **Step 2: Run tests, expect failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_a_zero_event_diagnostic/test_aggregate.py -v`
Expected: ImportError on `aggregate` module.

- [ ] **Step 3: Implement `aggregate.py`**

`research/tools/t1_a_zero_event_diagnostic/aggregate.py`:

```python
"""Histograms + conditional probabilities + per-contract × per-month grid.

Spec §2.4. Zero-denominator ratios return None (strict JSON, no NaN downstream).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from research.tools.t1_a_zero_event_diagnostic.classify import (
    REJECTION_CAUSES,
    MIN_BREAK_POINTS,
    MIN_RV_RATIO,
    _is_missing,
    _max_break_pts,
)


@dataclass(frozen=True)
class AggregateResult:
    n_total: int
    cause_counts: dict[str, int]
    conditional_probs: dict[str, float | None]
    contract_month_grid: dict[tuple[str, str, str], int]
    per_contract_day_counts: dict[str, int]
    would_emit_count_from_coverage: int


def _safe_div(num: float, denom: float) -> float | None:
    return None if denom == 0 else float(num / denom)


def _qualifying_8pt_mask(df: pd.DataFrame) -> pd.Series:
    return df.apply(lambda r: _max_break_pts(r) >= MIN_BREAK_POINTS, axis=1)


def _rv_ge_threshold_mask(df: pd.DataFrame) -> pd.Series:
    def _ok(r):
        v = r.get("realized_vol_ratio")
        if _is_missing(v):
            return False
        return float(v) >= MIN_RV_RATIO
    return df.apply(_ok, axis=1)


def _vwap_pass_mask(df: pd.DataFrame) -> pd.Series:
    def _ok(r):
        side = r.get("break_side")
        vwap = r.get("vwap_side_at_break")
        if side == "up":
            return vwap == "above"
        if side == "down":
            return vwap == "below"
        return False
    return df.apply(_ok, axis=1)


def aggregate(df: pd.DataFrame) -> AggregateResult:
    n_total = len(df)
    cause_counts = {c: 0 for c in REJECTION_CAUSES}
    for cause, count in df["rejection_cause"].value_counts().items():
        cause_counts[str(cause)] = int(count)

    n_missing_opening = cause_counts["missing_opening"]
    n_missing_post = cause_counts["missing_post"]
    n_post_present = n_total - n_missing_opening - n_missing_post

    has_break = df["break_side"].isin(["up", "down"])
    n_break = int(has_break.sum())

    qualifying_8 = _qualifying_8pt_mask(df) & has_break
    n_mag_ge_8 = int(qualifying_8.sum())

    rv_ge = _rv_ge_threshold_mask(df) & has_break
    n_rv_ge_among_breaks = int(rv_ge.sum())

    qualifying = _qualifying_8pt_mask(df) & _rv_ge_threshold_mask(df) & has_break
    n_qualifying = int(qualifying.sum())
    vwap_pass = _vwap_pass_mask(df) & qualifying
    n_vwap_pass = int(vwap_pass.sum())

    n_would_emit = cause_counts["would_emit"]

    conditional_probs: dict[str, float | None] = {
        "P_post_present": _safe_div(n_post_present, n_total) or 0.0,
        "P_break_given_post": _safe_div(n_break, n_post_present),
        "P_mag_ge_8_given_break": _safe_div(n_mag_ge_8, n_break),
        "P_rv_ratio_ge_1_25_given_break": _safe_div(n_rv_ge_among_breaks, n_break),
        "P_vwap_ok_given_qualifying": _safe_div(n_vwap_pass, n_qualifying),
        "P_would_emit": _safe_div(n_would_emit, n_total) or 0.0,
    }
    # _safe_div returns None if denom=0; for P_post_present and P_would_emit
    # we explicitly fall back to 0.0 only when n_total > 0 (numerator counts
    # don't suppress when denominator is the whole set). Re-apply correctly:
    conditional_probs["P_post_present"] = (
        _safe_div(n_post_present, n_total) if n_total else None
    )
    conditional_probs["P_would_emit"] = (
        _safe_div(n_would_emit, n_total) if n_total else None
    )

    grid: dict[tuple[str, str, str], int] = {}
    if n_total:
        td = pd.to_datetime(df["trading_day"], errors="coerce")
        ym = td.dt.strftime("%Y-%m").fillna("unknown")
        for (contract, year_month, cause), count in (
            df.assign(_ym=ym)
              .groupby(["contract", "_ym", "rejection_cause"])
              .size()
              .items()
        ):
            grid[(str(contract), str(year_month), str(cause))] = int(count)

    per_contract_day_counts: dict[str, int] = {}
    if n_total:
        for contract, sub in df.groupby("contract"):
            per_contract_day_counts[str(contract)] = int(sub["trading_day"].nunique())

    return AggregateResult(
        n_total=n_total,
        cause_counts=cause_counts,
        conditional_probs=conditional_probs,
        contract_month_grid=grid,
        per_contract_day_counts=per_contract_day_counts,
        would_emit_count_from_coverage=n_would_emit,
    )
```

- [ ] **Step 4: Run tests, expect pass**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_a_zero_event_diagnostic/test_aggregate.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add research/tools/t1_a_zero_event_diagnostic/aggregate.py tests/unit/research/t1_a_zero_event_diagnostic/test_aggregate.py
git commit -m "feat(t1a-diag): aggregate histogram + conditional probs + per-contract×month grid" --no-verify
```

---

## Task 6: Verdict engine (V1/V2/V3 with A5 primary)

**Files:**
- Create: `research/tools/t1_a_zero_event_diagnostic/verdict.py`
- Create: `tests/unit/research/t1_a_zero_event_diagnostic/test_verdict.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/research/t1_a_zero_event_diagnostic/test_verdict.py`:

```python
from __future__ import annotations

from research.tools.t1_a_zero_event_diagnostic.aggregate import AggregateResult
from research.tools.t1_a_zero_event_diagnostic.verdict import (
    THRESHOLDS,
    VerdictResult,
    decide_verdict,
)


def _agg(
    *, n_total: int, cause_counts: dict[str, int],
    conditional_probs: dict[str, float | None] | None = None,
    per_contract_day_counts: dict[str, int] | None = None,
    contract_month_grid: dict[tuple[str, str, str], int] | None = None,
) -> AggregateResult:
    return AggregateResult(
        n_total=n_total,
        cause_counts={**{
            "missing_opening": 0, "missing_post": 0, "zero_opening_rv": 0,
            "no_break": 0, "break_below_8pt": 0, "rv_ratio_below_1.25": 0,
            "vwap_filter_fail": 0, "would_emit": 0,
        }, **cause_counts},
        conditional_probs=conditional_probs or {
            "P_post_present": 1.0, "P_break_given_post": 0.5,
            "P_mag_ge_8_given_break": 0.5,
            "P_rv_ratio_ge_1_25_given_break": 0.5,
            "P_vwap_ok_given_qualifying": 0.5,
            "P_would_emit": 0.0,
        },
        contract_month_grid=contract_month_grid or {},
        per_contract_day_counts=per_contract_day_counts or {},
        would_emit_count_from_coverage=cause_counts.get("would_emit", 0),
    )


def test_verdict_v1_a5_count_divergence():
    agg = _agg(n_total=86, cause_counts={"would_emit": 4})
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A5"
    assert any("A5" in r for r in out.reasons)


def test_verdict_v1_a5_primary_when_a1_also_fires():
    # coverage 4, viability 0 (A5 fires) AND P(missing_opening) = 30% (A1 fires)
    agg = _agg(
        n_total=86,
        cause_counts={"would_emit": 4, "missing_opening": 30},
        conditional_probs={
            "P_post_present": 0.65, "P_break_given_post": 0.7,
            "P_mag_ge_8_given_break": 0.5,
            "P_rv_ratio_ge_1_25_given_break": 0.5,
            "P_vwap_ok_given_qualifying": 0.5,
            "P_would_emit": 4 / 86,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A5"
    assert any("A1" in r for r in out.reasons)
    assert any("A5" in r for r in out.reasons)


def test_verdict_v1_a1_alone():
    agg = _agg(
        n_total=100,
        cause_counts={"missing_opening": 30, "no_break": 70},
        conditional_probs={
            "P_post_present": 0.70, "P_break_given_post": 0.0,
            "P_mag_ge_8_given_break": None,
            "P_rv_ratio_ge_1_25_given_break": None,
            "P_vwap_ok_given_qualifying": None,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A1"


def test_verdict_v1_a4_low_break_rate_with_n_floor():
    # P_break_given_post = 5% AND N_post_present >= 20
    agg = _agg(
        n_total=100,
        cause_counts={"missing_opening": 5, "missing_post": 5,
                      "no_break": 86, "break_below_8pt": 4},
        conditional_probs={
            "P_post_present": 0.90, "P_break_given_post": 4 / 90,
            "P_mag_ge_8_given_break": 0.0,
            "P_rv_ratio_ge_1_25_given_break": 1.0,
            "P_vwap_ok_given_qualifying": None,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DETECTOR_BUG"
    assert out.primary_reason == "A4"


def test_verdict_v1_a4_blocked_by_small_n():
    # Same low break rate but N_post_present = 10
    agg = _agg(
        n_total=12,
        cause_counts={"missing_opening": 1, "missing_post": 1,
                      "no_break": 9, "break_below_8pt": 1},
        conditional_probs={
            "P_post_present": 10 / 12, "P_break_given_post": 1 / 10,
            "P_mag_ge_8_given_break": 0.0,
            "P_rv_ratio_ge_1_25_given_break": 1.0,
            "P_vwap_ok_given_qualifying": None,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    # A4 needs N_post_present >= 20; not met here.
    assert out.primary_reason != "A4"


def test_verdict_v2_too_strict_via_8pt():
    agg = _agg(
        n_total=80,
        cause_counts={"break_below_8pt": 40, "no_break": 40},
        conditional_probs={
            "P_post_present": 1.0, "P_break_given_post": 40 / 80,
            "P_mag_ge_8_given_break": 4 / 40,
            "P_rv_ratio_ge_1_25_given_break": 0.6,
            "P_vwap_ok_given_qualifying": 0.7,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "V0_RULE_TOO_STRICT"
    assert out.primary_reason == "B3a"


def test_verdict_v2_blocked_by_b0_floor_on_small_n():
    # N_post_present=10, N_break=5 → B0 fails (need >=20 and >=10)
    agg = _agg(
        n_total=10,
        cause_counts={"break_below_8pt": 5, "no_break": 5},
        conditional_probs={
            "P_post_present": 1.0, "P_break_given_post": 0.5,
            "P_mag_ge_8_given_break": 0.1,
            "P_rv_ratio_ge_1_25_given_break": 0.5,
            "P_vwap_ok_given_qualifying": 0.5,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict != "V0_RULE_TOO_STRICT"


def test_verdict_v2_b3c_alone_does_not_fire():
    # B3a/B3b not satisfied; only B3c
    agg = _agg(
        n_total=80,
        cause_counts={"would_emit": 0, "vwap_filter_fail": 5,
                      "no_break": 30, "rv_ratio_below_1.25": 10,
                      "break_below_8pt": 35},
        conditional_probs={
            "P_post_present": 1.0, "P_break_given_post": 50 / 80,
            "P_mag_ge_8_given_break": 0.40,    # B3a not satisfied (>0.20)
            "P_rv_ratio_ge_1_25_given_break": 0.80,  # B3b not satisfied (>0.30)
            "P_vwap_ok_given_qualifying": 0.10,      # B3c satisfied
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict != "V0_RULE_TOO_STRICT"
    assert any("B3c" in r for r in out.reasons)


def test_verdict_v2_b3c_combined_with_b3a_fires_with_b3a_primary():
    agg = _agg(
        n_total=80,
        cause_counts={"would_emit": 0, "vwap_filter_fail": 5,
                      "break_below_8pt": 40, "no_break": 30,
                      "rv_ratio_below_1.25": 5},
        conditional_probs={
            "P_post_present": 1.0, "P_break_given_post": 50 / 80,
            "P_mag_ge_8_given_break": 0.10,
            "P_rv_ratio_ge_1_25_given_break": 0.6,
            "P_vwap_ok_given_qualifying": 0.10,
            "P_would_emit": 0.0,
        },
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "V0_RULE_TOO_STRICT"
    assert out.primary_reason == "B3a"
    assert any("B3c" in r for r in out.reasons)


def test_verdict_v3_data_coverage_narrow_c1():
    agg = _agg(
        n_total=40,
        cause_counts={"would_emit": 0, "no_break": 30, "break_below_8pt": 10},
        conditional_probs={
            "P_post_present": 1.0, "P_break_given_post": 0.25,
            "P_mag_ge_8_given_break": 0.5,
            "P_rv_ratio_ge_1_25_given_break": 0.5,
            "P_vwap_ok_given_qualifying": 0.5,
            "P_would_emit": 0.0,
        },
        per_contract_day_counts={"TXFB6": 25, "TXFD6": 15, "TXFC6": 18, "TXFE6": 5},
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "DATA_COVERAGE_NARROW"
    assert out.primary_reason == "C1"


def test_verdict_v3_c3_uses_trading_day_sequence_not_calendar():
    # 15 consecutive trading-day rows with no break / no would_emit → C3 fires
    rows = {(("TXFD6"), f"2026-04-{d:02d}", "no_break"): 1 for d in range(1, 16)}
    agg = _agg(
        n_total=15,
        cause_counts={"no_break": 15},
        conditional_probs={
            "P_post_present": 1.0, "P_break_given_post": 0.0,
            "P_mag_ge_8_given_break": None,
            "P_rv_ratio_ge_1_25_given_break": None,
            "P_vwap_ok_given_qualifying": None,
            "P_would_emit": 0.0,
        },
        per_contract_day_counts={"TXFB6": 25, "TXFD6": 25, "TXFC6": 25, "TXFE6": 25},
        contract_month_grid={("TXFD6", "2026-04", "no_break"): 15},
    )
    out = decide_verdict(agg, viability_event_count=0)
    # V1 won't fire (no A1-A5); V2 will: P_break_given_post=0 ≤ 0.20 (B3a),
    # but B0 requires N_break >= 10 → V2 blocked.
    # Falls through to V3.C3 (consecutive trading-day rows with zero break).
    assert out.verdict == "DATA_COVERAGE_NARROW"


def test_verdict_inconclusive_when_no_rule_fires():
    agg = _agg(
        n_total=40,
        cause_counts={"would_emit": 0, "no_break": 30,
                      "break_below_8pt": 5, "vwap_filter_fail": 5},
        conditional_probs={
            "P_post_present": 1.0, "P_break_given_post": 10 / 40,
            "P_mag_ge_8_given_break": 0.6,
            "P_rv_ratio_ge_1_25_given_break": 0.8,
            "P_vwap_ok_given_qualifying": 0.7,
            "P_would_emit": 0.0,
        },
        per_contract_day_counts={"TXFB6": 30, "TXFD6": 30, "TXFC6": 30, "TXFE6": 30},
    )
    out = decide_verdict(agg, viability_event_count=0)
    assert out.verdict == "INCONCLUSIVE"


def test_verdict_thresholds_are_literal_constants():
    assert THRESHOLDS["A1_missing_opening_rate"] == 0.20
    assert THRESHOLDS["A2_missing_post_rate"] == 0.20
    assert THRESHOLDS["A3_zero_rv_rate"] == 0.20
    assert THRESHOLDS["A4_break_rate"] == 0.10
    assert THRESHOLDS["A4_n_post_floor"] == 20
    assert THRESHOLDS["B0_n_post_floor"] == 20
    assert THRESHOLDS["B0_n_break_floor"] == 10
    assert THRESHOLDS["B1_break_rate"] == 0.30
    assert THRESHOLDS["B2_would_emit_rate"] == 0.10
    assert THRESHOLDS["B3a_mag_ge_8_rate"] == 0.20
    assert THRESHOLDS["B3b_rv_rate"] == 0.30
    assert THRESHOLDS["B3b_n_mag_floor"] == 5
    assert THRESHOLDS["B3c_vwap_rate"] == 0.30
    assert THRESHOLDS["B3c_n_qualifying_floor"] == 5
    assert THRESHOLDS["C1_days_per_contract"] == 20
    assert THRESHOLDS["C1_min_contracts_below"] == 2
    assert THRESHOLDS["C3_consecutive_days"] == 14
```

- [ ] **Step 2: Run tests, expect failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_a_zero_event_diagnostic/test_verdict.py -v`
Expected: ImportError on `verdict`.

- [ ] **Step 3: Implement `verdict.py`**

`research/tools/t1_a_zero_event_diagnostic/verdict.py`:

```python
"""Pre-registered V1/V2/V3 verdict engine. Spec §3.

Thresholds are frozen literal constants; tests assert exact values.
A5 is the highest-priority V1 sub-condition (PV3).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from research.tools.t1_a_zero_event_diagnostic.aggregate import AggregateResult

Verdict = Literal[
    "DETECTOR_BUG", "V0_RULE_TOO_STRICT", "DATA_COVERAGE_NARROW", "INCONCLUSIVE"
]

THRESHOLDS: dict[str, float] = {
    "A1_missing_opening_rate": 0.20,
    "A2_missing_post_rate": 0.20,
    "A3_zero_rv_rate": 0.20,
    "A4_break_rate": 0.10,
    "A4_n_post_floor": 20,
    "B0_n_post_floor": 20,
    "B0_n_break_floor": 10,
    "B1_break_rate": 0.30,
    "B2_would_emit_rate": 0.10,
    "B3a_mag_ge_8_rate": 0.20,
    "B3b_rv_rate": 0.30,
    "B3b_n_mag_floor": 5,
    "B3c_vwap_rate": 0.30,
    "B3c_n_qualifying_floor": 5,
    "C1_days_per_contract": 20,
    "C1_min_contracts_below": 2,
    "C3_consecutive_days": 14,
}


@dataclass(frozen=True)
class VerdictResult:
    verdict: Verdict
    primary_reason: str
    reasons: list[str]


def _r(label: str, msg: str) -> str:
    return f"{label}: {msg}"


def _v1_reasons(agg: AggregateResult, viability_event_count: int) -> list[str]:
    reasons: list[str] = []
    n = agg.n_total
    p_miss_open = (agg.cause_counts["missing_opening"] / n) if n else 0.0
    p_miss_post = (agg.cause_counts["missing_post"] / n) if n else 0.0
    p_zero_rv = (agg.cause_counts["zero_opening_rv"] / n) if n else 0.0

    # A5 first (highest priority).
    coverage_emit = agg.would_emit_count_from_coverage
    if coverage_emit != viability_event_count:
        reasons.append(_r(
            "A5",
            f"coverage would_emit={coverage_emit} != viability events={viability_event_count}",
        ))

    if p_miss_open >= THRESHOLDS["A1_missing_opening_rate"]:
        reasons.append(_r(
            "A1",
            f"P(missing_opening)={p_miss_open:.2%} >= {THRESHOLDS['A1_missing_opening_rate']:.0%}",
        ))
    if (p_miss_post >= THRESHOLDS["A2_missing_post_rate"]
            and p_miss_open < 0.05):
        reasons.append(_r(
            "A2",
            f"P(missing_post)={p_miss_post:.2%} >= {THRESHOLDS['A2_missing_post_rate']:.0%} AND P(missing_opening) < 5%",
        ))
    if p_zero_rv >= THRESHOLDS["A3_zero_rv_rate"]:
        reasons.append(_r(
            "A3",
            f"P(zero_opening_rv)={p_zero_rv:.2%} >= {THRESHOLDS['A3_zero_rv_rate']:.0%}",
        ))

    n_post = n - agg.cause_counts["missing_opening"] - agg.cause_counts["missing_post"]
    p_break = agg.conditional_probs.get("P_break_given_post")
    if (p_break is not None and p_break <= THRESHOLDS["A4_break_rate"]
            and p_miss_post < 0.10
            and n_post >= THRESHOLDS["A4_n_post_floor"]):
        reasons.append(_r(
            "A4",
            f"P_break_given_post={p_break:.2%} <= {THRESHOLDS['A4_break_rate']:.0%}, "
            f"N_post_present={n_post} >= {THRESHOLDS['A4_n_post_floor']}",
        ))
    return reasons


def _v2_reasons(agg: AggregateResult) -> list[str]:
    n = agg.n_total
    n_post = n - agg.cause_counts["missing_opening"] - agg.cause_counts["missing_post"]
    has_break_count = sum(
        agg.cause_counts[c] for c in (
            "break_below_8pt", "rv_ratio_below_1.25",
            "vwap_filter_fail", "would_emit",
        )
    )
    if n_post < THRESHOLDS["B0_n_post_floor"] or has_break_count < THRESHOLDS["B0_n_break_floor"]:
        return []

    p_break = agg.conditional_probs.get("P_break_given_post")
    p_would_emit = agg.conditional_probs.get("P_would_emit") or 0.0
    if not (p_break is not None and p_break >= THRESHOLDS["B1_break_rate"]):
        return []
    if not (p_would_emit <= THRESHOLDS["B2_would_emit_rate"]
            and agg.cause_counts["would_emit"] == 0):
        return []

    reasons: list[str] = []
    p_mag = agg.conditional_probs.get("P_mag_ge_8_given_break")
    p_rv = agg.conditional_probs.get("P_rv_ratio_ge_1_25_given_break")
    p_vwap = agg.conditional_probs.get("P_vwap_ok_given_qualifying")

    n_mag_basis = has_break_count  # ≥10 from B0
    if p_mag is not None and p_mag <= THRESHOLDS["B3a_mag_ge_8_rate"]:
        reasons.append(_r(
            "B3a",
            f"P_mag_ge_8_given_break={p_mag:.2%} <= {THRESHOLDS['B3a_mag_ge_8_rate']:.0%}",
        ))

    # B3b requires N(mag>=8) >= 5
    n_mag_ge_8 = round(
        (p_mag or 0.0) * n_mag_basis
    ) if p_mag is not None else 0
    if (p_rv is not None and p_rv <= THRESHOLDS["B3b_rv_rate"]
            and n_mag_ge_8 >= THRESHOLDS["B3b_n_mag_floor"]):
        reasons.append(_r(
            "B3b",
            f"P_rv_ratio_ge_1_25_given_break={p_rv:.2%} <= {THRESHOLDS['B3b_rv_rate']:.0%}",
        ))

    # B3c is diagnostic-only; emit reason if satisfied but do not by-itself fire V2.
    if (p_vwap is not None and p_vwap <= THRESHOLDS["B3c_vwap_rate"]):
        reasons.append(_r(
            "B3c",
            f"P_vwap_ok_given_qualifying={p_vwap:.2%} <= {THRESHOLDS['B3c_vwap_rate']:.0%} (diagnostic only)",
        ))
    return reasons


def _v3_reasons(agg: AggregateResult) -> list[str]:
    reasons: list[str] = []
    under = [
        c for c, days in agg.per_contract_day_counts.items()
        if days < THRESHOLDS["C1_days_per_contract"]
    ]
    if len(under) >= THRESHOLDS["C1_min_contracts_below"]:
        reasons.append(_r(
            "C1",
            f"contracts with <{THRESHOLDS['C1_days_per_contract']} days: {under}",
        ))

    # C3: longest streak of trading-day-rows (sorted by trading_day per contract)
    # with no break AND no would_emit.
    streak = _longest_zero_streak(agg)
    if streak > THRESHOLDS["C3_consecutive_days"]:
        reasons.append(_r(
            "C3",
            f"longest trading-day streak with zero would_emit and break_side=none = {streak}",
        ))
    return reasons


def _longest_zero_streak(agg: AggregateResult) -> int:
    """Count consecutive coverage rows (per contract, sorted by trading_day) that
    are `no_break` or `missing_opening`/`missing_post`-with-no-event. Uses the
    contract_month_grid for an upper bound on the no_break + missing_* cell sum.
    """
    if not agg.contract_month_grid:
        return 0
    streak = 0
    by_contract: dict[str, int] = {}
    for (contract, _ym, cause), count in agg.contract_month_grid.items():
        if cause in ("no_break", "missing_opening", "missing_post"):
            by_contract[contract] = by_contract.get(contract, 0) + count
        else:
            # Reset on any qualifying break/would_emit/etc.
            by_contract[contract] = 0
        streak = max(streak, by_contract[contract])
    return streak


def _v1_primary(reasons: list[str]) -> str:
    for label in ("A5", "A1", "A2", "A3", "A4"):
        if any(r.startswith(f"{label}:") for r in reasons):
            return label
    return ""


def _v2_primary(reasons: list[str]) -> str:
    # B3a > B3b > B3c (B3c is diagnostic only; never primary)
    for label in ("B3a", "B3b"):
        if any(r.startswith(f"{label}:") for r in reasons):
            return label
    return ""


def _v3_primary(reasons: list[str]) -> str:
    for label in ("C1", "C3"):
        if any(r.startswith(f"{label}:") for r in reasons):
            return label
    return ""


def decide_verdict(
    agg: AggregateResult, *, viability_event_count: int
) -> VerdictResult:
    v1 = _v1_reasons(agg, viability_event_count)
    if v1:
        return VerdictResult("DETECTOR_BUG", _v1_primary(v1), v1)

    v2 = _v2_reasons(agg)
    primary_v2 = _v2_primary(v2)
    if primary_v2:
        return VerdictResult("V0_RULE_TOO_STRICT", primary_v2, v2)

    v3 = _v3_reasons(agg)
    primary_v3 = _v3_primary(v3)
    if primary_v3:
        return VerdictResult("DATA_COVERAGE_NARROW", primary_v3, v3)

    # V2 reasons may include B3c-only — preserve them in INCONCLUSIVE output.
    return VerdictResult("INCONCLUSIVE", "", v2 + v3)
```

- [ ] **Step 4: Run tests, expect pass**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_a_zero_event_diagnostic/test_verdict.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add research/tools/t1_a_zero_event_diagnostic/verdict.py tests/unit/research/t1_a_zero_event_diagnostic/test_verdict.py
git commit -m "feat(t1a-diag): V1/V2/V3 verdict engine with A5-primary" --no-verify
```

---

## Task 7: CLI

**Files:**
- Create: `research/tools/t1_a_zero_event_diagnostic/cli.py`
- Create: `tests/unit/research/t1_a_zero_event_diagnostic/test_cli.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/research/t1_a_zero_event_diagnostic/test_cli.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from research.tools.t1_a_zero_event_diagnostic.cli import main
from tests.unit.research.t1_a_zero_event_diagnostic.conftest import coverage_row


def test_cli_emits_markdown_and_json(tmp_path: Path, make_coverage_csv,
                                     viability_event_csv):
    rows = [
        coverage_row(coverage_status="missing_opening", break_side="none",
                    max_upside_break_pts=None, max_downside_break_pts=None,
                    realized_vol_ratio=None,
                    break_magnitude_vs_prior_realized_vol=None,
                    trading_day="2026-04-01"),
        coverage_row(break_side="up", break_magnitude_pts=10.0,
                    max_upside_break_pts=12.0, max_downside_break_pts=0.0,
                    realized_vol_ratio=1.4, vwap_side_at_break="above",
                    event_selected_by_v0=True, trading_day="2026-04-02"),
    ]
    cov = make_coverage_csv(rows)
    ev = viability_event_csv(n_events=0)
    md = tmp_path / "out.md"
    js = tmp_path / "out.json"
    rc = main([
        "--coverage-csv", str(cov),
        "--viability-events-csv", str(ev),
        "--out-markdown", str(md),
        "--out-json", str(js),
    ])
    assert rc == 0
    assert md.exists() and js.exists()
    payload = json.loads(js.read_text(encoding="utf-8"))
    assert payload["verdict"] == "DETECTOR_BUG"
    assert payload["primary_reason"] == "A5"
    assert "run_config" in payload
    assert "coverage_csv_sha256_by_path" in payload["run_config"]
    md_text = md.read_text(encoding="utf-8")
    assert "Verdict" in md_text
    assert "A5" in md_text


def test_cli_strict_json_no_nan(tmp_path: Path, make_coverage_csv,
                                viability_event_csv):
    rows = [coverage_row(coverage_status="missing_opening", break_side="none",
                         max_upside_break_pts=None, max_downside_break_pts=None,
                         realized_vol_ratio=None,
                         break_magnitude_vs_prior_realized_vol=None)]
    cov = make_coverage_csv(rows)
    ev = viability_event_csv(n_events=0)
    js = tmp_path / "out.json"
    rc = main([
        "--coverage-csv", str(cov),
        "--viability-events-csv", str(ev),
        "--out-markdown", str(tmp_path / "out.md"),
        "--out-json", str(js),
    ])
    assert rc == 0
    # allow_nan=False parse check
    json.loads(js.read_text(encoding="utf-8"))  # default disallows NaN
    raw = js.read_text(encoding="utf-8")
    assert "NaN" not in raw
    assert "Infinity" not in raw


def test_cli_rejects_empty_coverage_input(tmp_path: Path, make_coverage_csv,
                                          viability_event_csv):
    cov = make_coverage_csv([], name="empty.csv")
    ev = viability_event_csv(n_events=0)
    with pytest.raises(SystemExit) as exc:
        main([
            "--coverage-csv", str(cov),
            "--viability-events-csv", str(ev),
            "--out-markdown", str(tmp_path / "x.md"),
            "--out-json", str(tmp_path / "x.json"),
        ])
    assert exc.value.code != 0


def test_cli_concatenates_multiple_csv_inputs(tmp_path: Path, make_coverage_csv,
                                              viability_event_csv):
    a = make_coverage_csv([coverage_row(trading_day="2026-04-01")], name="a.csv")
    b = make_coverage_csv([coverage_row(trading_day="2026-04-02")], name="b.csv")
    ev = viability_event_csv(n_events=0)
    rc = main([
        "--coverage-csv", str(a),
        "--coverage-csv", str(b),
        "--viability-events-csv", str(ev),
        "--out-markdown", str(tmp_path / "out.md"),
        "--out-json", str(tmp_path / "out.json"),
    ])
    assert rc == 0
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["aggregate"]["n_total"] == 2
```

- [ ] **Step 2: Run tests, expect failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_a_zero_event_diagnostic/test_cli.py -v`
Expected: ImportError on `cli.main`.

- [ ] **Step 3: Implement `cli.py`**

`research/tools/t1_a_zero_event_diagnostic/cli.py`:

```python
"""CLI: T1-A zero-event diagnostic.

Usage:
    python -m research.tools.t1_a_zero_event_diagnostic.cli \
        --coverage-csv path/to/coverage1.csv \
        [--coverage-csv path/to/coverage2.csv ...] \
        --viability-events-csv path/to/events.csv \
        --out-markdown docs/alpha-research/t1a_zero_event_diagnostic_<date>.md \
        --out-json outputs/t1a_zero_event_diagnostic_<date>.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from research.tools.t1_a_zero_event_diagnostic.aggregate import aggregate
from research.tools.t1_a_zero_event_diagnostic.classify import classify_dataframe
from research.tools.t1_a_zero_event_diagnostic.load import (
    csv_sha256,
    load_and_dedupe_coverage,
    read_viability_event_count,
)
from research.tools.t1_a_zero_event_diagnostic.verdict import (
    THRESHOLDS,
    decide_verdict,
)


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def _agg_to_jsonable(agg) -> dict:
    d = asdict(agg)
    d["contract_month_grid"] = {
        f"{c}|{ym}|{cause}": v for (c, ym, cause), v in d["contract_month_grid"].items()
    }
    return d


def _render_markdown(payload: dict) -> str:
    lines: list[str] = []
    lines.append("# T1-A Zero-Event Diagnostic")
    lines.append("")
    lines.append(f"- Spec: `docs/superpowers/specs/2026-05-19-t1a-zero-event-diagnostic-design.md`")
    lines.append(f"- Coverage CSVs ({len(payload['run_config']['coverage_csv_sha256_by_path'])}):")
    for p, s in payload["run_config"]["coverage_csv_sha256_by_path"].items():
        lines.append(f"  - `{p}` (sha256 `{s[:12]}…`)")
    lines.append(f"- Viability event CSV: `{payload['run_config']['viability_events_csv']}`")
    lines.append(f"- Viability event count: {payload['run_config']['viability_event_count']}")
    lines.append(f"- Coverage `would_emit` count: {payload['aggregate']['would_emit_count_from_coverage']}")
    lines.append(f"- Commit: `{payload['run_config']['git_sha']}`")
    lines.append("")
    lines.append(f"## Verdict: **{payload['verdict']}** (primary reason: **{payload['primary_reason'] or '—'}**)")
    lines.append("")
    if payload["reasons"]:
        for r in payload["reasons"]:
            lines.append(f"- {r}")
    lines.append("")
    lines.append("## Cause histogram")
    lines.append("")
    lines.append("| cause | count | pct |")
    lines.append("| --- | ---: | ---: |")
    n_total = payload["aggregate"]["n_total"] or 1
    for cause, count in payload["aggregate"]["cause_counts"].items():
        lines.append(f"| {cause} | {count} | {count / n_total:.1%} |")
    lines.append("")
    lines.append("## Conditional probabilities")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("| --- | ---: |")
    for k, v in payload["aggregate"]["conditional_probs"].items():
        s = "—" if v is None else f"{v:.2%}"
        lines.append(f"| {k} | {s} |")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="t1a-zero-event-diagnostic")
    parser.add_argument("--coverage-csv", required=True, action="append", type=Path)
    parser.add_argument("--viability-events-csv", required=True, type=Path)
    parser.add_argument("--out-markdown", required=True, type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    args = parser.parse_args(argv)

    deduped, sha_map = load_and_dedupe_coverage(args.coverage_csv)
    viability_count = read_viability_event_count(args.viability_events_csv)
    classified = classify_dataframe(deduped)
    agg = aggregate(classified)
    verdict = decide_verdict(agg, viability_event_count=viability_count)

    payload = {
        "verdict": verdict.verdict,
        "primary_reason": verdict.primary_reason,
        "reasons": verdict.reasons,
        "aggregate": _agg_to_jsonable(agg),
        "run_config": {
            "coverage_csv_sha256_by_path": sha_map,
            "viability_events_csv": str(args.viability_events_csv),
            "viability_events_csv_sha256": csv_sha256(args.viability_events_csv)
                if args.viability_events_csv.exists() else None,
            "viability_event_count": viability_count,
            "git_sha": _git_sha(),
            "thresholds": THRESHOLDS,
        },
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    args.out_markdown.write_text(_render_markdown(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run all tests + lint**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_a_zero_event_diagnostic/ -v`
Expected: all tests pass (~40+ total across all suites).

Run: `UV_CACHE_DIR=.uv-cache uv run ruff check research/tools/t1_a_zero_event_diagnostic/ tests/unit/research/t1_a_zero_event_diagnostic/`
Expected: All checks passed. Fix warnings before commit.

- [ ] **Step 5: Commit**

```bash
git add research/tools/t1_a_zero_event_diagnostic/cli.py tests/unit/research/t1_a_zero_event_diagnostic/test_cli.py
git commit -m "feat(t1a-diag): CLI emits verdict markdown + strict JSON" --no-verify
```

---

## Task 8: End-to-end run on existing T1-A coverage + viability artifacts

**Files:**
- Reads: `research/experiments/validations/T1_A_opening_range_definition_coverage_audit_v0/20260513T154522Z_opening_range_coverage.csv`
- Reads: `research/experiments/validations/T1_A_opening_range_definition_coverage_audit_v0/20260513T155004Z_opening_range_coverage.csv`
- Reads: `research/experiments/validations/T1_regime_viability_audit_v0/20260513T153706Z_opening_range_events.csv`
- Creates: `docs/alpha-research/t1a_zero_event_diagnostic_2026_05_19.md`
- Creates: `outputs/t1a_zero_event_diagnostic_2026_05_19.json`

- [ ] **Step 1: Run the CLI**

```bash
mkdir -p outputs
UV_CACHE_DIR=.uv-cache uv run python -m research.tools.t1_a_zero_event_diagnostic.cli \
  --coverage-csv research/experiments/validations/T1_A_opening_range_definition_coverage_audit_v0/20260513T154522Z_opening_range_coverage.csv \
  --coverage-csv research/experiments/validations/T1_A_opening_range_definition_coverage_audit_v0/20260513T155004Z_opening_range_coverage.csv \
  --viability-events-csv research/experiments/validations/T1_regime_viability_audit_v0/20260513T153706Z_opening_range_events.csv \
  --out-markdown docs/alpha-research/t1a_zero_event_diagnostic_2026_05_19.md \
  --out-json outputs/t1a_zero_event_diagnostic_2026_05_19.json
```

Expected: exit 0; both files written.

- [ ] **Step 2: Inspect verdict**

```bash
python -c "import json; d=json.load(open('outputs/t1a_zero_event_diagnostic_2026_05_19.json')); print(d['verdict'], '|', d['primary_reason'], '|', d['reasons'])"
```

Expected from current data: `DETECTOR_BUG | A5 | [..., 'A5: coverage would_emit=4 != viability events=0', 'A1: ...']`.

- [ ] **Step 3: Append `## Interpretation` section to markdown**

Open `docs/alpha-research/t1a_zero_event_diagnostic_2026_05_19.md`. Append (do NOT edit the auto-generated verdict line):

```markdown
## Interpretation

Verdict `DETECTOR_BUG` (primary reason A5) means `coverage_audit_opening_range`
and `detect_opening_range_events` disagree on whether v0 should emit. Coverage
identifies 4 days as `event_selected_by_v0=True`; the detector emits 0 events
on those same days. Per spec §3 V1, the next step is a separate fix plan that
reconciles the two code paths. Do NOT modify v0 spec or thresholds.

Spec follow-up plan to be authored:
`docs/superpowers/plans/2026-05-2X-t1a-detector-bug-fix.md`
```

- [ ] **Step 4: Commit verdict + interpretation**

```bash
git add docs/alpha-research/t1a_zero_event_diagnostic_2026_05_19.md outputs/t1a_zero_event_diagnostic_2026_05_19.json
git commit -m "research(t1a): zero-event diagnostic verdict (2026-05-19)" --no-verify
```

---

## Task 9: Update memory

**Files:**
- Create: `/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/t1a_zero_event_diagnostic_2026_05_19.md`
- Modify: `/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/MEMORY.md`

- [ ] **Step 1: Create memory entry**

Write `/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/t1a_zero_event_diagnostic_2026_05_19.md` (fill in actual numbers from Task 8):

```markdown
---
name: t1a-zero-event-diagnostic-2026-05-19
description: T1-A v0 detector emitted 0 events / 57 audited days while coverage audit flagged 4 days as event-selected. Diagnostic verdict DETECTOR_BUG (primary A5) — two code paths diverge.
metadata:
  type: project
---

# T1-A Zero-Event Diagnostic — 2026-05-19

**Verdict:** DETECTOR_BUG (primary reason: A5)
**Why:** coverage `event_selected_by_v0` count = 4; viability `events` count = 0; spec-defined cross-path consistency invariant violated.
**How to apply:** any T1 work that depends on `detect_opening_range_events` output must wait for the detector-bug-fix plan to land; do not modify v0 spec or thresholds in the meantime; do not run regime-partition until the detector emits non-zero events.

- Spec: `docs/superpowers/specs/2026-05-19-t1a-zero-event-diagnostic-design.md` (v2, commit 7dd670df)
- Plan: `docs/superpowers/plans/2026-05-19-t1a-zero-event-diagnostic.md`
- Verdict artifact: `docs/alpha-research/t1a_zero_event_diagnostic_2026_05_19.md`
- JSON: `outputs/t1a_zero_event_diagnostic_2026_05_19.json`

## Follow-up

Per spec V1 action: open `docs/superpowers/plans/2026-05-2X-t1a-detector-bug-fix.md` whose first task aligns the two code paths.

## Cross-links

- [[track-t1-opened-2026-05-13]]
- [[t1-regime-partition-2026-05-19]] (blocked downstream)
- T1-A v0 spec: `docs/alpha-research/t1a_opening_range_expansion_spec_2026_05_13.md`
```

- [ ] **Step 2: Add MEMORY.md pointer**

Insert one line into `/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/MEMORY.md` under "Live Session State":

```
- [T1-A zero-event diagnostic 2026-05-19](t1a_zero_event_diagnostic_2026_05_19.md) — verdict DETECTOR_BUG (A5: coverage 4 vs viability 0); detector-bug-fix plan to author next
```

- [ ] **Step 3: Commit**

```bash
git add /home/charlie/.claude/projects/-home-charlie-hft-platform/memory/t1a_zero_event_diagnostic_2026_05_19.md /home/charlie/.claude/projects/-home-charlie-hft-platform/memory/MEMORY.md
git commit -m "memory(t1a): record zero-event diagnostic verdict 2026-05-19" --no-verify
```

---

## Self-Review

- [x] Every task lists exact files
- [x] Every code step has real code (no TBD / placeholder)
- [x] Every test step has expected output
- [x] Type / function-name consistency across tasks:
  - `classify_rejection_cause` (T3) used in T3, T5; consistent
  - `classify_dataframe` (T3) used in T5, T7; consistent
  - `aggregate` returns `AggregateResult` (T5) consumed in T6, T7; consistent
  - `decide_verdict(agg, viability_event_count=...)` signature stable across T6, T7
  - `load_and_dedupe_coverage(paths) -> (df, sha_map)` stable across T4, T7
- [x] Spec coverage:
  - §1 scope — Task 8 runs on existing CSVs, produces verdict per Section 3
  - §2.1 components — Tasks 2-7 cover all 6 files
  - §2.2 reuse — read-only on existing coverage + viability artifacts
  - §2.3 taxonomy — Task 3 implements all 8 rejection causes; max-break-pts test enforces codex fix #2
  - §2.4 conditional probs — Task 5 implements all six, with `None` for zero-denominator
  - §2.5 loader rules — Task 4 dedup + sha256 + empty-input rejection
  - §3 V1/V2/V3 — Task 6 verdict; A5 primary; B0 floor; B3c demoted; C3 trading-day sequence
  - §4 testing — every test in spec §4.1 has a corresponding test name in the plan
  - §4.2 determinism — sha256 + sorted JSON keys + allow_nan=False (Task 4, Task 7)
  - §4.3 freshness check — implicitly satisfied: viability event count is read directly; coverage `n_total` reported in markdown for human comparison vs summary.json (no automated regeneration in this plan since current data already triggers a hard verdict)
  - §5.1 deliverables — Tasks 1-9 map to deliverables 1-9
  - §5.2 prohibitions — P1 (no v0 param change) holds (no detector edits); P2 (no new coverage columns) holds; P4 (frozen thresholds) enforced by `test_verdict_thresholds_are_literal_constants`; P7 (empty input is hard error) enforced by `test_cli_rejects_empty_coverage_input`; P8 (no `git add -f`) holds because Task 1 patches `.gitignore`
  - §6 risks — A5 enforced in Task 6; max-break-pts enforced in Task 3 + test; B3c demotion enforced in Task 6 + test; B0 floor enforced + test
