from __future__ import annotations

import argparse
import json
import time
from unittest.mock import MagicMock

from hft_platform.feed_adapter import shioaji_client as mod


class _DummyClient:
    def __init__(self, code: str) -> None:
        self.code = code
        self._enqueue_tick = MagicMock()


def main() -> int:
    parser = argparse.ArgumentParser(description="Microbench global Shioaji callback dispatch routing.")
    parser.add_argument("--clients", type=int, default=64)
    parser.add_argument("--iters", type=int, default=200000)
    args = parser.parse_args()

    with mod.CLIENT_REGISTRY_LOCK:
        mod.CLIENT_REGISTRY.clear()
        mod.CLIENT_REGISTRY_BY_CODE.clear()
    clients = []
    for i in range(args.clients):
        code = f"{2300 + i}"
        c = _DummyClient(code)
        clients.append(c)
        mod._registry_register(c)
        mod._registry_rebind_codes(c, [code])

    target_code = clients[args.clients // 2].code
    topic = f"Q/TSE/{target_code}"
    quote = type("Quote", (), {"code": target_code})()

    t0 = time.perf_counter()
    for _ in range(args.iters):
        mod.dispatch_tick_cb(topic, quote)
    t1 = time.perf_counter()
    us = (t1 - t0) / max(1, args.iters) * 1e6

    called_counts = [c._enqueue_tick.call_count for c in clients]
    payload = {
        "clients": args.clients,
        "iters": args.iters,
        "us_per_dispatch": us,
        "target_calls": max(called_counts),
        "non_target_calls": sum(called_counts) - max(called_counts),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
