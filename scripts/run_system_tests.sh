#!/usr/bin/env bash
set -euo pipefail

compose_file="docker-compose.test.yml"

docker compose -f "${compose_file}" up -d

.venv/bin/python - <<'PY'
import socket
import time

deadline = time.time() + 30
while time.time() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", 8123), timeout=1):
            break
    except OSError:
        time.sleep(0.5)
else:
    raise SystemExit("ClickHouse not reachable on :8123")
PY

PYTHONPATH=src .venv/bin/python -m pytest -m "system or acceptance"

docker compose -f "${compose_file}" down -v
