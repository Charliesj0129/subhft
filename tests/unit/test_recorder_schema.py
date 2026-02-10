from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from hft_platform.recorder import schema


def test_load_statements_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sql"
    assert schema._load_statements(str(missing)) == []


def test_apply_schema_runs_commands(tmp_path: Path) -> None:
    sql_path = tmp_path / "schema.sql"
    sql_path.write_text("CREATE TABLE foo();\n;CREATE TABLE bar();")
    client = MagicMock()
    schema.apply_schema(client, schema_path=str(sql_path))
    assert client.command.call_count == 2


def test_apply_schema_handles_missing_file(tmp_path: Path) -> None:
    client = MagicMock()
    schema.apply_schema(client, schema_path=str(tmp_path / "none.sql"))
    client.command.assert_not_called()


def test_ensure_price_scaled_views_no_legacy() -> None:
    client = MagicMock()
    client.query.return_value = SimpleNamespace(result_rows=[["SELECT price_scaled FROM t"]])
    assert schema.ensure_price_scaled_views(client) is False
    client.command.assert_not_called()


def test_ensure_price_scaled_views_repairs() -> None:
    client = MagicMock()
    client.query.return_value = SimpleNamespace(result_rows=[["SELECT \\bprice\\b FROM t"]])
    assert schema.ensure_price_scaled_views(client) is True
    assert client.command.call_count > 0
