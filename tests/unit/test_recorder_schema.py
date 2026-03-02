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
