"""``structlog.testing.capture_logs`` must see a module's logger whatever ran before the test.

``configure_logging`` turns on ``cache_logger_on_first_use`` and installs a *new* processor list on every call. The first
call through a module-level logger freezes the list that was current then. ``capture_logs`` keeps the *current* list
instance and edits it in place, so once any earlier test (``tests/integration``, or a unit test that builds a real
``HFTSystem``) has configured logging a second time, the cached logger writes to a list nobody is editing and the
capture records nothing for the rest of the session. The event still reaches stdout, so such a test reads as "the
code stopped logging". These tests pin the fixture that undoes that, and a guard keeps the next ``capture_logs``
user from forgetting it.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from hft_platform.feed_adapter.shioaji import family_populator

UNIT_DIR = Path(__file__).resolve().parent
OPT_IN = frozenset({"uncached_structlog_loggers", "module_log_sink"})


def _forget_the_cached_logger() -> None:
    vars(family_populator.logger).pop("bind", None)


@contextmanager
def _logger_cached_under_a_superseded_configuration() -> Iterator[None]:
    """What two ``configure_logging()`` calls with a real ``HFTSystem`` in between do to a module-level logger.

    The first call through the logger freezes the processor list that is current; a second configuration then
    installs a different list instance, which is the one ``capture_logs`` edits.
    """
    config = structlog.get_config()
    original_processors, original_flag = config["processors"], config["cache_logger_on_first_use"]
    structlog.configure(cache_logger_on_first_use=True)
    try:
        family_populator.logger.info("freeze the processors")
        structlog.configure(processors=[*original_processors])
        yield
    finally:
        structlog.configure(processors=original_processors, cache_logger_on_first_use=original_flag)
        _forget_the_cached_logger()


def test_capture_logs_misses_a_logger_that_cached_under_an_earlier_configuration() -> None:
    """The premise. If a structlog upgrade ever fixes this, the fixture below is obsolete and this test says so."""
    with _logger_cached_under_a_superseded_configuration():
        with capture_logs() as logs:
            family_populator.logger.warning("after the cache")

    assert logs == []


def test_the_fixture_makes_a_logger_cached_earlier_visible_to_capture_logs(uncached_structlog_loggers) -> None:
    with _logger_cached_under_a_superseded_configuration():  # an earlier test's work, after the fixture's own sweep
        uncached_structlog_loggers()

        with capture_logs() as logs:
            family_populator.logger.warning("after the cache")

    assert [e["event"] for e in logs] == ["after the cache"]


def test_the_fixture_sweeps_the_loggers_cached_before_the_test_started(request: pytest.FixtureRequest) -> None:
    with _logger_cached_under_a_superseded_configuration():
        assert "bind" in vars(family_populator.logger)

        request.getfixturevalue("uncached_structlog_loggers")

        assert "bind" not in vars(family_populator.logger)


def test_the_fixture_reports_how_many_cached_loggers_it_cleared(uncached_structlog_loggers) -> None:
    assert uncached_structlog_loggers() == 0  # the fixture's own sweep already ran, and nothing cached since

    with _logger_cached_under_a_superseded_configuration():
        assert uncached_structlog_loggers() >= 1


# --- the guard: every direct capture_logs user opts in -----------------------------------------------------------


def _capture_logs_users_without_opt_in(root: Path, *, skip: set[str]) -> list[str]:
    missing: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name in skip:
            continue
        source = path.read_text(encoding="utf-8")
        if "capture_logs" not in source:
            continue
        tree = ast.parse(source)
        uses = any(
            (isinstance(n, ast.Attribute) and n.attr == "capture_logs")
            or (isinstance(n, ast.Name) and n.id == "capture_logs")
            or (isinstance(n, ast.alias) and n.name == "capture_logs")
            for n in ast.walk(tree)
        )
        opts_in = any(
            (isinstance(n, ast.Constant) and n.value in OPT_IN) or (isinstance(n, ast.arg) and n.arg in OPT_IN)
            for n in ast.walk(tree)
        )
        if uses and not opts_in:
            missing.append(str(path.relative_to(root)))
    return missing


def test_every_unit_test_file_that_calls_capture_logs_opts_in_to_the_isolation() -> None:
    """Use ``pytestmark = pytest.mark.usefixtures("uncached_structlog_loggers")``, or ``module_log_sink``."""
    missing = _capture_logs_users_without_opt_in(UNIT_DIR, skip={"conftest.py", Path(__file__).name})

    assert missing == [], f"capture_logs without isolation (flaky after the integration suite): {missing}"


def test_the_guard_flags_a_file_that_forgets_and_accepts_one_that_opts_in(tmp_path: Path) -> None:
    (tmp_path / "forgets.py").write_text(
        "from structlog.testing import capture_logs\n\n\ndef test_x():\n    with capture_logs():\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "marked.py").write_text(
        "import pytest\nimport structlog\n\n"
        'pytestmark = pytest.mark.usefixtures("uncached_structlog_loggers")\n\n\n'
        "def test_x():\n    with structlog.testing.capture_logs():\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "sink.py").write_text(
        "from structlog.testing import capture_logs\n\n\ndef test_x(module_log_sink):\n    capture_logs\n",
        encoding="utf-8",
    )
    # Each way a file can name ``capture_logs`` is its own detection path.
    (tmp_path / "attribute_only.py").write_text(
        "import structlog\n\n\ndef test_x():\n    with structlog.testing.capture_logs():\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "alias_only.py").write_text(
        "from structlog.testing import capture_logs as capture\n\n\ndef test_x():\n    with capture():\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "star_import.py").write_text(
        "from structlog.testing import *\n\n\ndef test_x():\n    with capture_logs():\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")

    assert _capture_logs_users_without_opt_in(tmp_path, skip=set()) == [
        "alias_only.py",
        "attribute_only.py",
        "forgets.py",
        "star_import.py",
    ]
