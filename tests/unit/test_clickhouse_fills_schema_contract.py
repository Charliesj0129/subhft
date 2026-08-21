"""The recorder's fill record must fit the table the migrations actually build.

On 2026-04-25, `20260425_001_fills_replacing_merge_tree.sql` rebuilt `hft.fills`
as a ReplacingMergeTree by CREATE-ing `hft.fills_new` with the twelve columns
from the *initial* schema, then RENAME-ing over the original. That silently
reverted three ALTERs that had already been applied and recorded in
`hft.schema_migrations`:

    20260325_002  tax_scaled
    20260327_002  decision_price, arrival_price
    20260330_001  instrument_type, oc_type

`recorder/mapper.py` emits all five. Every fills insert therefore failed with
`Unrecognized column 'tax_scaled' in table hft.fills` and the loader DLQ'd the
batch. It stayed invisible for four months because no fill was reaching the
loader at all -- the shioaji 1.5.x callback payload was being dropped further
upstream -- so the first fill to get through was the first to expose it.

These tests replay the migration directory the way the runner does and compare
the resulting column set against what the mapper writes, so a future rebuild
that forgets a prior ALTER fails here instead of in production.
"""

from __future__ import annotations

import re
from pathlib import Path

from hft_platform.recorder.schema import _extract_up_statements

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "hft_platform" / "migrations" / "clickhouse"

_CREATE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)\s*\((.*)", re.IGNORECASE | re.DOTALL)
_ALTER_RE = re.compile(r"ALTER\s+TABLE\s+([\w.]+)\s+(.*)", re.IGNORECASE | re.DOTALL)
_ADD_COL_RE = re.compile(r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_DROP_COL_RE = re.compile(r"DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_RENAME_RE = re.compile(r"RENAME\s+TABLE\s+(.*)", re.IGNORECASE | re.DOTALL)
_DROP_TABLE_RE = re.compile(r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([\w.]+)", re.IGNORECASE)


def _create_columns(body: str) -> list[str]:
    """Column names from a CREATE TABLE body, ignoring nested type parens."""
    depth = 0
    current: list[str] = []
    parts: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))

    names: list[str] = []
    for part in parts:
        token = part.strip().split()
        if not token:
            continue
        head = token[0]
        # Skip table-level clauses that can appear inside the parenthesis.
        if head.upper() in {"INDEX", "PRIMARY", "ORDER", "PARTITION", "CONSTRAINT", "PROJECTION"}:
            continue
        if re.fullmatch(r"\w+", head):
            names.append(head)
    return names


def _replay_migrations() -> dict[str, set[str]]:
    """Apply every migration's Up section and return {table: columns}."""
    tables: dict[str, set[str]] = {}
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        for stmt in _extract_up_statements(path.read_text(encoding="utf-8")):
            stripped = stmt.strip()
            if (m := _CREATE_RE.match(stripped)) is not None:
                tables.setdefault(m.group(1), set()).update(_create_columns(m.group(2)))
                continue
            if (m := _DROP_TABLE_RE.match(stripped)) is not None:
                tables.pop(m.group(1), None)
                continue
            if (m := _RENAME_RE.match(stripped)) is not None:
                for pair in m.group(1).split(","):
                    bits = re.split(r"\s+TO\s+", pair.strip(), flags=re.IGNORECASE)
                    if len(bits) == 2:
                        src, dst = bits[0].strip(), bits[1].strip().rstrip(";")
                        if src in tables:
                            tables[dst] = tables.pop(src)
                continue
            if (m := _ALTER_RE.match(stripped)) is not None:
                table, rest = m.group(1), m.group(2)
                cols = tables.setdefault(table, set())
                cols.update(_ADD_COL_RE.findall(rest))
                cols.difference_update(_DROP_COL_RE.findall(rest))
    return tables


# The keys recorder/mapper.py writes into a ("fills", record) tuple. Kept as a
# literal so a mapper change that adds a key without a migration fails here.
MAPPER_FILL_KEYS = {
    "ts_exchange",
    "ts_local",
    "client_order_id",
    "broker_order_id",
    "fill_id",
    "strategy_id",
    "symbol",
    "side",
    "qty",
    "price_scaled",
    "fee_scaled",
    "tax_scaled",
    "decision_price",
    "arrival_price",
    "source",
    "instrument_type",
    "oc_type",
}


def test_mapper_fill_keys_all_exist_in_the_migrated_fills_table() -> None:
    tables = _replay_migrations()
    assert "hft.fills" in tables, f"migration replay produced no hft.fills; got {sorted(tables)}"
    missing = MAPPER_FILL_KEYS - tables["hft.fills"]
    assert not missing, (
        "recorder/mapper.py writes columns the migrations do not create on hft.fills: "
        f"{sorted(missing)}. ClickHouse reports only the first offender per insert, so a "
        "single missing column silently DLQs every fill batch."
    )


def test_mapper_fill_keys_match_the_module_it_documents() -> None:
    """MAPPER_FILL_KEYS must stay in step with mapper.py's actual record."""
    source = (Path(__file__).resolve().parents[2] / "src" / "hft_platform" / "recorder" / "mapper.py").read_text(
        encoding="utf-8"
    )
    start = source.index("fill_record: Dict[str, Any] = {")
    end = source.index('return ("fills", fill_record)', start)
    literal_keys = set(re.findall(r'^\s{12}"(\w+)":', source[start:end], re.MULTILINE))
    assert literal_keys == MAPPER_FILL_KEYS, (
        "mapper.py's fill record and this test's expected key set have diverged: "
        f"only in mapper {sorted(literal_keys - MAPPER_FILL_KEYS)}, "
        f"only in test {sorted(MAPPER_FILL_KEYS - literal_keys)}"
    )


def test_a_table_rebuild_cannot_silently_drop_a_previously_added_column() -> None:
    """The specific 2026-04-25 regression, named.

    `20260425_001` recreated hft.fills from the initial twelve columns. If a
    future rebuild does the same, these five disappear again.
    """
    tables = _replay_migrations()
    reverted_by_the_rmt_rebuild = {"tax_scaled", "decision_price", "arrival_price", "instrument_type", "oc_type"}
    missing = reverted_by_the_rmt_rebuild - tables.get("hft.fills", set())
    assert not missing, (
        f"columns lost again by a table rebuild: {sorted(missing)}. A migration that "
        "recreates a table must carry forward every ALTER already applied to it."
    )
