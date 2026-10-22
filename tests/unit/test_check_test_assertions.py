"""``scripts/check_test_assertions.py``: the advisory threshold of ``make test-assertion-check``.

The Makefile target passes ``--max-advisory 0`` to say "no test may lack an assertion". The script once read ``0`` as
"no limit", so the target accepted any number of them while looking stricter than the ``10`` it replaced.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_test_assertions.py"

WITH_ASSERT = "def test_adds_two_numbers():\n    assert 1 + 1 == 2\n"
WITHOUT_ASSERT = "def test_adds_two_numbers_and_forgets_to_check():\n    total = 1 + 1\n    print(total)\n"


def _run(tmp_path: Path, files: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """Run the script in a scratch tree whose ``tests/`` holds ``files`` (the script scans ``./tests``)."""
    for name, source in files.items():
        target = tmp_path / "tests" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    return subprocess.run(  # noqa: S603 -- fixed interpreter and script path
        [sys.executable, str(SCRIPT), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_a_threshold_of_zero_accepts_a_tree_where_every_test_asserts(tmp_path: Path) -> None:
    result = _run(tmp_path, {"unit/test_a.py": WITH_ASSERT}, "--max-advisory", "0")

    assert result.returncode == 0, result.stdout
    assert "0 without assertions" in result.stdout


def test_a_threshold_of_zero_rejects_one_advisory_test_without_an_assertion(tmp_path: Path) -> None:
    result = _run(tmp_path, {"unit/test_a.py": WITH_ASSERT, "unit/test_b.py": WITHOUT_ASSERT}, "--max-advisory", "0")

    assert result.returncode == 1, result.stdout
    assert "exceeds threshold (0)" in result.stdout
    assert "test_b.py:1" in result.stdout


def test_a_threshold_of_one_accepts_one_and_rejects_two(tmp_path: Path) -> None:
    one = _run(tmp_path, {"unit/test_b.py": WITHOUT_ASSERT}, "--max-advisory", "1")
    two = _run(
        tmp_path, {"unit/test_c.py": WITHOUT_ASSERT.replace("forgets_to_check", "forgets_again")}, "--max-advisory", "1"
    )

    assert one.returncode == 0, one.stdout
    assert two.returncode == 1, two.stdout
    assert "exceeds threshold (1)" in two.stdout


def test_no_threshold_means_no_limit_on_advisory_tests(tmp_path: Path) -> None:
    result = _run(tmp_path, {"unit/test_b.py": WITHOUT_ASSERT})

    assert result.returncode == 0, result.stdout
    assert "1 without assertions" in result.stdout


def test_a_critical_path_test_without_an_assertion_fails_whatever_the_threshold(tmp_path: Path) -> None:
    result = _run(tmp_path, {"unit/test_risk_engine.py": WITHOUT_ASSERT})

    assert result.returncode == 1, result.stdout
    assert "CRITICAL" in result.stdout


def test_an_allowlisted_test_is_not_counted(tmp_path: Path) -> None:
    allowlisted = WITHOUT_ASSERT.replace("():", "():  # noqa: no-assert", 1)

    result = _run(tmp_path, {"unit/test_b.py": allowlisted}, "--max-advisory", "0")

    assert result.returncode == 0, result.stdout
    assert "0 without assertions" in result.stdout


def test_the_make_target_passes_a_threshold_of_zero() -> None:
    """The ratchet is the Makefile argument; if it grows again, the allowance is back."""
    recipe = re.search(r"^test-assertion-check:.*\n\t(.+)$", (REPO / "Makefile").read_text(encoding="utf-8"), re.M)

    assert recipe is not None, "no test-assertion-check recipe in the Makefile"
    assert re.search(r"check_test_assertions\.py\s+--max-advisory\s+0\s*$", recipe.group(1)), recipe.group(1)
