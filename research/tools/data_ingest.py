"""WU10: Real market data ingestion tool.

Ingests tick data from ClickHouse and converts it to governed .npy format
with UL3+ compliant metadata sidecar.

Price convention:
    ClickHouse stores price_scaled where price_scaled / 1_000_000 = NTD price.
    Output .npy uses x10000 scaled integers:
        price_x10000 = price_scaled / 100   (i.e. price_scaled * 10000 / 1_000_000)
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

# ---------------------------------------------------------------------------
# sys.path bootstrap so this module is importable as research.tools.data_ingest
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_RESEARCH_ROOT = _HERE.parent.parent
_PROJECT_ROOT = _RESEARCH_ROOT.parent
for _p in (_PROJECT_ROOT, _RESEARCH_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

logger = structlog.get_logger(__name__)

# ClickHouse price scaling constant (matches recorder/mapper.py CLICKHOUSE_PRICE_SCALE)
_CH_PRICE_SCALE: float = 1_000_000.0
# Target .npy price scale (x10000 convention per CLAUDE.md)
_NPY_PRICE_SCALE: int = 10_000

# LOB numpy dtype for ingested real data
_LOB_DTYPE = np.dtype(
    [
        ("timestamp_ns", "i8"),
        ("price", "i8"),
        ("volume", "i8"),
        ("bid_price", "i8"),
        ("bid_volume", "i8"),
        ("ask_price", "i8"),
        ("ask_volume", "i8"),
        ("side", "U4"),
    ]
)

_QUERY_TEMPLATE = """\
SELECT
    toUnixTimestamp64Nano(timestamp) AS timestamp_ns,
    price,
    volume,
    side,
    bid_price,
    bid_volume,
    ask_price,
    ask_volume
FROM hft.market_data
WHERE symbol = {symbol:String}
  AND timestamp >= parseDateTimeBestEffort({start:String})
  AND timestamp < parseDateTimeBestEffort({end:String})
ORDER BY timestamp
"""


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ch_price_to_x10000(price_scaled: int) -> int:
    """Convert ClickHouse price_scaled to x10000 int convention.

    ClickHouse: price_scaled / 1_000_000 = NTD
    Target:     price_x10000 / 10_000   = NTD
    Therefore:  price_x10000 = price_scaled / 100
    """
    return int(price_scaled) // 100


def _build_meta(
    *,
    symbol: str,
    start: str,
    end: str,
    clickhouse_host: str,
    row_count: int,
    data_fingerprint: str,
    output_path: Path,
) -> dict[str, Any]:
    owner = os.environ.get("USER", "unknown")
    return {
        "source_type": "real",
        "owner": owner,
        "symbols": [symbol],
        "data_ul": 3,
        "created_at": _now_iso(),
        "clickhouse_host": clickhouse_host,
        "date_range": [start, end],
        "row_count": row_count,
        "data_fingerprint": data_fingerprint,
        "generator_script": "research/tools/data_ingest.py",
        # UL2 required fields
        "dataset_id": output_path.stem,
        "schema_version": "1",
        "rows": row_count,
        "fields": list(_LOB_DTYPE.names),
        # UL3 required fields
        "rng_seed": None,
        "generator_version": "real_data_v1",
        "parameters": {"symbol": symbol, "start": start, "end": end},
    }


def ingest_from_clickhouse(
    symbol: str,
    date_range: tuple[str, str],
    output_dir: str,
    clickhouse_host: str = "localhost",
) -> Path:
    """Ingest real market data from ClickHouse and save as governed .npy.

    Args:
        symbol: Instrument symbol, e.g. "TXFC6".
        date_range: (start_date, end_date) as ISO date strings ("2026-03-01").
        output_dir: Directory where output .npy and .npy.meta.json are written.
        clickhouse_host: ClickHouse hostname or IP.

    Returns:
        Path to the written .npy file.
    """
    try:
        import clickhouse_connect  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ImportError(
            "clickhouse-connect is required for data ingestion. "
            "Install with: pip install clickhouse-connect"
        ) from exc

    start, end = date_range
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    npy_filename = f"{symbol}_{start}_{end}.npy"
    output_path = out_dir / npy_filename
    meta_path = output_path.parent / (output_path.name + ".meta.json")

    log = logger.bind(symbol=symbol, start=start, end=end, clickhouse_host=clickhouse_host)
    log.info("connecting_to_clickhouse")

    client = clickhouse_connect.get_client(host=clickhouse_host)

    log.info("executing_query")
    try:
        result = client.query(
            _QUERY_TEMPLATE,
            parameters={"symbol": symbol, "start": start, "end": end},
        )
    finally:
        client.close()

    rows = result.result_rows
    row_count = len(rows)
    log.info("query_complete", row_count=row_count)

    if row_count == 0:
        raise ValueError(
            f"No data returned for symbol={symbol!r} in range [{start}, {end}). "
            "Check symbol name, date range, and ClickHouse connectivity."
        )

    # Build structured numpy array
    arr = np.empty(row_count, dtype=_LOB_DTYPE)
    for i, row in enumerate(rows):
        (ts_ns, price, volume, side, bid_price, bid_volume, ask_price, ask_volume) = row
        arr[i]["timestamp_ns"] = int(ts_ns)
        arr[i]["price"] = _ch_price_to_x10000(int(price))
        arr[i]["volume"] = int(volume)
        arr[i]["side"] = str(side)[:4]
        arr[i]["bid_price"] = _ch_price_to_x10000(int(bid_price)) if bid_price is not None else 0
        arr[i]["bid_volume"] = int(bid_volume) if bid_volume is not None else 0
        arr[i]["ask_price"] = _ch_price_to_x10000(int(ask_price)) if ask_price is not None else 0
        arr[i]["ask_volume"] = int(ask_volume) if ask_volume is not None else 0

    np.save(str(output_path), arr)
    log.info("npy_written", path=str(output_path), rows=row_count)

    fingerprint = _sha256_file(output_path)
    meta = _build_meta(
        symbol=symbol,
        start=start,
        end=end,
        clickhouse_host=clickhouse_host,
        row_count=row_count,
        data_fingerprint=fingerprint,
        output_path=output_path,
    )
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    log.info("meta_written", path=str(meta_path), data_ul=meta["data_ul"])

    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest real market data from ClickHouse to governed .npy format (UL3+).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--symbol", required=True, help="Instrument symbol, e.g. TXFC6")
    parser.add_argument("--start", required=True, help="Start date inclusive, ISO format (2026-03-01)")
    parser.add_argument("--end", required=True, help="End date exclusive, ISO format (2026-03-10)")
    parser.add_argument(
        "--out",
        default="research/data/processed/",
        help="Output directory for .npy and .npy.meta.json",
    )
    parser.add_argument(
        "--clickhouse-host",
        default="localhost",
        help="ClickHouse host",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    try:
        output_path = ingest_from_clickhouse(
            symbol=args.symbol,
            date_range=(args.start, args.end),
            output_dir=args.out,
            clickhouse_host=args.clickhouse_host,
        )
    except (ValueError, ImportError, OSError) as exc:
        print(f"[data_ingest] ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"[data_ingest] OK: {output_path}")
    print(f"[data_ingest] meta: {output_path}.meta.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
