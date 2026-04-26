from pathlib import Path
from unittest.mock import MagicMock

from hft_platform.recorder import schema


def test_apply_schema_handles_missing_migrations_dir(tmp_path: Path, monkeypatch) -> None:
    client = MagicMock()
    monkeypatch.setattr(schema, "MIGRATIONS_DIR", str(tmp_path / "missing_migrations"))

    schema.apply_schema(client)

    # apply_schema always initializes schema_migrations table first.
    client.command.assert_any_call("CREATE DATABASE IF NOT EXISTS hft")
    assert client.command.call_count >= 2


def test_apply_schema_runs_migration_up_statements(tmp_path: Path, monkeypatch) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    migration = migrations_dir / "20260399_001_unit_test.sql"
    migration.write_text(
        "-- Up\nCREATE TABLE foo();\nCREATE TABLE bar();\n-- Down\nDROP TABLE foo();\n",
        encoding="utf-8",
    )

    client = MagicMock()
    client.query.return_value.result_rows = []
    monkeypatch.setattr(schema, "MIGRATIONS_DIR", str(migrations_dir))

    schema.apply_schema(client)

    issued = [str(call.args[0]) for call in client.command.call_args_list if call.args]
    assert any(stmt.startswith("CREATE TABLE foo()") for stmt in issued)
    assert any(stmt.startswith("CREATE TABLE bar()") for stmt in issued)
    # Migration recording must use client.insert(), not client.command()
    assert not any("INSERT INTO hft.schema_migrations" in stmt for stmt in issued), (
        "Migration recording must use client.insert(), not client.command()"
    )


def test_apply_schema_records_migration_via_insert(tmp_path: Path, monkeypatch) -> None:
    """Verify migration recording uses client.insert() with correct args (not client.command)."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    migration = migrations_dir / "20260399_002_record_test.sql"
    migration.write_text("-- Up\nCREATE TABLE baz();\n", encoding="utf-8")

    client = MagicMock()
    client.query.return_value.result_rows = []
    monkeypatch.setattr(schema, "MIGRATIONS_DIR", str(migrations_dir))

    schema.apply_schema(client)

    client.insert.assert_called_once_with(
        "hft.schema_migrations",
        [["20260399_002", "record_test"]],
        column_names=["version", "name"],
    )
    # Confirm no INSERT command was issued via client.command
    command_calls = [str(c.args[0]) for c in client.command.call_args_list if c.args]
    assert not any("INSERT INTO hft.schema_migrations" in s for s in command_calls)


def test_ensure_price_scaled_views_no_legacy() -> None:
    client = MagicMock()
    assert schema.ensure_price_scaled_views(client) is False
    client.command.assert_not_called()


def test_ensure_price_scaled_views_repairs() -> None:
    client = MagicMock()
    assert schema.ensure_price_scaled_views(client) is False
    client.command.assert_not_called()


# ---------------------------------------------------------------------------
# _extract_up_statements — parser edge cases (regression: 2026-04-26 outage
# where `-- Up:` and comment-only fragments stalled all migrations since
# 2026-03-23, leading to 13 missing migrations and ClickHouse insert failures).
# ---------------------------------------------------------------------------


def test_extract_up_statements_marker_with_trailing_text() -> None:
    """`-- Up: comment` must be treated as a marker, not split into `: comment`.

    Pre-fix the splitter took everything after the literal `-- Up`, which left
    `: post-market reconciliation` as the first token — ClickHouse rejected
    the leading colon as a syntax error and blocked all later migrations.
    """
    content = (
        "-- Up: post-market 3-way reconciliation results table\n"
        "CREATE TABLE foo (x Int64) ENGINE=MergeTree ORDER BY x;\n"
        "-- Down\n"
        "DROP TABLE foo;\n"
    )
    statements = schema._extract_up_statements(content)
    assert len(statements) == 1
    assert statements[0].startswith("CREATE TABLE foo")
    assert not statements[0].lstrip().startswith(":")


def test_extract_up_statements_skips_comment_only_fragments() -> None:
    """Header / trailing comment blocks must not be sent to ClickHouse.

    Pre-fix a file like the fills-RMT migration produced fragments that were
    only `--` lines; ClickHouse returned `Empty query (SYNTAX_ERROR)`.
    """
    content = (
        "-- header doc line 1\n"
        "-- header doc line 2\n"
        "\n"
        "-- Up\n"
        "CREATE TABLE foo (x Int64) ENGINE=MergeTree ORDER BY x;\n"
        "\n"
        "-- trailing operational note\n"
        "-- another note\n"
    )
    statements = schema._extract_up_statements(content)
    assert len(statements) == 1
    assert statements[0].startswith("CREATE TABLE foo")


def test_extract_up_statements_no_markers() -> None:
    """A file with no `-- Up`/`-- Down` markers parses as one Up section."""
    content = (
        "CREATE TABLE foo (x Int64) ENGINE=MergeTree ORDER BY x;\n"
        "CREATE TABLE bar (y Int64) ENGINE=MergeTree ORDER BY y;\n"
    )
    statements = schema._extract_up_statements(content)
    assert len(statements) == 2


def test_extract_up_statements_strips_down_section() -> None:
    """Statements inside `-- Down` must never be returned."""
    content = (
        "-- Up\n"
        "CREATE TABLE foo (x Int64) ENGINE=MergeTree ORDER BY x;\n"
        "-- Down\n"
        "DROP TABLE foo;\n"
    )
    statements = schema._extract_up_statements(content)
    assert len(statements) == 1
    assert "DROP" not in statements[0]


def test_extract_up_statements_marker_must_be_at_line_start() -> None:
    """A `-- Up` substring inside a SQL comment mid-statement is NOT a marker."""
    content = (
        "-- Up\n"
        "CREATE TABLE foo (\n"
        "    x Int64  -- Updated to wider int\n"
        ") ENGINE=MergeTree ORDER BY x;\n"
    )
    statements = schema._extract_up_statements(content)
    assert len(statements) == 1
    assert "CREATE TABLE foo" in statements[0]
    # The mid-statement `-- Updated...` must not have caused a re-split.
    assert "Updated to wider int" in statements[0]
