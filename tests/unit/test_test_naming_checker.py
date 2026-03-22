from __future__ import annotations

from scripts.check_test_naming import main


def _write_test_file(tmp_path, contents: str) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_example_behavior.py").write_text(contents, encoding="utf-8")


def test_check_test_naming_allows_behavior_filename(tmp_path, monkeypatch):
    _write_test_file(tmp_path, "def test_smoke():\n    assert True\n")
    monkeypatch.chdir(tmp_path)

    assert main() == 0


def test_check_test_naming_rejects_cov_filename(tmp_path, monkeypatch):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    bad_file = tests_dir / "test_example_cov.py"
    bad_file.write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main() == 1


def test_check_test_naming_rejects_forbidden_test_function_name(tmp_path, monkeypatch):
    _write_test_file(
        tmp_path,
        "def test_covers_bootstrap_path():\n"
        "    assert True\n",
    )
    monkeypatch.chdir(tmp_path)

    assert main() == 1


def test_check_test_naming_rejects_forbidden_async_test_function_name(tmp_path, monkeypatch):
    _write_test_file(
        tmp_path,
        "async def test_cov_async_bootstrap_path():\n"
        "    assert True\n",
    )
    monkeypatch.chdir(tmp_path)

    assert main() == 1


def test_check_test_naming_ignores_nested_helper_names(tmp_path, monkeypatch):
    _write_test_file(
        tmp_path,
        "def test_runs_behavior_check():\n"
        "    def test_covers_nested_helper():\n"
        "        return None\n"
        "    async def test_cov_nested_async_helper():\n"
        "        return None\n"
        "    assert test_covers_nested_helper() is None\n",
    )
    monkeypatch.chdir(tmp_path)

    assert main() == 0


def test_check_test_naming_ignores_non_collectable_class_methods(tmp_path, monkeypatch):
    _write_test_file(
        tmp_path,
        "class HelperCases:\n"
        "    def test_line_helper_method(self):\n"
        "        assert True\n",
    )
    monkeypatch.chdir(tmp_path)

    assert main() == 0
