"""Generate q_hat parquet fixtures for TMFD6 / TXFD6 / TXO (Slice B Task 7).

Pulls 5+ trading days per symbol from ClickHouse, persists each day's events
as a small parquet under ``tests/fixtures/q_hat_calibration/`` (committed for
Task 8 reproducibility), and runs the Slice B Task 6 calibration harness
against an in-memory ``LocalReplayChSource`` to produce the per-symbol
``q_hat`` parquets under ``research/backtest/q_hat_data/``.

Why both fixture sets?
----------------------
- ``research/backtest/q_hat_data/<symbol>_q_hat.parquet`` is the artefact that
  Task 8 wires into ``QueueDepletionFill``. Production replay uses these.
- ``tests/fixtures/q_hat_calibration/<symbol>_<date>_replay_actual.parquet``
  contains the raw event arrays the calibration was *derived from*. Committing
  these lets the Task 8 integration test re-derive the q_hat tables
  deterministically from committed fixtures alone (no live CK required).

Down-sampling of fixtures
-------------------------
The committed per-day fixtures keep only the four columns the calibration
harness reads (``ev``, ``exch_ts``, ``px``, ``qty``). Calibration only depends
on these; dropping ``local_ts`` / ``order_id`` / ``ival`` / ``fval`` keeps the
total fixture footprint inside the 50 MB plan budget.

Invocation
----------
    CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2) \\
        PYTHONPATH=. uv run python scripts/generate_q_hat_fixtures.py

Determinism
-----------
The same input fixtures + same harness produce identical outputs. The harness
is deterministic and contains no randomness, so re-runs against the committed
fixtures regenerate the same q_hat parquets bit-for-bit (pyarrow ordering
aside).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# Make ``research/`` and ``src/`` importable when invoked as a script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from hft_platform.backtest.ch_data_source import ChDataSource  # noqa: E402
from research.backtest.calibrate_queue_fill import calibrate  # noqa: E402

# ---------------------------------------------------------------------------
# Inputs: 5 days per symbol. Inventory verified via ClickHouse on 2026-05-05.
# Selection rationale:
#   - TMFD6 / TXFD6: pick mid-volume trading days (avoid the 1.3M-event 04-02
#     spike to keep fixtures inside the 50 MB total budget) but with enough
#     events that ``MIN_ATTEMPTS_PER_CELL=30`` saturates most active hours.
#   - TXO35500E6: most-active April option contract (~2 weeks pre-expiry).
#     Plan §6 Task 7 expects TXO to drop more cells than the futures.
# ---------------------------------------------------------------------------
SYMBOL_DATES: dict[str, list[str]] = {
    "TMFD6": [
        "2026-04-08",
        "2026-04-09",
        "2026-04-10",
        "2026-04-13",
        "2026-04-14",
    ],
    "TXFD6": [
        "2026-04-08",
        "2026-04-09",
        "2026-04-10",
        "2026-04-13",
        "2026-04-14",
    ],
    # TXO35000Q6 chosen as the most active option contract with at least 5
    # days carrying real trade ticks. Trade activity is sparse compared to
    # futures (hundreds vs hundreds-of-thousands) — Plan §6 Task 7 expects
    # cells_dropped to dominate for TXO; we still include it so Task 8 can
    # exercise the multi-symbol q_hat lookup path. Days were chosen to
    # maximise tick coverage within the available inventory (sampled
    # 2026-05-05).
    "TXO35000Q6": [
        "2026-04-17",
        "2026-04-22",
        "2026-04-28",
        "2026-04-29",
        "2026-04-30",
    ],
}

# Where to write outputs.
Q_HAT_DIR = _REPO_ROOT / "research" / "backtest" / "q_hat_data"
FIXTURE_DIR = _REPO_ROOT / "tests" / "fixtures" / "q_hat_calibration"

# Calibration columns. The harness only reads these four fields off each row.
_CALIB_DTYPE = np.dtype(
    [
        ("ev", "u8"),
        ("exch_ts", "i8"),
        ("px", "f8"),
        ("qty", "f8"),
    ]
)


def _project_calibration_columns(events: np.ndarray) -> np.ndarray:
    """Return a structured array containing only the calibration columns.

    ``calibrate_queue_fill._ingest_day`` reads exactly these fields per row;
    keeping only them shrinks the committed fixture by ~50%.
    """
    out = np.empty(len(events), dtype=_CALIB_DTYPE)
    out["ev"] = events["ev"]
    out["exch_ts"] = events["exch_ts"]
    out["px"] = events["px"]
    out["qty"] = events["qty"]
    return out


def _write_fixture_parquet(out_path: Path, events: np.ndarray) -> None:
    """Persist a calibration-column structured array as a small parquet.

    Uses ``zstd`` level 9 for ~30% smaller files than snappy on these arrays
    (probed locally on 2026-05-05). Parquet is the project-standard committed
    fixture format (see ``tests/fixtures/maker_engine_pre_mtm_baseline/``).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "ev": events["ev"],
            "exch_ts": events["exch_ts"],
            "px": events["px"],
            "qty": events["qty"],
        }
    )
    pq.write_table(table, out_path, compression="zstd", compression_level=9)


def _read_fixture_parquet(in_path: Path) -> np.ndarray:
    """Reverse of ``_write_fixture_parquet``. Returns a structured array.

    The harness only needs the four calibration columns; we materialise them
    in a numpy structured array so ``row["ev"]`` / ``row["exch_ts"]`` / etc.
    work unchanged.
    """
    table = pq.read_table(in_path)
    n = table.num_rows
    out = np.empty(n, dtype=_CALIB_DTYPE)
    out["ev"] = table.column("ev").to_numpy()
    out["exch_ts"] = table.column("exch_ts").to_numpy()
    out["px"] = table.column("px").to_numpy()
    out["qty"] = table.column("qty").to_numpy()
    return out


class LocalReplayChSource:
    """``ChDataSourceLike`` adapter over the committed per-day fixtures.

    Implements ``load_day(symbol, date) -> np.ndarray`` (Protocol from
    ``research/backtest/calibrate_queue_fill.py``) by reading the parquet
    written by ``_write_fixture_parquet``. This is the source the harness
    uses inside the generator — so the calibration is reproducible from the
    committed fixtures alone, no live CK access required.
    """

    def __init__(self, fixture_dir: Path) -> None:
        self._fixture_dir = fixture_dir

    def load_day(self, instrument: str, date: str) -> np.ndarray:
        path = self._fixture_dir / f"{instrument}_{date}_replay_actual.parquet"
        return _read_fixture_parquet(path)


def _summarise(symbol: str, result, n_dates: int) -> tuple[float, float, float]:
    """Print + return (min, max, mean) q_hat over the calibrated cells."""
    table_dict = result.table._data
    if not table_dict:
        print(
            f"  {symbol}: cells_calibrated={result.cells_calibrated} "
            f"cells_dropped={result.cells_dropped} (no q_hat values, all fall back)"
        )
        return float("nan"), float("nan"), float("nan")
    vals = list(table_dict.values())
    qmin, qmax = min(vals), max(vals)
    qmean = sum(vals) / len(vals)
    print(
        f"  {symbol}: n_dates={n_dates} "
        f"cells_calibrated={result.cells_calibrated} cells_dropped={result.cells_dropped} "
        f"q_hat min={qmin:.3f} max={qmax:.3f} mean={qmean:.3f}"
    )
    return qmin, qmax, qmean


def _ck_source_from_env() -> ChDataSource:
    """Build a ChDataSource using the platform's standard env-var conventions.

    Honours ``CLICKHOUSE_PASSWORD``; falls back to localhost defaults for the
    other connection params (Task 1 / scripts/a1_day_decomposition.py
    precedent).
    """
    return ChDataSource(
        ch_host=os.environ.get("CLICKHOUSE_HOST", "localhost"),
        ch_port=int(os.environ.get("CLICKHOUSE_PORT", "8123")),
        ch_user=os.environ.get("CLICKHOUSE_USER", "default"),
        ch_password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )


def step1_extract_fixtures(symbols_dates: dict[str, list[str]]) -> None:
    """Pull each (symbol, date) from CK and write the raw-replay fixture.

    Skips days whose fixture parquet already exists — re-runs are idempotent.
    """
    ck = _ck_source_from_env()
    for symbol, dates in symbols_dates.items():
        for date in dates:
            fx_path = FIXTURE_DIR / f"{symbol}_{date}_replay_actual.parquet"
            if fx_path.exists():
                print(f"[skip] {fx_path.relative_to(_REPO_ROOT)} already exists")
                continue
            print(f"[extract] {symbol} {date} -> CK ...")
            events = ck.load_day(symbol, date)
            calib_events = _project_calibration_columns(events)
            _write_fixture_parquet(fx_path, calib_events)
            size_mb = fx_path.stat().st_size / 1e6
            print(
                f"  rows={len(events):,} -> {fx_path.relative_to(_REPO_ROOT)} ({size_mb:.2f} MB)"
            )


def step2_run_calibration(symbols_dates: dict[str, list[str]]) -> None:
    """Run the harness against the committed fixtures and write q_hat parquets."""
    src = LocalReplayChSource(FIXTURE_DIR)
    print()
    print("=== Calibration summary ===")
    for symbol, dates in symbols_dates.items():
        out_path = Q_HAT_DIR / f"{symbol.lower()}_q_hat.parquet"
        result = calibrate(symbol, dates, out_path, ch_source=src)
        _summarise(symbol, result, len(dates))


def step3_report_total_size() -> None:
    """Print the total committed-fixture footprint so the commit body is auditable."""
    total = 0
    counts = {"q_hat": 0, "fixtures": 0}
    for p in Q_HAT_DIR.glob("*.parquet"):
        total += p.stat().st_size
        counts["q_hat"] += 1
    for p in FIXTURE_DIR.glob("*.parquet"):
        total += p.stat().st_size
        counts["fixtures"] += 1
    print()
    print(
        f"=== Total committed fixture footprint: {total / 1e6:.2f} MB "
        f"({counts['q_hat']} q_hat parquets + {counts['fixtures']} per-day fixtures) ==="
    )


def main() -> None:
    step1_extract_fixtures(SYMBOL_DATES)
    step2_run_calibration(SYMBOL_DATES)
    step3_report_total_size()


if __name__ == "__main__":
    main()
