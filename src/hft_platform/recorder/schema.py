from __future__ import annotations

import os
from typing import Iterable

from structlog import get_logger

logger = get_logger("recorder.schema")

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "../migrations/clickhouse")


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
        logger.warning("Failed to query schema_migrations", error=str(exc))
        applied_versions = set()

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

        # Parse Up section
        up_content = content
        if "-- Down" in content:
            up_content = content.split("-- Down")[0]
        if "-- Up" in up_content:
            up_content = up_content.split("-- Up")[-1]

        # Extract individual statements
        statements = [stmt.strip() for stmt in up_content.split(";") if stmt.strip()]
        for stmt in statements:
            try:
                client.command(stmt)
            except Exception as exc:
                logger.error("Migration statement failed", version=version, statement=stmt[:160], error=str(exc))
                raise

        # Record successful migration
        client.command("INSERT INTO hft.schema_migrations (version, name) VALUES", [[version, name]])
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
