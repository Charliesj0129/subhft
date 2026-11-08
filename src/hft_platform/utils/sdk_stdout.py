"""Keep broker-SDK prints out of the engine's container logs.

The Shioaji native core prints session events straight to file descriptor 1
(``Response Code: 200 | Event Code: 18 | Info: #P2P/.../PYAPI/<person id>/...``).
The person ID in that line is the login identity. Those prints bypass
structlog and its scrubbing, so on THESHOW they landed in ``docker logs
hft-engine`` (1024 lines in the 2026-10-23 container).

The engine's own logs go to stderr (``utils/logging.py``), so fd 1 carries
nothing the engine needs. ``isolate_sdk_stdout`` points fd 1 at ``/dev/null``
before any SDK object exists. It does not use a pipe and a reader thread: if
that thread died, the pipe would fill and the SDK thread writing to it would
block.
"""

from __future__ import annotations

import os
import sys

from structlog import get_logger

# Imported for its side effect: it points structlog's default logger at stderr.
# Without it, the warnings below would be written to the fd this module closes.
import hft_platform.utils.logging  # noqa: F401

logger = get_logger("sdk_stdout")

_ENV = "HFT_SDK_STDOUT"
_DEVNULL = "devnull"
_INHERIT = "inherit"


def isolate_sdk_stdout() -> str:
    """Redirect fd 1 to ``/dev/null`` unless ``HFT_SDK_STDOUT=inherit``.

    Returns the mode applied. An unknown value is treated as ``devnull``:
    the leak is the failure to close, so a typo must not reopen it.
    """
    raw = os.getenv(_ENV, _DEVNULL).strip().lower()
    if raw == _INHERIT:
        logger.warning("sdk_stdout_inherited", reason="HFT_SDK_STDOUT=inherit; SDK prints reach container logs")
        return _INHERIT
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        # A closed (ValueError) or broken (OSError) stdout must not stop the fd-1 redirect below.
        pass
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
    finally:
        os.close(devnull)
    if raw != _DEVNULL:
        logger.warning("sdk_stdout_unknown_mode", value=raw, applied=_DEVNULL)
    logger.info("sdk_stdout_isolated", fd=1, target=os.devnull)
    return _DEVNULL
