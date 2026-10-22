"""A unit test must not inherit the operator's environment.

``make`` exports everything in ``.env`` to the test process, so a variable that is set there reaches pytest under
``make test-unit-ci`` but not under a bare ``pytest``. ``OrderAdapter`` builds a ``ShadowOrderSink`` that reads
``HFT_ORDER_SHADOW_MODE`` at construction; inherited as ``1`` it turned every pre-dispatch rejection into
``shadow_intercepted`` and failed ``test_predispatch_rejection_releases_pending_slot.py`` (a safety property: a
CANCEL has to reach dispatch during a HALT) in the full run only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from hft_platform.order.shadow import ShadowOrderSink

UNIT_CONFTEST = str(Path(__file__).resolve().with_name("conftest.py"))


def _unit_conftest() -> ModuleType:
    for module in list(sys.modules.values()):
        if getattr(module, "__file__", None) == UNIT_CONFTEST:
            return module
    pytest.fail(f"{UNIT_CONFTEST} is not loaded as a conftest")


def test_the_scrub_covers_the_shadow_mode_switch() -> None:
    assert "HFT_ORDER_SHADOW_MODE" in _unit_conftest().OPERATOR_ENV_VARS


def test_the_scrub_still_covers_the_variables_it_scrubbed_before_the_shadow_switch_was_added() -> None:
    assert {
        "HFT_MODE",
        "HFT_ENV",
        "HFT_QUOTE_CONNECTIONS",
        "HFT_LOOP_ID",
        "HFT_LOOP",
        "HFT_ORDER_MODE",
        "HFT_LIVE_CONFIRM",
    } <= set(_unit_conftest().OPERATOR_ENV_VARS)


def test_the_scrub_removes_every_operator_variable_it_lists() -> None:
    conftest = _unit_conftest()

    with pytest.MonkeyPatch.context() as mp:
        for var in conftest.OPERATOR_ENV_VARS:
            mp.setenv(var, "1")

        conftest.scrub_operator_env(mp)

        assert [var for var in conftest.OPERATOR_ENV_VARS if var in os.environ] == []


def test_a_shadow_sink_built_in_a_unit_test_is_not_in_shadow_mode() -> None:
    """The reachable symptom: with ``HFT_ORDER_SHADOW_MODE=1`` exported, a default-built adapter intercepts orders."""
    assert ShadowOrderSink().enabled is False


def test_a_test_that_sets_the_shadow_switch_itself_still_gets_shadow_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_ORDER_SHADOW_MODE", "1")

    assert ShadowOrderSink().enabled is True


def test_a_unit_test_run_with_the_shadow_switch_exported_still_builds_a_sink_that_is_not_in_shadow_mode() -> None:
    """The autouse fixture, end to end: the variable is exported to a fresh pytest, as ``make`` exports ``.env``."""
    node = f"{Path(__file__)}::test_a_shadow_sink_built_in_a_unit_test_is_not_in_shadow_mode"
    env = {**os.environ, "HFT_ORDER_SHADOW_MODE": "1"}

    result = subprocess.run(
        [sys.executable, "-m", "pytest", node, "-q", "--no-cov", "-p", "no:cacheprovider"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
