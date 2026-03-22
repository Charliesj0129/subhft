from __future__ import annotations

from scripts.check_test_quality_patterns import main


def _write_test_file(tmp_path, contents: str) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_quality_behavior.py").write_text(contents, encoding="utf-8")


def test_checker_allows_concrete_postcondition(tmp_path, monkeypatch):
    _write_test_file(
        tmp_path,
        "def test_updates_state():\n    value = 2 + 2\n    assert value == 4\n",
    )
    monkeypatch.chdir(tmp_path)

    assert main() == 0


def test_checker_rejects_tautological_none_or_not_none(tmp_path, monkeypatch):
    _write_test_file(
        tmp_path,
        "def test_bad():\n    value = object()\n    assert value is None or value is not None\n",
    )
    monkeypatch.chdir(tmp_path)

    assert main() == 1


def test_checker_rejects_tautological_eq_or_neq(tmp_path, monkeypatch):
    _write_test_file(
        tmp_path,
        "def test_bad():\n    left = 1\n    right = 2\n    assert left == right or left != right\n",
    )
    monkeypatch.chdir(tmp_path)

    assert main() == 1


def test_checker_rejects_blanket_except_exception_pass(tmp_path, monkeypatch):
    _write_test_file(
        tmp_path,
        "def test_bad():\n"
        "    try:\n"
        "        value = 1 / 1\n"
        "    except Exception:\n"
        "        pass\n"
        "    assert value == 1\n",
    )
    monkeypatch.chdir(tmp_path)

    assert main() == 1


def test_checker_ignores_except_exception_with_real_assertion(tmp_path, monkeypatch):
    _write_test_file(
        tmp_path,
        "def test_handles_fallback():\n"
        "    try:\n"
        "        raise RuntimeError('boom')\n"
        "    except Exception as exc:\n"
        "        assert str(exc) == 'boom'\n",
    )
    monkeypatch.chdir(tmp_path)

    assert main() == 0


def test_checker_inspects_collectable_test_methods(tmp_path, monkeypatch):
    _write_test_file(
        tmp_path,
        "class TestQualityPatterns:\n"
        "    def test_bad_method(self):\n"
        "        value = None\n"
        "        assert value is None or value is not None\n",
    )
    monkeypatch.chdir(tmp_path)

    assert main() == 1
