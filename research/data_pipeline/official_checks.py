"""Integrity gates over the parsed official TAIFEX tick files (``parsed/<dataset>/<day>.parquet``).

The parser already guarantees the shape of a row; these gates check the *content* and that the
``.summary.json`` written next to each day still describes the Parquet beside it. Everything is
read-only and offline: nothing here fetches, parses a zip, or writes into the data root.

Findings carry a severity. ``fail`` makes the run fail; ``info`` is reported and never fails.

Scope rules, each established on the 19 local ``fut_ticks`` days (13,480,206 rows):

* Whole-point prices are required only of the index futures (TX, MTX, TMF by default) and only on
  single legs. 281 other products, mostly stock futures, quote fractional prices on purpose, and a
  calendar-spread row carries a price *difference* that is negative on 39,414 rows.
* Option premiums may be fractional, so ``opt_ticks`` gets the summary reconciliation only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

INDEX_FUTURES: tuple[str, ...] = ("TX", "MTX", "TMF")
CHECKED_DATASETS: tuple[str, ...] = ("fut_ticks", "opt_ticks")
SEVERITY_FAIL = "fail"
SEVERITY_INFO = "info"
MAX_GROUPS_IN_DETAIL = 5
SUMMARY_SUFFIX = ".summary.json"

_REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "fut_ticks": ("product", "expiry", "session", "price", "contracts", "spread"),
    "opt_ticks": ("product", "expiry", "session", "price", "contracts"),
}
_GroupKey = tuple[str, str, str, bool]


@dataclass(frozen=True)
class Finding:
    day: str
    check: str
    severity: str
    rows: int
    detail: str


def _count(mask: Any) -> int:
    import pyarrow as pa
    import pyarrow.compute as pc

    return int(pc.sum(pc.cast(pc.fill_null(mask, False), pa.int64())).as_py() or 0)


def _fail(day: str, check: str, rows: int, detail: str) -> Finding:
    return Finding(day, check, SEVERITY_FAIL, rows, detail)


def _info(day: str, check: str, rows: int, detail: str) -> Finding:
    return Finding(day, check, SEVERITY_INFO, rows, detail)


def _read_summary(path: Path) -> dict[_GroupKey, tuple[int, int]] | str:
    """``{(product, expiry, session, spread): (contracts, trades)}``, or a reason it cannot be read."""
    try:
        body = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        return f"{path.name}: {exc}"
    if not isinstance(body, list):
        return f"{path.name}: expected a JSON list, got {type(body).__name__}"
    groups: dict[_GroupKey, tuple[int, int]] = {}
    for item in body:
        try:
            key = (str(item["product"]), str(item["expiry"]), str(item["session"]), bool(item["spread"]))
            groups[key] = (int(item["contracts"]), int(item["trades"]))
        except (KeyError, TypeError, ValueError):
            return f"{path.name}: a row lacks product, expiry, session, spread, contracts or trades"
    return groups


def _parquet_groups(table: Any) -> dict[_GroupKey, tuple[int, int]]:
    import pyarrow as pa
    import pyarrow.compute as pc

    if "spread" not in table.schema.names:
        table = table.append_column("spread", pa.array([False] * table.num_rows, type=pa.bool_()))
    aggregated = table.group_by(["product", "expiry", "session", "spread"]).aggregate(
        [("contracts", "sum"), ("contracts", "count", pc.CountOptions(mode="all"))]
    )
    groups: dict[_GroupKey, tuple[int, int]] = {}
    for row in aggregated.to_pylist():
        key = (row["product"], row["expiry"], row["session"], bool(row["spread"]))
        groups[key] = (int(row["contracts_sum"] or 0), int(row["contracts_count"]))
    return groups


def _describe(key: _GroupKey) -> str:
    product, expiry, session, spread = key
    return f"{product} {expiry} {session}{' spread' if spread else ''}"


def _reconcile(day: str, table: Any, summary_path: Path) -> list[Finding]:
    if not summary_path.exists():
        return [_fail(day, "summary_missing", 1, f"{summary_path.name} does not exist")]
    expected = _read_summary(summary_path)
    if isinstance(expected, str):
        return [_fail(day, "summary_unreadable", 1, expected)]
    actual = _parquet_groups(table)
    problems: list[str] = []
    for key in sorted(actual.keys() | expected.keys()):
        if key not in expected:
            problems.append(f"{_describe(key)}: in the parquet, not in the summary")
        elif key not in actual:
            problems.append(f"{_describe(key)}: in the summary, not in the parquet")
        elif actual[key] != expected[key]:
            parts = []
            if actual[key][0] != expected[key][0]:
                parts.append(f"contracts parquet {actual[key][0]} vs summary {expected[key][0]}")
            if actual[key][1] != expected[key][1]:
                parts.append(f"trades parquet {actual[key][1]} vs summary {expected[key][1]}")
            problems.append(f"{_describe(key)}: {', '.join(parts)}")
    if not problems:
        return []
    shown = "; ".join(problems[:MAX_GROUPS_IN_DETAIL])
    more = len(problems) - MAX_GROUPS_IN_DETAIL
    return [_fail(day, "summary_mismatch", len(problems), shown + (f"; +{more} more" if more > 0 else ""))]


def _content_gates(day: str, table: Any, products: Sequence[str]) -> tuple[list[Finding], int]:
    """Price and contract gates for the futures dataset. Returns the findings and the in-scope row count."""
    import pyarrow as pa
    import pyarrow.compute as pc

    scoped = pc.is_in(table["product"], value_set=pa.array(list(products), type=pa.string()))
    in_scope = _count(scoped)
    findings: list[Finding] = []
    if in_scope == 0:
        findings.append(
            _info(
                day,
                "no_in_scope_rows",
                0,
                f"no rows for {', '.join(products)}; the price and contract gates did not run",
            )
        )
        return findings, 0

    price, contracts, spread = table["price"], table["contracts"], pc.fill_null(table["spread"], False)
    single_leg = pc.and_(scoped, pc.invert(spread))
    whole = pc.and_(pc.is_finite(price), pc.equal(price, pc.floor(price)))

    def add(check: str, mask: Any, detail: str, severity: str = SEVERITY_FAIL) -> None:
        rows = _count(mask)
        if rows:
            findings.append(Finding(day, check, severity, rows, detail))

    add("price_missing", pc.and_(scoped, pc.is_null(price)), "price is null on an in-scope row")
    add(
        "non_integer_price",
        pc.and_(single_leg, pc.invert(pc.fill_null(whole, True))),
        "single-leg price is not a whole index point",
    )
    add("price_not_positive", pc.and_(single_leg, pc.less_equal(price, 0)), "single-leg price is zero or negative")
    add("contracts_missing", pc.and_(scoped, pc.is_null(contracts)), "contracts is null on an in-scope row")
    add("contracts_not_positive", pc.and_(scoped, pc.less_equal(contracts, 0)), "contracts is zero or negative")
    add(
        "negative_spread_price",
        pc.and_(pc.and_(scoped, spread), pc.less(price, 0)),
        "calendar-spread rows quote a price difference, which can be negative",
        SEVERITY_INFO,
    )
    return findings, in_scope


def check_day(
    root: Path, dataset: str, day: str, *, products: Sequence[str] = INDEX_FUTURES
) -> tuple[list[Finding], int, int]:
    """Gate one day. Returns ``(findings, rows_checked, in_scope_rows)``."""
    import pyarrow.parquet as pq

    folder = root / "parsed" / dataset
    parquet_path, summary_path = folder / f"{day}.parquet", folder / f"{day}{SUMMARY_SUFFIX}"
    if not parquet_path.exists():
        summary = _read_summary(summary_path) if summary_path.exists() else {}
        if isinstance(summary, dict) and not summary:
            return [_info(day, "empty_day", 0, "no parquet and an empty summary: the day held no ticks")], 0, 0
        return [_fail(day, "parquet_missing", 1, f"{parquet_path.name} does not exist but the summary does")], 0, 0

    table = pq.read_table(parquet_path)
    missing = [name for name in _REQUIRED_COLUMNS[dataset] if name not in table.schema.names]
    if missing:
        return [_fail(day, "schema", 1, f"missing columns: {', '.join(missing)}")], table.num_rows, 0

    findings: list[Finding] = []
    in_scope = table.num_rows
    if dataset == "fut_ticks":
        findings, in_scope = _content_gates(day, table, products)
    findings.extend(_reconcile(day, table, summary_path))
    return findings, table.num_rows, in_scope


def _days_on_disk(folder: Path) -> list[str]:
    names: set[str] = set()
    for path in folder.glob("*"):
        if path.name.endswith(SUMMARY_SUFFIX):
            names.add(path.name.removesuffix(SUMMARY_SUFFIX))
        elif path.suffix == ".parquet":
            names.add(path.stem)
    return sorted(names)


def check_dataset(
    root: Path,
    dataset: str,
    *,
    day: str | None = None,
    products: Iterable[str] = INDEX_FUTURES,
) -> dict[str, Any]:
    """Gate every parsed day of ``dataset`` (or only ``day``). Raises ``ValueError`` for bad input."""
    if dataset not in CHECKED_DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; check supports {', '.join(CHECKED_DATASETS)}")
    scope = tuple(products)
    if dataset == "fut_ticks" and not scope:
        raise ValueError("no products given; name at least one, e.g. --products TX,MTX,TMF")
    days = _days_on_disk(root / "parsed" / dataset) if (root / "parsed" / dataset).is_dir() else []
    if not days:
        raise ValueError(f"no parsed {dataset} under {root / 'parsed' / dataset}; run parse --dataset {dataset} first")
    if day is not None:
        if day not in days:
            raise ValueError(f"no parsed {dataset} for {day}; run parse --dataset {dataset} first")
        days = [day]

    findings: list[Finding] = []
    rows_checked = in_scope_rows = 0
    for name in days:
        day_findings, rows, in_scope = check_day(root, dataset, name, products=scope)
        findings.extend(day_findings)
        rows_checked += rows
        in_scope_rows += in_scope
    return {
        "dataset": dataset,
        "products": list(scope) if dataset == "fut_ticks" else [],
        "days_checked": len(days),
        "rows_checked": rows_checked,
        "in_scope_rows": in_scope_rows,
        "failed": any(item.severity == SEVERITY_FAIL for item in findings),
        "findings": [asdict(item) for item in findings],
    }
