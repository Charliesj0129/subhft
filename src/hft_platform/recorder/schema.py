from __future__ import annotations

import os
import re
from typing import Iterable

from structlog import get_logger

logger = get_logger("recorder.schema")

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "../migrations/clickhouse")

# Markers must start a line; trailing text on the marker line is allowed and
# discarded together with the marker. Without line anchoring, `-- Up: foo`
# would split as `: foo` and ClickHouse would reject the leading colon.
_MARKER_UP_RE = re.compile(r"^[ \t]*--[ \t]*Up\b.*$", re.MULTILINE)
_MARKER_DOWN_RE = re.compile(r"^[ \t]*--[ \t]*Down\b.*$", re.MULTILINE)


def _extract_up_statements(content: str) -> list[str]:
    """Return executable SQL statements from a migration file's Up section.

    Behavior:
      * If `-- Down` exists at start-of-line, drop everything from that point on.
      * If `-- Up` exists at start-of-line, keep only what follows the *last* Up marker.
      * If no markers exist, treat the whole content as the Up section.
      * `;` only terminates a statement OUTSIDE `--` comments (a `;` inside a
        comment used to split the file mid-comment and ship garbage to the
        server — e.g. 20260504_001's "ENABLED=1; otherwise table stays empty").
        `;` inside string literals is not handled; don't use it in migrations.
      * Statements containing only `--` comment lines or whitespace are filtered
        out (ClickHouse rejects them as `Empty query`).
    """
    down_match = _MARKER_DOWN_RE.search(content)
    if down_match:
        content = content[: down_match.start()]
    up_matches = list(_MARKER_UP_RE.finditer(content))
    if up_matches:
        content = content[up_matches[-1].end() :]

    out: list[str] = []
    buf: list[str] = []

    def _flush() -> None:
        stmt = "\n".join(buf).strip()
        buf.clear()
        if not stmt:
            return
        has_executable = any(line.strip() and not line.strip().startswith("--") for line in stmt.splitlines())
        if has_executable:
            out.append(stmt)

    for line in content.splitlines():
        code = line.split("--", 1)[0]
        if ";" not in code:
            buf.append(line)
            continue
        # Terminator line(s): the comment tail (if any) is dropped, which is
        # harmless — it annotated the statement that just ended.
        while ";" in code:
            head, _, code = code.partition(";")
            buf.append(head)
            _flush()
        if code.strip():
            buf.append(code)
    _flush()
    return out


def _init_migrations_table(client) -> None:
    """Create the schema_migrations table if it doesn't exist."""
    client.command("CREATE DATABASE IF NOT EXISTS hft")
    client.command("""
        CREATE TABLE IF NOT EXISTS hft.schema_migrations (
            version String,
            name String,
            applied_at DateTime DEFAULT now()
        ) ENGINE = MergeTree()
        ORDER BY version
    """)


def apply_schema(client, schema_path: str | None = None) -> None:
    """
    Run all unapplied ClickHouse schema migrations.
    This replaces the legacy behavior of running a single SQL file.
    """
    _init_migrations_table(client)

    # Get all applied migrations
    try:
        result = client.query("SELECT version FROM hft.schema_migrations")
        applied_versions = {row[0] for row in result.result_rows}
    except Exception as exc:
        logger.critical(
            "Failed to query schema_migrations — refusing to proceed to avoid re-running destructive migrations",
            error=str(exc),
        )
        raise RuntimeError("Cannot determine applied migrations; aborting to prevent data loss") from exc

    # Find migration files
    if not os.path.exists(MIGRATIONS_DIR):
        logger.warning("Migrations directory not found", path=MIGRATIONS_DIR)
        return

    migration_files = sorted([f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql")])
    if not migration_files:
        logger.warning("No migration files found", path=MIGRATIONS_DIR)
        return

    for filename in migration_files:
        # Expected format: YYYYMMDD_NNN_migration_name.sql
        parts = filename.split("_", 2)
        if len(parts) >= 2:
            version = f"{parts[0]}_{parts[1]}"
            name = parts[2].replace(".sql", "") if len(parts) > 2 else ""
        else:
            version = filename.replace(".sql", "")
            name = ""

        if version in applied_versions:
            continue

        filepath = os.path.join(MIGRATIONS_DIR, filename)
        logger.info("Applying migration", version=version, name=name)

        with open(filepath, "r") as f:
            content = f.read()

        statements = _extract_up_statements(content)
        total = len(statements)
        for idx, stmt in enumerate(statements):
            try:
                client.command(stmt)
            except Exception as exc:
                logger.error(
                    "Migration statement failed — schema may be partially applied",
                    version=version,
                    statement_index=f"{idx + 1}/{total}",
                    statement=stmt[:160],
                    error=str(exc),
                )
                raise

        # Record successful migration
        client.insert("hft.schema_migrations", [[version, name]], column_names=["version", "name"])
        logger.info("Migration applied successfully", version=version, name=name)

    logger.info("Schema migrations up to date")


# =============================================================================
# Legacy compatibility stubs for older components
# =============================================================================


def _view_uses_legacy_price(client, name: str) -> bool:
    return False


def _execute_all(client, statements: Iterable[str]) -> None:
    for stmt in statements:
        client.command(stmt)


def ensure_price_scaled_views(client) -> bool:
    """
    Legacy view repair function. Now handled entirely by migrations.
    Returns False as a no-op to indicate no repair was needed here.
    """
    return False
