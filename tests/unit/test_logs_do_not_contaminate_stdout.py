"""stdout is for machine-readable output; logs belong on stderr.

``configure_logging`` used structlog's ``PrintLoggerFactory()`` with no file
argument, which defaults to stdout, and pointed stdlib logging at stdout too.
Any command that printed a JSON payload therefore shared a stream with the log
lines emitted while producing it. ``hft alpha cheap-screen`` wrote a
``cheap_screen_start`` debug event immediately before its verdict, so::

    {"alpha_id": "...", "event": "cheap_screen_start", "level": "debug", ...}
    {"verdict": "kill", ...}

piping that to ``jq`` -- or ``json.loads`` -- failed with "Extra data" as soon
as DEBUG was enabled. It stayed hidden because the default level is INFO, and
surfaced only when another test in the same process turned DEBUG on.
"""

from __future__ import annotations

import json
import subprocess
import sys

CHILD = """
import json, logging, sys
from hft_platform.utils.logging import configure_logging, get_logger

configure_logging(level=logging.DEBUG)
log = get_logger("stream_contract_probe")
log.debug("noisy_debug_event", detail="must not reach stdout")
log.info("noisy_info_event")
log.warning("noisy_warning_event")
log.error("noisy_error_event")
print(json.dumps({"payload": "the only thing on stdout"}))
"""


def _run_probe() -> subprocess.CompletedProcess[str]:
    # A subprocess is the honest test: capsys and caplog both intercept the
    # streams this contract is about, so an in-process check could pass while
    # the real command still interleaved.
    return subprocess.run(
        [sys.executable, "-c", CHILD],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


def test_stdout_stays_parseable_json_while_logging_at_debug() -> None:
    result = _run_probe()

    # The whole stream, not "from the first brace" -- that accommodation is
    # what let the bug survive.
    assert json.loads(result.stdout) == {"payload": "the only thing on stdout"}


def test_log_records_are_written_to_stderr() -> None:
    result = _run_probe()

    events = []
    for line in result.stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            events.append(json.loads(line).get("event"))

    # Losing the logs entirely would also make stdout parse, so assert they
    # arrived on the other stream rather than merely that stdout is clean.
    for expected in ("noisy_debug_event", "noisy_info_event", "noisy_warning_event", "noisy_error_event"):
        assert expected in events, f"{expected} missing from stderr: {result.stderr!r}"


def test_no_log_event_leaks_onto_stdout() -> None:
    result = _run_probe()

    assert "noisy_" not in result.stdout
    assert "level" not in result.stdout


def test_configure_logging_binds_both_sinks_to_stderr() -> None:
    """stdlib logging shares the stream, so both sinks have to move together."""
    import inspect

    from hft_platform.utils import logging as hft_logging

    src = inspect.getsource(hft_logging.configure_logging)
    assert "logger_factory=_stderr_logger_factory" in src
    assert "stream=sys.stderr" in src
    assert "stream=sys.stdout" not in src

    # The sink resolves sys.stderr per write; binding it once would pin a
    # test's redirect buffer for the rest of the process.
    factory_src = inspect.getsource(hft_logging._CurrentStderr)
    assert "sys.stderr.write" in factory_src


def test_logging_level_still_applies() -> None:
    """Moving the stream must not disturb level filtering."""
    child = CHILD.replace("level=logging.DEBUG", "level=logging.INFO")
    result = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(result.stdout) == {"payload": "the only thing on stdout"}
    assert "noisy_info_event" in result.stderr


UNCONFIGURED_CHILD = """
import json, sys
from hft_platform.utils.logging import get_logger

# Deliberately no configure_logging() call.
get_logger("unconfigured_probe").warning("unconfigured_event")
print(json.dumps({"payload": "clean"}))
"""


def test_stdout_stays_clean_before_configure_logging_runs() -> None:
    """structlog's own defaults print to stdout; importing us must override that.

    This is the half that actually broke the CLI tests: they call the command
    function directly, so ``configure_logging`` never ran and the sink fell
    back to structlog's stdout default.
    """
    result = subprocess.run(
        [sys.executable, "-c", UNCONFIGURED_CHILD], capture_output=True, text=True, timeout=60, check=True
    )

    assert json.loads(result.stdout) == {"payload": "clean"}
    assert "unconfigured_event" not in result.stdout
    assert "unconfigured_event" in result.stderr
