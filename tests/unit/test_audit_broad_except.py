"""``scripts/audit_broad_except.py``: the read-only inventory of broad ``except`` handlers.

The script backs ``docs/architecture/hot-path-broad-except-inventory.md``. These tests pin the definitions that the
document's numbers depend on (what is "broad", what is "silent") on small synthetic trees, so a change to the script
cannot quietly move the counts.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# scripts/ is not a Python package; load the module by file path.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_broad_except.py"
_spec = importlib.util.spec_from_file_location("audit_broad_except", _SCRIPT)
assert _spec is not None and _spec.loader is not None
audit = importlib.util.module_from_spec(_spec)
sys.modules["audit_broad_except"] = audit
_spec.loader.exec_module(audit)


def _scan_source(tmp_path: Path, source: str, rel: str = "order/mod.py") -> list:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return audit.scan_file(path, tmp_path)


def _only(handlers: list):
    assert len(handlers) == 1, handlers
    return handlers[0]


# ---- what counts as silent -------------------------------------------------


@pytest.mark.parametrize("body", ["pass", "...", "continue", "break", "return", "return None"])
def test_a_single_silent_statement_in_a_broad_handler_is_silent(tmp_path, body):
    src = (
        f"def f(xs):\n    for x in xs:\n        try:\n            g(x)\n        except Exception:\n            {body}\n"
    )
    handler = _only(_scan_source(tmp_path, src))
    assert handler.shape == "silent"
    assert handler.body == body


def test_a_return_of_a_constant_is_a_fallback_not_silent(tmp_path):
    src = "def f():\n    try:\n        return g()\n    except Exception:\n        return False\n"
    handler = _only(_scan_source(tmp_path, src))
    assert handler.shape == "fallback"


def test_a_computed_return_is_neither_silent_nor_fallback(tmp_path):
    src = "def f():\n    try:\n        return g()\n    except Exception:\n        return default()\n"
    assert _only(_scan_source(tmp_path, src)).shape == "other"


def test_several_silent_statements_still_count_as_silent_with_a_multi_body(tmp_path):
    src = "def f():\n    try:\n        g()\n    except Exception:\n        pass\n        return None\n"
    handler = _only(_scan_source(tmp_path, src))
    assert (handler.shape, handler.body) == ("silent", "multi")


# ---- what counts as broad --------------------------------------------------


def test_a_specific_exception_type_is_not_reported(tmp_path):
    src = "def f():\n    try:\n        g()\n    except KeyError:\n        pass\n    except (ValueError, OSError):\n        pass\n"
    assert _scan_source(tmp_path, src) == []


@pytest.mark.parametrize(
    ("clause", "caught"),
    [
        ("except:", "bare"),
        ("except Exception:", "Exception"),
        ("except BaseException:", "BaseException"),
        ("except Exception as exc:", "Exception"),
        ("except (KeyError, Exception):", "tuple"),
    ],
)
def test_every_broad_spelling_is_reported(tmp_path, clause, caught):
    src = f"def f():\n    try:\n        g()\n    {clause}\n        pass\n"
    assert _only(_scan_source(tmp_path, src)).caught == caught


def test_nested_try_blocks_are_each_reported_once(tmp_path):
    src = (
        "def f():\n"
        "    try:\n"
        "        try:\n"
        "            g()\n"
        "        except Exception:\n"
        "            pass\n"
        "    except Exception:\n"
        "        pass\n"
    )
    assert sorted(h.line for h in _scan_source(tmp_path, src)) == [5, 7]


# ---- what the handler does instead of staying silent ------------------------


@pytest.mark.parametrize(
    ("handler_body", "shape"),
    [
        ("logger.warning('x')", "logs"),
        ("logger.error('x', exc_info=True)", "logs"),
        ("logger.debug('x')", "debug-only"),
        ("logger.debug('x')\n        logger.debug('y')", "debug-only"),
        ("logger.debug('x')\n        logger.warning('y')", "logs"),
        ("self.metrics.failed_total.inc()", "counts"),
        ("self.metrics.failed_total.inc()\n        logger.warning('x')", "logs"),
        ("raise", "reraises"),
        ("raise RuntimeError('x') from None", "reraises"),
        ("cleanup()", "other"),
        # every level the document counts as "a log line", and a re-raise that logs first is still a re-raise
        ("logger.info('x')", "logs"),
        ("logger.warn('x')", "logs"),
        ("logger.critical('x')", "logs"),
        ("logger.exception('x')", "logs"),
        ("logger.warning('x')\n        raise", "reraises"),
        # the receiver decides: a mixin logger is ``self``, anything else with a log-sounding method is not a log
        ("self.warning('x')", "logs"),
        ("result.error('x')", "other"),
    ],
)
def test_a_handler_that_does_something_is_classified_by_what_it_does(tmp_path, handler_body, shape):
    src = f"def f():\n    try:\n        g()\n    except Exception:\n        {handler_body}\n"
    assert _only(_scan_source(tmp_path, src)).shape == shape


# ---- what the try body touches ---------------------------------------------


@pytest.mark.parametrize(
    ("try_body", "kind"),
    [
        ("self.metrics.orders_total.labels(side='buy').inc()", "metrics-guard"),
        ("gauge.set(1)", "metrics-guard"),
        ("self._metrics.get('x').inc()", "metrics-guard"),
        ("conn.close()", "cleanup"),
        ("task.cancel()", "cleanup"),
        ("return int(value)", "parse"),
        ("return orjson.loads(raw)", "parse"),
        ("do_something_else()", "other"),
    ],
)
def test_the_try_body_decides_the_kind(tmp_path, try_body, kind):
    src = f"def f():\n    try:\n        {try_body}\n    except Exception:\n        pass\n"
    assert _only(_scan_source(tmp_path, src)).try_kind == kind


# ---- comments --------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "commented"),
    [
        ("def f():\n    try:\n        g()\n    except Exception:  # explained\n        pass\n", True),
        ("def f():\n    try:\n        g()\n    except Exception:\n        pass  # explained\n", True),
        ("def f():\n    try:\n        g()\n    # explained\n    except Exception:\n        pass\n", True),
        ("def f():\n    try:\n        g()\n    except Exception:\n        pass\n", False),
        ("def f():\n    try:\n        g()  # not about the handler\n    except Exception:\n        pass\n", False),
    ],
)
def test_a_comment_on_or_above_the_handler_counts_as_an_explanation(tmp_path, src, commented):
    assert _only(_scan_source(tmp_path, src)).commented is commented


# ---- groups ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "group"),
    [
        ("order/adapter.py", "money"),
        ("risk/engine.py", "money"),
        ("gateway/service.py", "money"),
        ("execution/router.py", "money"),
        ("feed_adapter/shioaji/client.py", "market-data"),
        ("strategy/runner.py", "market-data"),
        ("feature/engine.py", "market-data"),
        ("recorder/writer.py", "recorder"),
        ("services/bootstrap.py", "services"),
        ("ops/evidence.py", "other"),
        ("events.py", "root"),
    ],
)
def test_a_file_is_grouped_by_its_top_level_package(rel, group):
    assert audit.group_of(Path(rel)) == group


# ---- scanning a tree -------------------------------------------------------


def test_test_files_pycache_and_unparseable_files_are_skipped_without_stopping_the_scan(tmp_path):
    broad = "def f():\n    try:\n        g()\n    except Exception:\n        pass\n"
    (tmp_path / "order").mkdir()
    (tmp_path / "order" / "keep.py").write_text(broad)
    (tmp_path / "order" / "test_skip.py").write_text(broad)
    (tmp_path / "order" / "__pycache__").mkdir()
    (tmp_path / "order" / "__pycache__" / "junk.py").write_text(broad)
    (tmp_path / "order" / "broken.py").write_text("def f(:\n")

    found = audit.scan(tmp_path)

    assert [(h.file, h.line) for h in found] == [("order/keep.py", 4)]


def test_handlers_come_back_ordered_by_file_then_line(tmp_path):
    """A nested ``try`` is visited outer first, and ``a.py`` has its later line before ``b.py``'s earlier one."""
    nested = (
        "def f():\n"
        "    try:\n"
        "        try:\n"
        "            g()\n"
        "        except Exception:\n"
        "            pass\n"
        "    except Exception:\n"
        "        pass\n"
    )
    (tmp_path / "order").mkdir()
    (tmp_path / "order" / "b.py").write_text("def f():\n    try:\n        g()\n    except Exception:\n        pass\n")
    (tmp_path / "order" / "a.py").write_text(nested)

    found = audit.scan(tmp_path)

    assert [(h.file, h.line) for h in found] == [("order/a.py", 5), ("order/a.py", 7), ("order/b.py", 4)]


def test_scanning_never_modifies_the_tree(tmp_path):
    src = "def f():\n    try:\n        g()\n    except Exception:\n        pass\n"
    (tmp_path / "order").mkdir()
    target = tmp_path / "order" / "mod.py"
    target.write_text(src)
    before = target.read_bytes(), target.stat().st_mtime_ns

    audit.scan(tmp_path)
    assert audit.main(["--root", str(tmp_path)]) == 0

    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.rglob("*")) == ["mod.py", "order"]


def test_summary_counts_silent_handlers_and_those_without_a_comment(tmp_path):
    src = (
        "def f():\n"
        "    try:\n        g()\n    except Exception:\n        pass\n"
        "    try:\n        g()\n    except Exception:  # why\n        pass\n"
        "    try:\n        g()\n    except Exception:\n        logger.warning('x')\n"
    )
    handlers = _scan_source(tmp_path, src)

    row = audit.summarize(handlers)["money"]

    assert row["total"] == 3
    assert row["silent"] == 2
    assert row["logs"] == 1
    assert row["silent, no comment"] == 1


# ---- command line ----------------------------------------------------------


@pytest.fixture()
def tree(tmp_path):
    """order/a.py: silent ``pass`` (line 5). order/c.py: silent ``continue`` (line 6). services/b.py: logs."""
    sources = {
        "order/a.py": "def f():\n    try:\n        g()\n    except Exception:\n        pass\n",
        "order/c.py": "def f(y):\n    for x in y:\n        try:\n            g(x)\n        except Exception:\n"
        "            continue\n",
        "services/b.py": "def f():\n    try:\n        g()\n    except Exception:\n        logger.warning('x')\n",
    }
    for rel, source in sources.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


def test_a_missing_root_is_an_error_not_an_empty_report(tmp_path, capsys):
    assert audit.main(["--root", str(tmp_path / "nope")]) == 2
    assert "is not a directory" in capsys.readouterr().err


def test_the_markdown_report_has_a_row_per_group_and_a_total(tree, capsys):
    assert audit.main(["--root", str(tree)]) == 0

    out = capsys.readouterr().out
    assert "Broad handlers (bare / Exception / BaseException): 3" in out
    assert "| money |" in out
    assert "| services |" in out
    assert "| **all** |" in out


def _markdown_rows(out: str) -> dict[str, dict[str, str]]:
    """The report's table as {group: {column: cell}}; the separator row is dropped."""
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in out.splitlines() if line.startswith("|")]
    header, body = rows[0], rows[2:]
    return {row[0]: dict(zip(header[1:], row[1:], strict=True)) for row in body}


def test_the_markdown_numbers_per_group_and_in_total_are_the_ones_the_handlers_add_up_to(tree, capsys):
    assert audit.main(["--root", str(tree)]) == 0

    table = _markdown_rows(capsys.readouterr().out)

    assert table["money"]["total"] == "2"
    assert table["money"]["silent"] == "2"
    assert table["money"]["silent, no comment"] == "2"
    assert table["services"]["total"] == "1"
    assert table["services"]["logs"] == "1"
    assert table["services"]["silent, no comment"] == "0"
    assert table["**all**"]["total"] == "3"
    assert table["**all**"]["silent"] == "2"
    assert table["**all**"]["logs"] == "1"
    assert table["**all**"]["silent, no comment"] == "2"


@pytest.fixture()
def mixed_tree(tmp_path):
    """Money and services each hold a silent and a logging handler, so neither filter can stand in for the other."""
    sources = {
        "order/a.py": "def f():\n    try:\n        conn.close()\n    except Exception:  # why\n        pass\n",
        "order/d.py": "def f():\n    try:\n        g()\n    except Exception:\n        logger.warning('x')\n",
        "services/e.py": "def f():\n    try:\n        g()\n    except Exception:\n        pass\n",
        "services/h.py": "def f():\n    try:\n        g()\n    except Exception:\n        logger.warning('x')\n",
    }
    for rel, source in sources.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return tmp_path


def _listed(capsys) -> list[list[str]]:
    return [line.split() for line in capsys.readouterr().out.strip().splitlines()]


def test_list_by_group_alone_and_by_group_and_shape(mixed_tree, capsys):
    assert audit.main(["--root", str(mixed_tree), "--list", "money"]) == 0
    assert [fields[0] for fields in _listed(capsys)] == ["order/a.py:4", "order/d.py:4"]

    assert audit.main(["--root", str(mixed_tree), "--list", "money", "--shape", "silent"]) == 0
    assert [fields[0] for fields in _listed(capsys)] == ["order/a.py:4"]

    assert audit.main(["--root", str(mixed_tree), "--list", "services", "--shape", "silent"]) == 0
    assert [fields[0] for fields in _listed(capsys)] == ["services/e.py:4"]


def test_a_list_line_carries_caught_shape_comment_try_kind_and_body(mixed_tree, capsys):
    assert audit.main(["--root", str(mixed_tree), "--list", "all"]) == 0

    lines = {fields[0]: fields[1:] for fields in _listed(capsys)}

    assert lines["order/a.py:4"] == ["Exception", "silent", "commented", "cleanup", "pass"]
    assert lines["services/e.py:4"] == ["Exception", "silent", "NO-COMMENT", "other", "pass"]
    assert lines["order/d.py:4"][:3] == ["Exception", "logs", "NO-COMMENT"]


def test_the_json_report_is_parseable_and_carries_every_handler(tree, capsys):
    assert audit.main(["--root", str(tree), "--format", "json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert len(data["handlers"]) == 3
    assert data["summary"]["money"]["silent"] == 2


def test_list_filters_by_group_and_by_shape(tree, capsys):
    assert audit.main(["--root", str(tree), "--list", "money", "--shape", "silent"]) == 0

    lines = capsys.readouterr().out.strip().splitlines()
    assert [line.split()[0] for line in lines] == ["order/a.py:4", "order/c.py:5"]
    assert lines[0].endswith("pass")
    assert lines[1].endswith("continue")


def test_shape_without_list_is_refused_instead_of_ignored(tree, capsys):
    with pytest.raises(SystemExit) as exc:
        audit.main(["--root", str(tree), "--shape", "silent"])

    assert exc.value.code == 2
    assert "--shape needs --list" in capsys.readouterr().err


def test_list_all_includes_every_group(tree, capsys):
    assert audit.main(["--root", str(tree), "--list", "all"]) == 0

    assert len(capsys.readouterr().out.strip().splitlines()) == 3


def test_the_real_source_tree_scans_cleanly():
    """Smoke test on the repository itself: no crash, every field in range. It does not pin counts."""
    root = Path(__file__).resolve().parents[2] / "src" / "hft_platform"

    found = audit.scan(root)

    assert found, "the platform has broad handlers; an empty scan means the scanner broke"
    assert {h.shape for h in found} <= set(audit.SHAPES)
    assert all((root / h.file).is_file() for h in found)
