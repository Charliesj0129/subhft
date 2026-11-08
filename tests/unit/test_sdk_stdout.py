"""fd-1 isolation for broker-SDK prints (utils/sdk_stdout.py).

Each case runs in a child process: redirecting fd 1 inside the pytest process
would swallow pytest's own output. The child writes to fd 1 with ``os.write``,
the way the SDK's native core does, so ``sys.stdout`` buffering is not what is
being tested.
"""

from __future__ import annotations

import os
import subprocess
import sys

_CHILD = """
import os
from hft_platform.utils.sdk_stdout import isolate_sdk_stdout
mode = isolate_sdk_stdout()
os.write(1, b"Info: #P2P/v:x/y/PYAPI/A123456789/1023/ | Event: Session up\\n")
os.write(2, ("mode=" + mode + "\\n").encode())
"""


def _run_child(env_value: str | None) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if k != "HFT_SDK_STDOUT"}
    if env_value is not None:
        env["HFT_SDK_STDOUT"] = env_value
    return subprocess.run([sys.executable, "-c", _CHILD], capture_output=True, env=env, timeout=20, check=True)


def test_sdk_stdout_default_drops_fd1_writes():
    proc = _run_child(None)
    assert proc.stdout == b""
    assert b"mode=devnull" in proc.stderr


def test_sdk_stdout_inherit_keeps_fd1_writes():
    proc = _run_child("inherit")
    assert b"PYAPI/A123456789" in proc.stdout
    assert b"mode=inherit" in proc.stderr


def test_sdk_stdout_unknown_value_fails_closed_to_devnull():
    proc = _run_child("off")
    assert proc.stdout == b""
    assert b"mode=devnull" in proc.stderr


def test_sdk_stdout_main_entry_isolates_before_running():
    import hft_platform.main as main_mod

    source = open(main_mod.__file__, encoding="utf-8").read()
    entry = source.split('if __name__ == "__main__":', 1)[1]
    assert entry.index("isolate_sdk_stdout()") < entry.index("asyncio.run(main())")
