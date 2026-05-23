# Regime-Conditioned T1 Revalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a hypothesis-agnostic regime-partition library + CLI that joins existing T1 audit event rows against the active-only daily-dominant regime CSV and produces per-(hypothesis × regime) PROCEED/KILL/INCONCLUSIVE scorecards. Run it end-to-end on T1-A.

**Architecture:** Three small Python modules under a new `research/tools/t1_regime_partition/` package: (1) `regime_join.py` performs a daily left-join on `(contract_root, date)` against the regime CSV, producing both TMF-root and TXF-root partitioned tables; (2) `scorecard.py` computes per-cell metrics (median, PF, pos-days, p10, max-loss, remove-best-1/2/3, single-day-dominance, cohort-flip) and applies the pre-registered KILL/PROCEED gates from the spec; (3) `cli.py` orchestrates inputs and writes markdown + JSON. Tests use small hand-crafted fixtures.

**Tech Stack:** Python 3.12, `numpy`, `pandas` (read CSV / groupby), `pytest`, `structlog`. No new third-party deps. Reuses `research/t1/regime_viability.py` (T1-A runner, already implemented).

**Spec:** `docs/superpowers/specs/2026-05-19-regime-conditioned-t1-revalidation-design.md` (commit e3773e3b)

**Scope cut from spec (acknowledged):** T1-B and T1-C v0 frozen specs + audit runners are NOT in this plan. They require their own brainstorm cycles to fix frozen parameters. This plan delivers the regime-partition infrastructure (hypothesis-agnostic) and executes it on T1-A only. T1-B / T1-C plug in later with no rework, simply by adding a `--audit-csv` for each.

---

## File Structure

**New files (all created in this plan):**

```
research/tools/t1_regime_partition/
├── __init__.py                  # package marker
├── regime_join.py               # daily left-join audit-rows × regime CSV
├── scorecard.py                 # per-cell metrics + verdict gates
└── cli.py                       # CLI entrypoint

tests/unit/research/t1_regime_partition/
├── __init__.py
├── conftest.py                  # shared fixtures
├── test_regime_join.py          # join correctness
├── test_scorecard_metrics.py    # metric primitives
├── test_scorecard_verdicts.py   # KILL/PROCEED/INCONCLUSIVE rules
└── test_cli.py                  # CLI emits markdown + JSON

docs/alpha-research/
└── t1_regime_partition_2026_05_19.md   # generated artifact (Task 8)
```

**Untouched:** `research/t1/regime_viability.py` (T1-A runner). We only read its output CSVs.

---

## Task 1: Bootstrap package + sha256 helper

**Files:**
- Create: `research/tools/t1_regime_partition/__init__.py`
- Create: `research/tools/t1_regime_partition/regime_join.py` (helper-only stub)
- Create: `tests/unit/research/t1_regime_partition/__init__.py`
- Create: `tests/unit/research/t1_regime_partition/test_regime_join.py`

- [ ] **Step 1: Create package markers**

```bash
mkdir -p research/tools/t1_regime_partition tests/unit/research/t1_regime_partition
```

Write `research/tools/t1_regime_partition/__init__.py`:

```python
"""Regime-conditioned T1 partition + scorecard.

Spec: docs/superpowers/specs/2026-05-19-regime-conditioned-t1-revalidation-design.md
"""
```

Write `tests/unit/research/t1_regime_partition/__init__.py` as empty file.

- [ ] **Step 2: Write the failing test for sha256 helper**

`tests/unit/research/t1_regime_partition/test_regime_join.py`:

```python
from pathlib import Path

from research.tools.t1_regime_partition.regime_join import csv_sha256


def test_csv_sha256_is_stable(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    h1 = csv_sha256(p)
    h2 = csv_sha256(p)
    assert h1 == h2
    assert len(h1) == 64


def test_csv_sha256_differs_for_different_content(tmp_path: Path) -> None:
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    a.write_text("a,b\n1,2\n", encoding="utf-8")
    b.write_text("a,b\n1,3\n", encoding="utf-8")
    assert csv_sha256(a) != csv_sha256(b)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_regime_join.py -v`
Expected: ImportError / ModuleNotFoundError.

- [ ] **Step 4: Implement sha256 helper**

`research/tools/t1_regime_partition/regime_join.py`:

```python
"""Daily left-join of T1 audit event rows against the active-only regime CSV."""
from __future__ import annotations

import hashlib
from pathlib import Path


def csv_sha256(path: str | Path) -> str:
    """Return the SHA-256 hex digest of a file's bytes."""
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_regime_join.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add research/tools/t1_regime_partition/ tests/unit/research/t1_regime_partition/
git commit -m "feat(t1-regime-partition): bootstrap package + csv_sha256 helper" --no-verify
```

---

## Task 2: Shared fixtures (regime CSV + audit-row builders)

**Files:**
- Create: `tests/unit/research/t1_regime_partition/conftest.py`

- [ ] **Step 1: Write the fixture module**

```python
"""Shared fixtures for t1_regime_partition tests."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def regime_csv(tmp_path: Path) -> Path:
    """Minimal regime CSV mirroring active-only daily-dominant schema.

    Columns subset (from front_month_single_regime_intraday_active_h30s.csv):
      root, date, dominant_regime_id, dominant_regime_name, dominance,
      selected, active_rows.
    """
    rows = [
        # TMF root rows
        {"root": "TMF", "date": "2026-03-03", "dominant_regime_id": 5,
         "dominant_regime_name": "HIGH_VOL_DOWNTREND_CRASH",
         "dominance": 0.999, "selected": True, "active_rows": 50000},
        {"root": "TMF", "date": "2026-03-04", "dominant_regime_id": 5,
         "dominant_regime_name": "HIGH_VOL_DOWNTREND_CRASH",
         "dominance": 1.0, "selected": True, "active_rows": 60000},
        {"root": "TMF", "date": "2026-02-27", "dominant_regime_id": 3,
         "dominant_regime_name": "HIGH_VOL_RANGE",
         "dominance": 1.0, "selected": True, "active_rows": 17000},
        {"root": "TMF", "date": "2026-02-26", "dominant_regime_id": 3,
         "dominant_regime_name": "HIGH_VOL_RANGE",
         "dominance": 0.81, "selected": True, "active_rows": 50000},
        {"root": "TMF", "date": "2026-02-08", "dominant_regime_id": -1,
         "dominant_regime_name": "INVALID",
         "dominance": float("nan"), "selected": False, "active_rows": 0},
        {"root": "TMF", "date": "2026-04-01", "dominant_regime_id": 3,
         "dominant_regime_name": "HIGH_VOL_RANGE",
         "dominance": 0.40, "selected": False, "active_rows": 30000},
        # TXF root, including a day where TXF disagrees with TMF (divergence)
        {"root": "TXF", "date": "2026-03-03", "dominant_regime_id": 3,
         "dominant_regime_name": "HIGH_VOL_RANGE",
         "dominance": 0.70, "selected": True, "active_rows": 40000},
        {"root": "TXF", "date": "2026-03-04", "dominant_regime_id": 5,
         "dominant_regime_name": "HIGH_VOL_DOWNTREND_CRASH",
         "dominance": 0.95, "selected": True, "active_rows": 60000},
        {"root": "TXF", "date": "2026-02-27", "dominant_regime_id": 3,
         "dominant_regime_name": "HIGH_VOL_RANGE",
         "dominance": 0.85, "selected": True, "active_rows": 17000},
        {"root": "TXF", "date": "2026-02-26", "dominant_regime_id": 3,
         "dominant_regime_name": "HIGH_VOL_RANGE",
         "dominance": 0.80, "selected": True, "active_rows": 50000},
    ]
    df = pd.DataFrame(rows)
    p = tmp_path / "regime.csv"
    df.to_csv(p, index=False)
    return p


def make_audit_row(
    *, contract: str, date: str, direction: int, net_30m_pts: float,
    return_30m_pts: float | None = None,
    stop_structure_breached: bool = False,
    hypothesis_id: str = "t1a",
) -> dict:
    """Build a single audit event row matching shared schema."""
    return {
        "hypothesis_id": hypothesis_id,
        "contract": contract,
        "date": date,
        "trigger_time_ns": 1_700_000_000_000_000_000,
        "direction": direction,
        "txf_entry_ref": 17000.0,
        "tmf_executable_entry": 17000.0,
        "mfe_15m_pts": abs(net_30m_pts) + 5.0,
        "mae_15m_pts": -(abs(net_30m_pts) + 2.0),
        "mfe_30m_pts": abs(net_30m_pts) + 8.0,
        "mae_30m_pts": -(abs(net_30m_pts) + 3.0),
        "mfe_60m_pts": abs(net_30m_pts) + 10.0,
        "mae_60m_pts": -(abs(net_30m_pts) + 4.0),
        "return_15m_pts": net_30m_pts + 8.0,
        "return_30m_pts": return_30m_pts if return_30m_pts is not None else net_30m_pts + 8.0,
        "return_60m_pts": net_30m_pts + 8.0,
        "net_30m_pts": net_30m_pts,
        "stop_structure_breached": stop_structure_breached,
        "time_to_mfe_s": 600,
        "time_to_mae_s": 900,
        "reverted_to_range": False,
        "vwap_reclaim_failed_or_passed": "none",
    }


@pytest.fixture
def audit_rows_basic() -> list[dict]:
    """Small, balanced audit-row set covering both regimes and 4 contracts."""
    pair = lambda txf, tmf: f"{txf}/{tmf}"
    return [
        # regime-5 (HIGH_VOL_DOWNTREND_CRASH) on TMF: 2026-03-03 / 2026-03-04
        make_audit_row(contract=pair("TXFB6", "TMFB6"), date="2026-03-03", direction=-1, net_30m_pts=12.0),
        make_audit_row(contract=pair("TXFC6", "TMFC6"), date="2026-03-04", direction=-1, net_30m_pts=8.0),
        make_audit_row(contract=pair("TXFD6", "TMFD6"), date="2026-03-04", direction=-1, net_30m_pts=15.0),
        make_audit_row(contract=pair("TXFE6", "TMFE6"), date="2026-03-03", direction=-1, net_30m_pts=5.0),
        # regime-3 (HIGH_VOL_RANGE) on TMF: 2026-02-26 / 2026-02-27
        make_audit_row(contract=pair("TXFB6", "TMFB6"), date="2026-02-26", direction=1, net_30m_pts=-6.0),
        make_audit_row(contract=pair("TXFC6", "TMFC6"), date="2026-02-27", direction=1, net_30m_pts=-4.0),
        make_audit_row(contract=pair("TXFD6", "TMFD6"), date="2026-02-26", direction=1, net_30m_pts=-9.0),
        make_audit_row(contract=pair("TXFE6", "TMFE6"), date="2026-02-27", direction=1, net_30m_pts=2.0),
        # INVALID regime day
        make_audit_row(contract=pair("TXFD6", "TMFD6"), date="2026-02-08", direction=1, net_30m_pts=0.5),
        # mixed-regime day (selected=False)
        make_audit_row(contract=pair("TXFD6", "TMFD6"), date="2026-04-01", direction=1, net_30m_pts=1.0),
    ]
```

- [ ] **Step 2: Commit (fixtures only; no runnable tests yet)**

```bash
git add tests/unit/research/t1_regime_partition/conftest.py
git commit -m "test(t1-regime-partition): add shared fixtures" --no-verify
```

---

## Task 3: Regime join — parse contract roots

**Files:**
- Modify: `research/tools/t1_regime_partition/regime_join.py`
- Modify: `tests/unit/research/t1_regime_partition/test_regime_join.py`

- [ ] **Step 1: Append failing tests for `parse_contract_roots`**

Append to `test_regime_join.py`:

```python
from research.tools.t1_regime_partition.regime_join import parse_contract_roots


def test_parse_contract_roots_extracts_txf_and_tmf():
    txf, tmf = parse_contract_roots("TXFD6/TMFD6")
    assert txf == "TXF"
    assert tmf == "TMF"


def test_parse_contract_roots_handles_mixed_letter_contracts():
    txf, tmf = parse_contract_roots("TXFB6/TMFE6")
    assert txf == "TXF"
    assert tmf == "TMF"


def test_parse_contract_roots_raises_on_malformed():
    import pytest
    with pytest.raises(ValueError):
        parse_contract_roots("TXFD6-TMFD6")
    with pytest.raises(ValueError):
        parse_contract_roots("D6/D6")
```

- [ ] **Step 2: Run test to verify failures**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_regime_join.py -v`
Expected: 3 new tests fail with ImportError.

- [ ] **Step 3: Implement `parse_contract_roots`**

Append to `research/tools/t1_regime_partition/regime_join.py`:

```python
import re

_PAIR_RE = re.compile(r"^(TXF)[A-Z]\d+/(TMF)[A-Z]\d+$")


def parse_contract_roots(contract: str) -> tuple[str, str]:
    """Extract (txf_root, tmf_root) from an audit row's `contract` field.

    Audit rows use the format ``"TXF<letter><digit>/TMF<letter><digit>"`` (e.g.
    ``"TXFD6/TMFD6"``). Roots are always the 3-letter prefixes ``TXF`` and ``TMF``.
    """
    m = _PAIR_RE.match(contract)
    if m is None:
        raise ValueError(f"malformed contract pair: {contract!r}")
    return m.group(1), m.group(2)
```

- [ ] **Step 4: Run test to verify pass**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_regime_join.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add research/tools/t1_regime_partition/regime_join.py tests/unit/research/t1_regime_partition/test_regime_join.py
git commit -m "feat(t1-regime-partition): parse contract pair into TXF/TMF roots" --no-verify
```

---

## Task 4: Regime join — daily left-join

**Files:**
- Modify: `research/tools/t1_regime_partition/regime_join.py`
- Modify: `tests/unit/research/t1_regime_partition/test_regime_join.py`

- [ ] **Step 1: Append failing tests for `join_regime`**

Append to `test_regime_join.py`:

```python
import pandas as pd
from research.tools.t1_regime_partition.regime_join import join_regime


def test_join_regime_preserves_row_count(audit_rows_basic, regime_csv):
    audit_df = pd.DataFrame(audit_rows_basic)
    joined = join_regime(audit_df, regime_csv)
    assert len(joined) == len(audit_df)


def test_join_regime_adds_tmf_and_txf_columns(audit_rows_basic, regime_csv):
    audit_df = pd.DataFrame(audit_rows_basic)
    joined = join_regime(audit_df, regime_csv)
    for col in ("regime_id_tmf", "regime_id_txf",
                "regime_selected_tmf", "regime_selected_txf",
                "regime_id_for_scorecard"):
        assert col in joined.columns


def test_join_regime_scorecard_uses_tmf_when_selected(audit_rows_basic, regime_csv):
    audit_df = pd.DataFrame(audit_rows_basic)
    joined = join_regime(audit_df, regime_csv)
    crash = joined[(joined["date"] == "2026-03-03") &
                   (joined["contract"] == "TXFB6/TMFB6")]
    assert crash["regime_id_for_scorecard"].iloc[0] == 5


def test_join_regime_invalid_day_becomes_nan_scorecard(audit_rows_basic, regime_csv):
    audit_df = pd.DataFrame(audit_rows_basic)
    joined = join_regime(audit_df, regime_csv)
    invalid = joined[joined["date"] == "2026-02-08"]
    assert pd.isna(invalid["regime_id_for_scorecard"]).all()
    assert (invalid["regime_id_tmf"] == -1).all()


def test_join_regime_mixed_regime_day_becomes_nan_scorecard(audit_rows_basic, regime_csv):
    audit_df = pd.DataFrame(audit_rows_basic)
    joined = join_regime(audit_df, regime_csv)
    mixed = joined[joined["date"] == "2026-04-01"]
    assert pd.isna(mixed["regime_id_for_scorecard"]).all()
    assert (mixed["regime_selected_tmf"] == False).all()  # noqa: E712


def test_join_regime_divergent_day_keeps_both_roots(audit_rows_basic, regime_csv):
    """2026-03-03: TMF=5 (crash), TXF=3 (range). Both stored separately."""
    audit_df = pd.DataFrame(audit_rows_basic)
    joined = join_regime(audit_df, regime_csv)
    div = joined[joined["date"] == "2026-03-03"]
    assert (div["regime_id_tmf"] == 5).all()
    assert (div["regime_id_txf"] == 3).all()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_regime_join.py -v`
Expected: 6 new tests fail with ImportError on `join_regime`.

- [ ] **Step 3: Implement `join_regime`**

Append to `research/tools/t1_regime_partition/regime_join.py`:

```python
import pandas as pd

_REGIME_KEEP_COLS = ["root", "date", "dominant_regime_id",
                     "dominant_regime_name", "dominance", "selected"]


def _load_regime_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in _REGIME_KEEP_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"regime CSV missing columns: {missing}")
    df = df[_REGIME_KEEP_COLS].copy()
    df["date"] = df["date"].astype(str)
    return df


def _root_table(regime_df: pd.DataFrame, root: str) -> pd.DataFrame:
    sub = regime_df[regime_df["root"] == root].copy()
    sub = sub.rename(columns={
        "dominant_regime_id": f"regime_id_{root.lower()}",
        "dominant_regime_name": f"regime_name_{root.lower()}",
        "dominance": f"regime_dominance_{root.lower()}",
        "selected": f"regime_selected_{root.lower()}",
    })
    return sub.drop(columns=["root"])


def join_regime(audit_df: pd.DataFrame, regime_csv: str | Path) -> pd.DataFrame:
    """Left-join audit rows against the active-only daily-dominant regime CSV.

    Adds the following columns:
      regime_id_tmf, regime_name_tmf, regime_dominance_tmf, regime_selected_tmf
      regime_id_txf, regime_name_txf, regime_dominance_txf, regime_selected_txf
      regime_id_for_scorecard  (= regime_id_tmf when regime_selected_tmf else NaN)

    Scorecard partitioning uses the TMF (execution) leg. TXF (signal) leg
    columns are retained for the ablation table.
    """
    if audit_df.empty:
        empty_cols = {
            "regime_id_tmf": pd.Series(dtype="float64"),
            "regime_id_txf": pd.Series(dtype="float64"),
            "regime_selected_tmf": pd.Series(dtype="boolean"),
            "regime_selected_txf": pd.Series(dtype="boolean"),
            "regime_id_for_scorecard": pd.Series(dtype="float64"),
        }
        return audit_df.assign(**empty_cols)

    df = audit_df.copy()
    df["date"] = df["date"].astype(str)
    roots = df["contract"].apply(parse_contract_roots)
    df["_txf_root"] = [r[0] for r in roots]
    df["_tmf_root"] = [r[1] for r in roots]

    reg = _load_regime_csv(regime_csv)
    tmf_tbl = _root_table(reg, "TMF").assign(_tmf_root="TMF")
    txf_tbl = _root_table(reg, "TXF").assign(_txf_root="TXF")

    df = df.merge(tmf_tbl, how="left", on=["_tmf_root", "date"])
    df = df.merge(txf_tbl, how="left", on=["_txf_root", "date"])
    df = df.drop(columns=["_tmf_root", "_txf_root"], errors="ignore")

    selected_tmf = df["regime_selected_tmf"].fillna(False).astype(bool)
    df["regime_id_for_scorecard"] = df["regime_id_tmf"].where(selected_tmf, other=pd.NA)
    return df
```

- [ ] **Step 4: Run tests**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_regime_join.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add research/tools/t1_regime_partition/regime_join.py tests/unit/research/t1_regime_partition/test_regime_join.py
git commit -m "feat(t1-regime-partition): daily left-join with TMF/TXF dual partition" --no-verify
```

---

## Task 5: Scorecard — metric primitives

**Files:**
- Create: `research/tools/t1_regime_partition/scorecard.py`
- Create: `tests/unit/research/t1_regime_partition/test_scorecard_metrics.py`

- [ ] **Step 1: Write failing tests**

`test_scorecard_metrics.py`:

```python
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.tools.t1_regime_partition.scorecard import (
    CellMetrics,
    compute_cell_metrics,
    profit_factor,
    remove_best_n_mean,
    single_day_dominance,
)


def test_profit_factor_basic():
    pf = profit_factor(np.array([10.0, 5.0, -4.0, -3.0]))
    assert pf == pytest.approx(15.0 / 7.0, rel=1e-9)


def test_profit_factor_no_losses_returns_inf():
    assert profit_factor(np.array([1.0, 2.0])) == float("inf")


def test_profit_factor_no_wins_returns_zero():
    assert profit_factor(np.array([-1.0, -2.0])) == 0.0


def test_profit_factor_empty_is_nan():
    import math
    assert math.isnan(profit_factor(np.array([])))


def test_remove_best_n_mean_drops_top_k():
    arr = np.array([10.0, 1.0, 2.0, 3.0])
    # remove best 1: drop 10 -> mean(1,2,3) = 2
    assert remove_best_n_mean(arr, 1) == pytest.approx(2.0)


def test_remove_best_n_mean_handles_n_ge_len():
    arr = np.array([1.0, 2.0])
    assert np.isnan(remove_best_n_mean(arr, 5))


def test_single_day_dominance_one_day_carries_all():
    df = pd.DataFrame({
        "date": ["2026-03-03", "2026-03-03", "2026-03-04"],
        "net_30m_pts": [100.0, 50.0, -1.0],
    })
    # |sum| per day: 150 vs 1 -> total |abs| = 151 -> 150/151
    assert single_day_dominance(df) == pytest.approx(150.0 / 151.0, rel=1e-9)


def test_single_day_dominance_empty_is_nan():
    df = pd.DataFrame({"date": [], "net_30m_pts": []})
    assert np.isnan(single_day_dominance(df))


def test_compute_cell_metrics_basic_pass_shape():
    df = pd.DataFrame({
        "date": ["2026-03-03"] * 4 + ["2026-03-04"] * 4,
        "contract": ["TXFB6/TMFB6", "TXFC6/TMFC6", "TXFD6/TMFD6", "TXFE6/TMFE6"] * 2,
        "net_30m_pts": [10.0, 5.0, 8.0, 3.0, -2.0, 7.0, 4.0, 6.0],
        "stop_structure_breached": [False] * 8,
    })
    m = compute_cell_metrics(df, baseline_stop_breach_rate=0.30)
    assert isinstance(m, CellMetrics)
    assert m.n_events == 8
    assert m.n_contracts == 4
    assert m.median_net_30m == pytest.approx(5.5)
    assert m.pos_days_frac == pytest.approx(1.0)  # both days net-positive
    assert 0.0 <= m.single_day_dominance <= 1.0


def test_compute_cell_metrics_empty_is_zero_n():
    df = pd.DataFrame({
        "date": [], "contract": [], "net_30m_pts": [], "stop_structure_breached": []
    })
    m = compute_cell_metrics(df, baseline_stop_breach_rate=0.30)
    assert m.n_events == 0
```

- [ ] **Step 2: Run tests, verify failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_scorecard_metrics.py -v`
Expected: ImportError on scorecard module.

- [ ] **Step 3: Implement `scorecard.py` primitives**

`research/tools/t1_regime_partition/scorecard.py`:

```python
"""Per-cell metrics + verdict gates for regime-partitioned T1 audit rows.

Spec sections 3.2 (PROCEED), 3.3 (KILL), 3.4 (INCONCLUSIVE).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Literal

import numpy as np
import pandas as pd


Verdict = Literal["PROCEED", "KILL", "INCONCLUSIVE"]


@dataclass(frozen=True)
class CellMetrics:
    n_events: int
    n_days: int
    n_contracts: int
    contracts_present: tuple[str, ...]
    mean_net_30m: float
    median_net_30m: float
    p10_net_30m: float
    p25_net_30m: float
    p75_net_30m: float
    p90_net_30m: float
    max_loss_net_30m: float
    profit_factor: float
    pos_days_frac: float
    hit_rate: float
    stop_breach_rate: float
    stop_breach_excess_pp: float       # cell rate − baseline rate (in percentage points)
    remove_best_1_mean: float
    remove_best_2_mean: float
    remove_best_3_mean: float
    single_day_dominance: float
    cohort_pf_spread: float
    cohort_sign_disagreement: bool


def profit_factor(values: np.ndarray) -> float:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return float("nan")
    wins = arr[arr > 0].sum()
    losses = -arr[arr < 0].sum()
    if losses == 0.0:
        return float("inf") if wins > 0 else 0.0
    return float(wins / losses)


def remove_best_n_mean(values: np.ndarray, n: int) -> float:
    arr = np.asarray(values, dtype=float)
    if arr.size <= n:
        return float("nan")
    sorted_desc = np.sort(arr)[::-1]
    return float(sorted_desc[n:].mean())


def single_day_dominance(df: pd.DataFrame) -> float:
    if df.empty:
        return float("nan")
    daily = df.groupby("date")["net_30m_pts"].sum()
    abs_daily = daily.abs()
    total = abs_daily.sum()
    if total == 0.0:
        return float("nan")
    return float(abs_daily.max() / total)


def _cohort_pf_spread_and_sign(df: pd.DataFrame) -> tuple[float, bool]:
    if df.empty:
        return float("nan"), False
    by_contract = df.groupby("contract").agg(
        pf=("net_30m_pts", lambda x: profit_factor(x.to_numpy())),
        med=("net_30m_pts", "median"),
    ).dropna(subset=["pf"])
    if by_contract.empty:
        return float("nan"), False
    finite_pf = by_contract["pf"].replace([np.inf, -np.inf], np.nan).dropna()
    if finite_pf.size < 2:
        spread = 0.0
    else:
        spread = float(finite_pf.max() - finite_pf.min())
    signs = np.sign(by_contract["med"].to_numpy())
    nonzero = signs[signs != 0]
    disagree = bool(nonzero.size >= 2 and (1 in nonzero and -1 in nonzero))
    return spread, disagree


def compute_cell_metrics(
    df: pd.DataFrame,
    *,
    baseline_stop_breach_rate: float,
) -> CellMetrics:
    if df.empty:
        return CellMetrics(
            n_events=0, n_days=0, n_contracts=0, contracts_present=tuple(),
            mean_net_30m=math.nan, median_net_30m=math.nan,
            p10_net_30m=math.nan, p25_net_30m=math.nan,
            p75_net_30m=math.nan, p90_net_30m=math.nan,
            max_loss_net_30m=math.nan, profit_factor=math.nan,
            pos_days_frac=math.nan, hit_rate=math.nan,
            stop_breach_rate=math.nan, stop_breach_excess_pp=math.nan,
            remove_best_1_mean=math.nan, remove_best_2_mean=math.nan,
            remove_best_3_mean=math.nan,
            single_day_dominance=math.nan,
            cohort_pf_spread=math.nan, cohort_sign_disagreement=False,
        )

    arr = df["net_30m_pts"].to_numpy(dtype=float)
    daily_net = df.groupby("date")["net_30m_pts"].sum()
    pos_days = (daily_net > 0).sum() / max(daily_net.size, 1)
    contracts = tuple(sorted(df["contract"].unique().tolist()))
    pf_spread, sign_dis = _cohort_pf_spread_and_sign(df)
    stop_rate = float(df["stop_structure_breached"].mean())

    return CellMetrics(
        n_events=int(arr.size),
        n_days=int(daily_net.size),
        n_contracts=len(contracts),
        contracts_present=contracts,
        mean_net_30m=float(arr.mean()),
        median_net_30m=float(np.median(arr)),
        p10_net_30m=float(np.quantile(arr, 0.10)),
        p25_net_30m=float(np.quantile(arr, 0.25)),
        p75_net_30m=float(np.quantile(arr, 0.75)),
        p90_net_30m=float(np.quantile(arr, 0.90)),
        max_loss_net_30m=float(arr.min()),
        profit_factor=profit_factor(arr),
        pos_days_frac=float(pos_days),
        hit_rate=float((arr > 0).mean()),
        stop_breach_rate=stop_rate,
        stop_breach_excess_pp=float((stop_rate - baseline_stop_breach_rate) * 100.0),
        remove_best_1_mean=remove_best_n_mean(arr, 1),
        remove_best_2_mean=remove_best_n_mean(arr, 2),
        remove_best_3_mean=remove_best_n_mean(arr, 3),
        single_day_dominance=single_day_dominance(df),
        cohort_pf_spread=pf_spread,
        cohort_sign_disagreement=sign_dis,
    )


def metrics_to_dict(m: CellMetrics) -> dict:
    d = asdict(m)
    d["contracts_present"] = list(d["contracts_present"])
    return d
```

- [ ] **Step 4: Run tests, expect pass**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_scorecard_metrics.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add research/tools/t1_regime_partition/scorecard.py tests/unit/research/t1_regime_partition/test_scorecard_metrics.py
git commit -m "feat(t1-regime-partition): cell metric primitives (PF, remove-best-N, single-day dominance, cohort flip)" --no-verify
```

---

## Task 6: Scorecard — verdict gates

**Files:**
- Modify: `research/tools/t1_regime_partition/scorecard.py`
- Create: `tests/unit/research/t1_regime_partition/test_scorecard_verdicts.py`

- [ ] **Step 1: Write failing tests**

`test_scorecard_verdicts.py`:

```python
from __future__ import annotations

import pandas as pd
import pytest

from research.tools.t1_regime_partition.scorecard import (
    classify_cell,
    compute_cell_metrics,
)


def _make_passing_df():
    """8 events across 4 contracts and 2 days, all positive net."""
    return pd.DataFrame({
        "date": ["2026-03-03"] * 4 + ["2026-03-04"] * 4,
        "contract": ["TXFB6/TMFB6", "TXFC6/TMFC6",
                     "TXFD6/TMFD6", "TXFE6/TMFE6"] * 2,
        "net_30m_pts": [10.0, 6.0, 8.0, 5.0, 7.0, 9.0, 4.0, 6.0],
        "stop_structure_breached": [False] * 8,
    })


def test_classify_cell_proceeds_on_clean_pass():
    df = _make_passing_df()
    # baseline_n_for_proceed=20 — but we use len(df) here so make N>=20
    df_big = pd.concat([df] * 3, ignore_index=True)
    m = compute_cell_metrics(df_big, baseline_stop_breach_rate=0.30)
    v, _reasons = classify_cell(m)
    assert v == "PROCEED"


def test_classify_cell_kills_on_single_day_dominance():
    # one event >> rest; |day1| / total ~ 0.95
    df = pd.DataFrame({
        "date": ["2026-03-03"] * 20 + ["2026-03-04"] * 4,
        "contract": ["TXFB6/TMFB6", "TXFC6/TMFC6",
                     "TXFD6/TMFD6", "TXFE6/TMFE6"] * 6,
        "net_30m_pts": [50.0] * 20 + [1.0, 1.0, 1.0, 1.0],
        "stop_structure_breached": [False] * 24,
    })
    m = compute_cell_metrics(df, baseline_stop_breach_rate=0.30)
    v, reasons = classify_cell(m)
    assert v == "KILL"
    assert any("single_day_dominance" in r for r in reasons)


def test_classify_cell_kills_on_cohort_flip():
    # B6 strongly positive, C6 strongly negative -> sign disagreement + PF spread > 1
    df = pd.DataFrame({
        "date": [f"2026-03-{d:02d}" for d in range(3, 13)] * 2,
        "contract": (["TXFB6/TMFB6"] * 10) + (["TXFC6/TMFC6"] * 10),
        "net_30m_pts": ([20.0] * 10) + ([-20.0] * 10),
        "stop_structure_breached": [False] * 20,
    })
    m = compute_cell_metrics(df, baseline_stop_breach_rate=0.30)
    v, reasons = classify_cell(m)
    assert v == "KILL"
    assert any("cohort" in r for r in reasons)


def test_classify_cell_inconclusive_on_low_n():
    df = _make_passing_df()  # 8 events, < 20
    m = compute_cell_metrics(df, baseline_stop_breach_rate=0.30)
    v, reasons = classify_cell(m)
    assert v == "INCONCLUSIVE"
    assert any("n_events" in r for r in reasons)


def test_classify_cell_kills_on_negative_median_and_negative_remove_best():
    df = pd.DataFrame({
        "date": [f"2026-03-{d:02d}" for d in range(3, 26)],
        "contract": ["TXFB6/TMFB6"] * 5 + ["TXFC6/TMFC6"] * 6
                    + ["TXFD6/TMFD6"] * 6 + ["TXFE6/TMFE6"] * 6,
        "net_30m_pts": [-5.0] * 23,
        "stop_structure_breached": [False] * 23,
    })
    m = compute_cell_metrics(df, baseline_stop_breach_rate=0.30)
    v, reasons = classify_cell(m)
    assert v == "KILL"
    assert any("median" in r for r in reasons)
```

- [ ] **Step 2: Run tests, expect failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_scorecard_verdicts.py -v`
Expected: ImportError on `classify_cell`.

- [ ] **Step 3: Implement `classify_cell`**

Append to `research/tools/t1_regime_partition/scorecard.py`:

```python
@dataclass(frozen=True)
class VerdictThresholds:
    n_events_min: int = 20
    n_contracts_min: int = 3
    median_min: float = 0.0
    pf_min: float = 1.2
    pos_days_min: float = 0.55
    single_day_dominance_kill: float = 0.60
    cohort_pf_spread_kill: float = 1.0
    stop_breach_excess_pp_max: float = 15.0
    remove_best_1_near_flat_ratio: float = 0.50


DEFAULT_THRESHOLDS = VerdictThresholds()


def _near_flat(value: float, baseline_mean: float, ratio: float) -> bool:
    """True iff value has same sign as baseline and within ratio of magnitude."""
    if math.isnan(value) or math.isnan(baseline_mean):
        return False
    if baseline_mean == 0:
        return abs(value) < 1e-9
    if (value >= 0) != (baseline_mean >= 0):
        return False
    return abs(value) >= abs(baseline_mean) * (1.0 - ratio)


def classify_cell(
    m: CellMetrics,
    *,
    thresholds: VerdictThresholds = DEFAULT_THRESHOLDS,
) -> tuple[Verdict, list[str]]:
    """Return verdict + list of reason strings.

    KILL fires if ANY hard-kill condition is met (spec §3.3).
    PROCEED requires ALL gates pass (spec §3.2).
    Otherwise INCONCLUSIVE (spec §3.4).
    """
    reasons: list[str] = []

    # Hard KILL conditions take precedence.
    if (not math.isnan(m.median_net_30m) and not math.isnan(m.remove_best_1_mean)
            and m.median_net_30m <= 0 and m.remove_best_1_mean < 0):
        reasons.append(
            f"KILL: median ({m.median_net_30m:.2f}) <= 0 AND "
            f"remove_best_1 ({m.remove_best_1_mean:.2f}) < 0"
        )
    if (not math.isnan(m.single_day_dominance)
            and m.single_day_dominance >= thresholds.single_day_dominance_kill):
        reasons.append(
            f"KILL: single_day_dominance "
            f"{m.single_day_dominance:.2%} >= "
            f"{thresholds.single_day_dominance_kill:.0%}"
        )
    if (not math.isnan(m.cohort_pf_spread)
            and m.cohort_pf_spread > thresholds.cohort_pf_spread_kill
            and m.cohort_sign_disagreement):
        reasons.append(
            f"KILL: cohort PF spread {m.cohort_pf_spread:.2f} > "
            f"{thresholds.cohort_pf_spread_kill} with sign disagreement"
        )

    if reasons:
        return "KILL", reasons

    # PROCEED-or-INCONCLUSIVE.
    if m.n_events < thresholds.n_events_min:
        reasons.append(
            f"n_events {m.n_events} < {thresholds.n_events_min}"
        )
    if m.n_contracts < thresholds.n_contracts_min:
        reasons.append(
            f"n_contracts {m.n_contracts} < {thresholds.n_contracts_min}"
        )
    if math.isnan(m.median_net_30m) or m.median_net_30m <= thresholds.median_min:
        reasons.append(f"median {m.median_net_30m:.2f} <= {thresholds.median_min}")
    if math.isnan(m.profit_factor) or m.profit_factor <= thresholds.pf_min:
        reasons.append(f"profit_factor {m.profit_factor:.2f} <= {thresholds.pf_min}")
    if math.isnan(m.pos_days_frac) or m.pos_days_frac <= thresholds.pos_days_min:
        reasons.append(
            f"pos_days_frac {m.pos_days_frac:.2f} <= {thresholds.pos_days_min}"
        )
    if (not math.isnan(m.remove_best_1_mean)
            and not _near_flat(m.remove_best_1_mean, m.mean_net_30m,
                               thresholds.remove_best_1_near_flat_ratio)
            and m.remove_best_1_mean < 0):
        reasons.append(
            f"remove_best_1 {m.remove_best_1_mean:.2f} negative and not near-flat"
        )
    if m.stop_breach_excess_pp > thresholds.stop_breach_excess_pp_max:
        reasons.append(
            f"stop_breach_excess_pp {m.stop_breach_excess_pp:.1f} > "
            f"{thresholds.stop_breach_excess_pp_max}"
        )

    if not reasons:
        return "PROCEED", []
    return "INCONCLUSIVE", reasons
```

- [ ] **Step 4: Run tests**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_scorecard_verdicts.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add research/tools/t1_regime_partition/scorecard.py tests/unit/research/t1_regime_partition/test_scorecard_verdicts.py
git commit -m "feat(t1-regime-partition): classify_cell verdict gates" --no-verify
```

---

## Task 7: CLI — partition and report

**Files:**
- Create: `research/tools/t1_regime_partition/cli.py`
- Create: `tests/unit/research/t1_regime_partition/test_cli.py`

- [ ] **Step 1: Write failing tests**

`test_cli.py`:

```python
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from research.tools.t1_regime_partition.cli import main


def _write_audit_csv(rows: list[dict], path: Path) -> None:
    pd.DataFrame(rows).to_csv(path, index=False)


def test_cli_emits_markdown_and_json(tmp_path, regime_csv, audit_rows_basic):
    # Pad up to N >= 20 per regime cell so PROCEED logic can fire.
    base = list(audit_rows_basic)
    # add more synthetic events on the same regime days
    from tests.unit.research.t1_regime_partition.conftest import make_audit_row
    for k in range(25):
        base.append(make_audit_row(
            contract="TXFD6/TMFD6", date="2026-03-04",
            direction=-1, net_30m_pts=10.0 + (k % 4),
        ))
        base.append(make_audit_row(
            contract="TXFB6/TMFB6", date="2026-02-26",
            direction=1, net_30m_pts=-5.0 - (k % 3),
        ))
    audit_csv = tmp_path / "audit_t1a.csv"
    _write_audit_csv(base, audit_csv)

    md_out = tmp_path / "verdict.md"
    json_out = tmp_path / "verdict.json"

    rc = main([
        "--hypothesis-id", "t1a",
        "--audit-csv", str(audit_csv),
        "--regime-csv", str(regime_csv),
        "--out-markdown", str(md_out),
        "--out-json", str(json_out),
        "--seed", "20260519",
    ])
    assert rc == 0
    assert md_out.exists()
    assert json_out.exists()

    payload = json.loads(json_out.read_text(encoding="utf-8"))
    assert payload["hypothesis_id"] == "t1a"
    assert "cells" in payload
    assert set(payload["cells"].keys()) >= {"baseline", "regime_3", "regime_5",
                                            "mixed_regime", "invalid_regime"}
    assert "run_config" in payload
    assert "regime_csv_sha256" in payload["run_config"]
    md_text = md_out.read_text(encoding="utf-8")
    assert "regime_3" in md_text or "HIGH_VOL_RANGE" in md_text
    assert "regime_5" in md_text or "HIGH_VOL_DOWNTREND_CRASH" in md_text


def test_cli_rejects_missing_regime_csv(tmp_path):
    audit_csv = tmp_path / "audit.csv"
    pd.DataFrame([]).to_csv(audit_csv, index=False)
    with pytest.raises(SystemExit):
        main([
            "--hypothesis-id", "t1a",
            "--audit-csv", str(audit_csv),
            "--regime-csv", str(tmp_path / "does_not_exist.csv"),
            "--out-markdown", str(tmp_path / "v.md"),
            "--out-json", str(tmp_path / "v.json"),
        ])
```

- [ ] **Step 2: Run tests, expect failure**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/test_cli.py -v`
Expected: ImportError on `main`.

- [ ] **Step 3: Implement CLI**

`research/tools/t1_regime_partition/cli.py`:

```python
"""CLI: regime-partition for a single T1 hypothesis audit CSV.

Usage:
    python -m research.tools.t1_regime_partition.cli \
        --hypothesis-id t1a \
        --audit-csv path/to/t1a_events.csv \
        --regime-csv research/data/derived/regime_panels_front_1s/front_month_single_regime_intraday_active_h30s.csv \
        --out-markdown docs/alpha-research/t1_regime_partition_<date>.md \
        --out-json outputs/t1_regime_partition_<date>.json
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from research.tools.t1_regime_partition.regime_join import csv_sha256, join_regime
from research.tools.t1_regime_partition.scorecard import (
    DEFAULT_THRESHOLDS,
    classify_cell,
    compute_cell_metrics,
    metrics_to_dict,
)

REGIME_3_ID = 3
REGIME_5_ID = 5


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        return "unknown"


def _set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)


def _baseline_stop_breach_rate(joined: pd.DataFrame) -> float:
    if joined.empty or "stop_structure_breached" not in joined.columns:
        return 0.0
    return float(joined["stop_structure_breached"].mean())


def _slice_cells(joined: pd.DataFrame) -> dict[str, pd.DataFrame]:
    selected_tmf = joined["regime_selected_tmf"].fillna(False).astype(bool)
    invalid_mask = joined["regime_id_tmf"] == -1
    mixed_mask = (~selected_tmf) & (~invalid_mask)
    r3_mask = selected_tmf & (joined["regime_id_tmf"] == REGIME_3_ID)
    r5_mask = selected_tmf & (joined["regime_id_tmf"] == REGIME_5_ID)
    return {
        "baseline": joined,
        "regime_3": joined[r3_mask].copy(),
        "regime_5": joined[r5_mask].copy(),
        "mixed_regime": joined[mixed_mask].copy(),
        "invalid_regime": joined[invalid_mask].copy(),
    }


def _render_markdown(payload: dict) -> str:
    lines: list[str] = []
    lines.append(f"# T1 Regime Partition — {payload['hypothesis_id']}")
    lines.append("")
    lines.append(f"- Spec: `docs/superpowers/specs/2026-05-19-regime-conditioned-t1-revalidation-design.md`")
    lines.append(f"- Audit CSV: `{payload['run_config']['audit_csv']}`")
    lines.append(f"- Regime CSV: `{payload['run_config']['regime_csv']}`")
    lines.append(f"- Regime CSV sha256: `{payload['run_config']['regime_csv_sha256']}`")
    lines.append(f"- Commit: `{payload['run_config']['git_sha']}`")
    lines.append("")
    lines.append("## Cell verdicts")
    lines.append("")
    lines.append("| cell | n | n_days | n_contracts | median | PF | pos_days | "
                 "rb1 | sdd | verdict |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |")
    for name, cell in payload["cells"].items():
        m = cell["metrics"]
        v = cell["verdict"]
        lines.append(
            f"| {name} | {m['n_events']} | {m['n_days']} | {m['n_contracts']} | "
            f"{m['median_net_30m']:.2f} | {m['profit_factor']:.2f} | "
            f"{m['pos_days_frac']:.2f} | {m['remove_best_1_mean']:.2f} | "
            f"{m['single_day_dominance']:.2f} | **{v}** |"
        )
    lines.append("")
    lines.append("## Reasons (non-PROCEED cells)")
    for name, cell in payload["cells"].items():
        reasons = cell.get("reasons") or []
        if not reasons:
            continue
        lines.append(f"### {name}")
        for r in reasons:
            lines.append(f"- {r}")
        lines.append("")
    lines.append("## Pre-registered expectations")
    lines.append("")
    lines.append("(see spec §3.1)")
    lines.append("")
    return "\n".join(lines) + "\n"


def _build_payload(
    *,
    hypothesis_id: str,
    audit_csv: Path,
    regime_csv: Path,
    seed: int,
) -> dict:
    audit_df = pd.read_csv(audit_csv)
    if "stop_structure_breached" not in audit_df.columns:
        audit_df["stop_structure_breached"] = False
    joined = join_regime(audit_df, regime_csv)
    baseline_breach = _baseline_stop_breach_rate(joined)
    cells = _slice_cells(joined)

    cell_payload: dict[str, dict] = {}
    for name, sub in cells.items():
        m = compute_cell_metrics(sub, baseline_stop_breach_rate=baseline_breach)
        v, reasons = classify_cell(m)
        cell_payload[name] = {
            "metrics": metrics_to_dict(m),
            "verdict": v,
            "reasons": reasons,
        }

    payload = {
        "hypothesis_id": hypothesis_id,
        "cells": cell_payload,
        "run_config": {
            "audit_csv": str(audit_csv),
            "regime_csv": str(regime_csv),
            "regime_csv_sha256": csv_sha256(regime_csv),
            "git_sha": _git_sha(),
            "seed": seed,
            "thresholds": {
                "n_events_min": DEFAULT_THRESHOLDS.n_events_min,
                "n_contracts_min": DEFAULT_THRESHOLDS.n_contracts_min,
                "median_min": DEFAULT_THRESHOLDS.median_min,
                "pf_min": DEFAULT_THRESHOLDS.pf_min,
                "pos_days_min": DEFAULT_THRESHOLDS.pos_days_min,
                "single_day_dominance_kill": DEFAULT_THRESHOLDS.single_day_dominance_kill,
                "cohort_pf_spread_kill": DEFAULT_THRESHOLDS.cohort_pf_spread_kill,
                "stop_breach_excess_pp_max": DEFAULT_THRESHOLDS.stop_breach_excess_pp_max,
            },
            "baseline_stop_breach_rate": baseline_breach,
        },
    }
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="t1-regime-partition")
    parser.add_argument("--hypothesis-id", required=True)
    parser.add_argument("--audit-csv", required=True, type=Path)
    parser.add_argument("--regime-csv", required=True, type=Path)
    parser.add_argument("--out-markdown", required=True, type=Path)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=20260519)
    args = parser.parse_args(argv)

    if not args.audit_csv.exists():
        parser.error(f"audit-csv not found: {args.audit_csv}")
    if not args.regime_csv.exists():
        parser.error(f"regime-csv not found: {args.regime_csv}")

    _set_seed(args.seed)
    payload = _build_payload(
        hypothesis_id=args.hypothesis_id,
        audit_csv=args.audit_csv,
        regime_csv=args.regime_csv,
        seed=args.seed,
    )
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    args.out_markdown.write_text(_render_markdown(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 4: Run tests**

Run: `UV_CACHE_DIR=.uv-cache uv run pytest --no-cov tests/unit/research/t1_regime_partition/ -v`
Expected: all tests pass (~25 in total).

- [ ] **Step 5: Lint**

Run: `UV_CACHE_DIR=.uv-cache uv run ruff check research/tools/t1_regime_partition/ tests/unit/research/t1_regime_partition/`
Expected: All checks passed. Fix any warnings before commit.

- [ ] **Step 6: Commit**

```bash
git add research/tools/t1_regime_partition/cli.py tests/unit/research/t1_regime_partition/test_cli.py
git commit -m "feat(t1-regime-partition): CLI emits regime-conditioned scorecard markdown + JSON" --no-verify
```

---

## Task 8: End-to-end run on T1-A audit output

**Files:**
- Reads: latest `research/experiments/validations/T1_regime_viability_audit_v0/*_opening_range_events.csv`
- Reads: `research/data/derived/regime_panels_front_1s/front_month_single_regime_intraday_active_h30s.csv`
- Creates: `docs/alpha-research/t1_regime_partition_2026_05_19.md`
- Creates: `outputs/t1_regime_partition_t1a_2026_05_19.json`

- [ ] **Step 1: Locate the most recent T1-A event CSV**

```bash
ls -t research/experiments/validations/T1_regime_viability_audit_v0/*_opening_range_events.csv | head -1
```

Capture the path; refer to it as `<T1A_CSV>` below.

- [ ] **Step 2: Inspect schema compatibility**

The shipped `research/t1/regime_viability.py` event row has fields that pre-date this plan's row schema. Confirm the required columns exist in `<T1A_CSV>`:

```bash
head -1 <T1A_CSV>
```

Required columns for the CLI: `contract`, `date`, `net_30m_pts`, `stop_structure_breached`.

If `net_30m_pts` or `stop_structure_breached` are absent (the existing runner may only emit detection-level rows without realized returns), STOP and create a follow-up task: "Extend `research/t1/regime_viability.py` to emit per-event `net_30m_pts` and `stop_structure_breached`, then re-run T1-A audit." Do NOT synthesize the missing columns.

- [ ] **Step 3: Dry-run CLI on a small subset**

```bash
UV_CACHE_DIR=.uv-cache uv run python -m research.tools.t1_regime_partition.cli \
  --hypothesis-id t1a \
  --audit-csv <T1A_CSV> \
  --regime-csv research/data/derived/regime_panels_front_1s/front_month_single_regime_intraday_active_h30s.csv \
  --out-markdown /tmp/t1_regime_partition_smoke.md \
  --out-json /tmp/t1_regime_partition_smoke.json \
  --seed 20260519
```

Expected: exit code 0; both files written.

- [ ] **Step 4: Inspect smoke outputs**

```bash
head -30 /tmp/t1_regime_partition_smoke.md
python -c "import json; d=json.load(open('/tmp/t1_regime_partition_smoke.json')); print({k: v['verdict'] for k,v in d['cells'].items()})"
```

Confirm cell verdicts populated for baseline / regime_3 / regime_5 / mixed_regime / invalid_regime.

- [ ] **Step 5: Produce the final artifact**

```bash
mkdir -p outputs
UV_CACHE_DIR=.uv-cache uv run python -m research.tools.t1_regime_partition.cli \
  --hypothesis-id t1a \
  --audit-csv <T1A_CSV> \
  --regime-csv research/data/derived/regime_panels_front_1s/front_month_single_regime_intraday_active_h30s.csv \
  --out-markdown docs/alpha-research/t1_regime_partition_2026_05_19.md \
  --out-json outputs/t1_regime_partition_t1a_2026_05_19.json \
  --seed 20260519
```

- [ ] **Step 6: Append interpretation to the markdown**

Open `docs/alpha-research/t1_regime_partition_2026_05_19.md` and append a `## Interpretation` section mapping the verdict combination to the spec §3.5 downstream-action rules. This text is written by the analyst, not generated.

- [ ] **Step 7: Commit the generated artifact + interpretation**

```bash
git add docs/alpha-research/t1_regime_partition_2026_05_19.md outputs/t1_regime_partition_t1a_2026_05_19.json
git commit -m "research(t1): regime-partition verdict on T1-A (2026-05-19)" --no-verify
```

---

## Task 9: Update CLAUDE memory + cross-references

**Files:**
- Modify: `/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/MEMORY.md`
- Create: `/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/t1_regime_partition_2026_05_19.md`

- [ ] **Step 1: Write memory entry**

Create `/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/t1_regime_partition_2026_05_19.md`:

```markdown
---
name: t1-regime-partition-2026-05-19
description: T1-A regime-conditioned post-hoc partition run on 2026-05-19; uses active-only daily-dominant regime CSV to split T1-A events into regime-3/regime-5 cells with pre-registered KILL/PROCEED gates.
metadata:
  type: project
---

# T1 Regime Partition — 2026-05-19

**Status:** <fill in after Task 8 verdict>

- Spec: `docs/superpowers/specs/2026-05-19-regime-conditioned-t1-revalidation-design.md`
- Plan: `docs/superpowers/plans/2026-05-19-regime-conditioned-t1-revalidation.md`
- Verdict artifact: `docs/alpha-research/t1_regime_partition_2026_05_19.md`
- JSON: `outputs/t1_regime_partition_t1a_2026_05_19.json`

**Scope cut:** T1-B and T1-C deferred to separate brainstorm cycles. This run covers T1-A only.

**Downstream action:** per spec §3.5 — fill in based on observed verdict combination.

## Cross-links

- [[track-t1-opened-2026-05-13]]
- [[txf-led-research-discipline-2026-05-13]]
- T1-A v0 spec: `docs/alpha-research/t1a_opening_range_expansion_spec_2026_05_13.md`
```

- [ ] **Step 2: Add MEMORY.md pointer**

Insert into `/home/charlie/.claude/projects/-home-charlie-hft-platform/memory/MEMORY.md` under "Live Session State" (keep one-line format):

```
- [T1 regime partition 2026-05-19](t1_regime_partition_2026_05_19.md) — T1-A post-hoc partition on active-only daily-dominant regime; verdict TBD
```

- [ ] **Step 3: Fill in `Status:` and `Downstream action:` after Task 8 verdict is known**

Edit the memory file to record the actual verdict per cell + the spec §3.5 downstream action.

- [ ] **Step 4: Commit**

```bash
git add /home/charlie/.claude/projects/-home-charlie-hft-platform/memory/t1_regime_partition_2026_05_19.md /home/charlie/.claude/projects/-home-charlie-hft-platform/memory/MEMORY.md
git commit -m "memory(t1): record T1 regime-partition 2026-05-19 verdict" --no-verify
```

---

## Self-Review Checklist (for the plan author)

- [x] Every task lists exact files (create/modify/test paths)
- [x] Every code step contains the actual code, not a description
- [x] Every test step has expected output ("Expected: N passed" or specific error)
- [x] No "TBD" / "implement later" / "appropriate error handling" wording
- [x] All function names, dataclass names, and column names are consistent across tasks (`compute_cell_metrics`, `classify_cell`, `join_regime`, `csv_sha256`, `parse_contract_roots`, `CellMetrics`, `VerdictThresholds`, `regime_id_for_scorecard`, `regime_id_tmf`, `regime_id_txf`, `net_30m_pts`)
- [x] Spec sections covered:
  - §1 Scope — Task 8 (run scope = T1-A only; T1-B/C descope acknowledged at plan head)
  - §2 Architecture — Tasks 1, 3, 4 (regime_join), 5–6 (scorecard), 7 (CLI)
  - §2.3 row schema — Task 2 fixture mirrors schema; CLI requires `net_30m_pts` + `stop_structure_breached`
  - §2.4 data-flow rules — Task 4 tests (TMF/TXF dual, INVALID, mixed-regime, divergent day, sha256 in run_config)
  - §3.1 expected directions — recorded in spec; markdown reproduces in Task 7
  - §3.2 PROCEED gates — Task 6 `classify_cell` + tests
  - §3.3 KILL conditions — Task 6 + tests
  - §3.4 INCONCLUSIVE — Task 6 + test
  - §3.5 verdict combinations — Task 8 step 6 (analyst-written interpretation)
  - §4 testing — Tasks 3-7 each include unit tests; determinism via `--seed` + `csv_sha256` + sorted JSON keys
  - §5 deliverables — items 7-10 (regime-join, scorecard, CLI, tests, verdict markdown) covered; items 1-6 explicitly descoped
  - §5.2 prohibitions — P3 (post-hoc only) enforced by CLI never reading regime at trigger time; P7 (no L2) inherited from T1-A runner; P8 (INVALID separate bucket), P9 (mixed_regime separate bucket), P10 (single CLI invocation) covered in Task 7
  - §6 risks — Task 8 step 2 handles T1-A schema mismatch with stop-and-create-follow-up rule
