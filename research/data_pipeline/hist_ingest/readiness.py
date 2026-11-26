from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from http.client import HTTPException
from urllib.error import URLError
from urllib.request import Request, urlopen

from .store import clickhouse_endpoint


def http_ping(host: str, port: int, timeout: float) -> bool:
    address = f"[{host}]" if ":" in host and not host.startswith("[") else host
    try:
        with urlopen(Request(f"http://{address}:{port}/ping"), timeout=timeout) as response:
            return response.read(16).strip() == b"Ok."
    except (OSError, URLError, HTTPException, ValueError):
        return False


def wait_for_clickhouse(
    host: str,
    port: int,
    *,
    timeout_s: float = 120.0,
    ping: Callable[[str, int, float], bool] = http_ping,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = clock() + timeout_s
    while (remaining := deadline - clock()) > 0:
        if ping(host, port, min(2.0, remaining)):
            return True
        remaining = deadline - clock()
        if remaining > 0:
            sleep(min(1.0, remaining))
    return False


def main(argv: Sequence[str] | None = None) -> int:
    from .__main__ import load_env_file

    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)
    load_env_file(args.env_file)
    try:
        ready = wait_for_clickhouse(*clickhouse_endpoint())
    except ValueError:
        ready = False
    if not ready:
        sys.stderr.write("clickhouse_not_ready: HTTP /ping unavailable after 120s\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
