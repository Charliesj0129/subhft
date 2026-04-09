"""L5 bid/ask data extraction for alpha backtesting.

Reads golden parquet files (which already contain L5 arrays) and converts
them to a compact .npy structured array format optimized for alpha signal
replay. Each row represents one BidAsk snapshot with 5-level depth.

Output dtype per row:
    timestamp_ns  : int64   — exchange timestamp (nanoseconds)
    bids_price    : int64[5] — bid prices at L1..L5 (x10000 scaled)
    bids_vol      : int64[5] — bid volumes at L1..L5
    asks_price    : int64[5] — ask prices at L1..L5 (x10000 scaled)
    asks_vol      : int64[5] — ask volumes at L1..L5

Price convention:
    Parquet stores price_scaled where price_scaled / 1_000_000 = NTD.
    Output uses x10000: price_x10000 = price_scaled // 100.

Usage:
    uv run python -m research.tools.extract_l5_data \\
        --symbols 2330,2317,TXFD6 \\
        --out research/data/l5/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import structlog

_HERE = Path(__file__).resolve()
_RESEARCH_ROOT = _HERE.parent.parent
_PROJECT_ROOT = _RESEARCH_ROOT.parent
for _p in (_PROJECT_ROOT, _RESEARCH_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

logger = structlog.get_logger(__name__)

_N_LEVELS: int = 5
_CH_PRICE_TO_X10000: int = 100  # price_scaled / 100 = x10000

GOLDEN_DIR = _RESEARCH_ROOT / "data" / "real" / "golden"

# Structured dtype for L5 backtesting data
L5_DTYPE = np.dtype(
    [
        ("timestamp_ns", "i8"),
        ("bids_price", "i8", (_N_LEVELS,)),
        ("bids_vol", "i8", (_N_LEVELS,)),
        ("asks_price", "i8", (_N_LEVELS,)),
        ("asks_vol", "i8", (_N_LEVELS,)),
    ]
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _pad_or_trim(arr: list[int], n: int) -> list[int]:
    """Ensure array has exactly n elements, zero-padding if short."""
    if len(arr) >= n:
        return arr[:n]
    return arr + [0] * (n - len(arr))


def extract_l5_from_parquet(
    symbol: str,
    golden_dir: Path = GOLDEN_DIR,
    output_dir: Path | None = None,
    min_levels: int = 3,
) -> Path | None:
    """Extract L5 bid/ask data from golden parquet files for one symbol.

    Args:
        symbol: Instrument symbol directory name (e.g. "2330", "TXFD6").
        golden_dir: Root of golden parquet data.
        output_dir: Where to write output .npy + .meta.json.
        min_levels: Minimum number of bid/ask levels required to include row.

    Returns:
        Path to written .npy file, or None if no data found.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise ImportError("pyarrow required: pip install pyarrow") from exc

    sym_dir = golden_dir / symbol
    if not sym_dir.is_dir():
        logger.warning("symbol_dir_not_found", symbol=symbol, path=str(sym_dir))
        return None

    parquet_files = sorted(sym_dir.glob("*.parquet"))
    if not parquet_files:
        logger.warning("no_parquet_files", symbol=symbol)
        return None

    if output_dir is None:
        output_dir = _RESEARCH_ROOT / "data" / "l5"
    output_dir.mkdir(parents=True, exist_ok=True)

    log = logger.bind(symbol=symbol, n_files=len(parquet_files))
    log.info("extracting_l5_data")

    chunks: list[np.ndarray] = []
    dates_used: list[str] = []

    for pf in parquet_files:
        table = pq.read_table(str(pf))
        date_str = pf.stem

        if table.num_rows == 0:
            continue

        # Vectorized filter: BidAsk type only
        types = table.column("type").to_pylist()
        mask = [t == "BidAsk" for t in types]
        if not any(mask):
            continue

        import pyarrow.compute as pc  # noqa: E402

        ba_table = table.filter(mask)
        n = ba_table.num_rows
        if n == 0:
            continue

        dates_used.append(date_str)

        # Extract columns as Python lists (vectorized, much faster than row-by-row)
        ts_col = ba_table.column("exch_ts").to_pylist()
        bp_col = ba_table.column("bids_price").to_pylist()
        bv_col = ba_table.column("bids_vol").to_pylist()
        ap_col = ba_table.column("asks_price").to_pylist()
        av_col = ba_table.column("asks_vol").to_pylist()

        # Build chunk array
        chunk = np.zeros(n, dtype=L5_DTYPE)
        valid_mask = np.ones(n, dtype=bool)

        for i in range(n):
            bp = bp_col[i]
            ap = ap_col[i]
            if bp is None or ap is None:
                valid_mask[i] = False
                continue
            if len(bp) < min_levels or len(ap) < min_levels:
                valid_mask[i] = False
                continue

            chunk[i]["timestamp_ns"] = ts_col[i]
            bp_padded = _pad_or_trim(bp, _N_LEVELS)
            bv_padded = _pad_or_trim(bv_col[i] or [], _N_LEVELS)
            ap_padded = _pad_or_trim(ap, _N_LEVELS)
            av_padded = _pad_or_trim(av_col[i] or [], _N_LEVELS)
            chunk[i]["bids_price"] = [p // _CH_PRICE_TO_X10000 for p in bp_padded]
            chunk[i]["bids_vol"] = bv_padded
            chunk[i]["asks_price"] = [p // _CH_PRICE_TO_X10000 for p in ap_padded]
            chunk[i]["asks_vol"] = av_padded

        chunk = chunk[valid_mask]
        if len(chunk) > 0:
            chunks.append(chunk)

        log.info("file_processed", file=pf.name, rows_in=n, rows_out=len(chunk))

    if not chunks:
        log.warning("no_l5_rows_found")
        return None

    # Concatenate and sort by timestamp
    arr = np.concatenate(chunks)
    arr.sort(order="timestamp_ns")

    # Write .npy
    npy_path = output_dir / f"{symbol}_l5.npy"
    np.save(str(npy_path), arr)

    # Write metadata sidecar
    fingerprint = _sha256_file(npy_path)
    meta: dict[str, Any] = {
        "source_type": "real",
        "owner": os.environ.get("USER", "unknown"),
        "symbols": [symbol],
        "data_ul": 3,
        "created_at": _now_iso(),
        "source_dir": str(golden_dir / symbol),
        "dates_used": dates_used,
        "n_trading_days": len(dates_used),
        "row_count": len(arr),
        "data_fingerprint": fingerprint,
        "generator_script": "research/tools/extract_l5_data.py",
        "dataset_id": npy_path.stem,
        "schema_version": "1",
        "rows": len(arr),
        "fields": list(L5_DTYPE.names),
        "dtype_description": {
            "timestamp_ns": "exchange timestamp (nanoseconds)",
            "bids_price": "bid prices L1-L5 (x10000 scaled int)",
            "bids_vol": "bid volumes L1-L5",
            "asks_price": "ask prices L1-L5 (x10000 scaled int)",
            "asks_vol": "ask volumes L1-L5",
        },
        "price_convention": "x10000 (price_scaled / 100)",
        "n_levels": _N_LEVELS,
        "min_levels_filter": min_levels,
        "rng_seed": None,
        "generator_version": "extract_l5_v1",
        "parameters": {"symbol": symbol, "min_levels": min_levels},
    }
    meta_path = npy_path.parent / (npy_path.name + ".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")

    log.info(
        "l5_extraction_complete",
        rows=len(arr),
        n_days=len(dates_used),
        path=str(npy_path),
    )
    return npy_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract L5 bid/ask data from golden parquet for alpha backtesting.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--symbols",
        default="2330,2317,TXFD6",
        help="Comma-separated symbol list",
    )
    parser.add_argument(
        "--out",
        default="research/data/l5/",
        help="Output directory for .npy and .meta.json",
    )
    parser.add_argument(
        "--golden-dir",
        default=str(GOLDEN_DIR),
        help="Golden parquet data root",
    )
    parser.add_argument(
        "--min-levels",
        type=int,
        default=3,
        help="Minimum bid/ask levels to include a row",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    output_dir = Path(args.out)
    golden_dir = Path(args.golden_dir)

    results: list[tuple[str, Path | None]] = []
    for sym in symbols:
        try:
            path = extract_l5_from_parquet(
                symbol=sym,
                golden_dir=golden_dir,
                output_dir=output_dir,
                min_levels=args.min_levels,
            )
            results.append((sym, path))
        except Exception as exc:
            print(f"[extract_l5] ERROR ({sym}): {exc}", file=sys.stderr)
            results.append((sym, None))

    print("\n[extract_l5] Summary:")
    for sym, path in results:
        if path:
            arr = np.load(str(path))
            print(f"  {sym}: {len(arr)} rows -> {path}")
        else:
            print(f"  {sym}: FAILED or no data")

    return 0 if all(p for _, p in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
