from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import yaml

from hft_platform.feed_adapter import shioaji_client as mod


def _default_symbols_config() -> str:
    env = os.getenv("SYMBOLS_CONFIG")
    if env:
        return env
    if Path("config/symbols.yaml").exists():
        return "config/symbols.yaml"
    return "config/base/symbols.yaml"


def _topic_variants(code: str, exchange: str) -> list[str]:
    exch = (exchange or "TSE").upper()
    if exch in {"TSE", "OTC", "OES"}:
        return [f"Q/{exch}/{code}", f"L1:STK:{code}:tick"]
    return [f"Quote:v1:BidAsk:{code}", f"L1:FOP:{code}:tick"]


def main() -> int:
    p = argparse.ArgumentParser(
        description="Validate symbols config topic→code parsing coverage after make sync-symbols (Shioaji routing sanity check)."
    )
    p.add_argument("--config", default=_default_symbols_config())
    p.add_argument("--limit", type=int, default=0, help="Only validate first N symbols (0=all).")
    args = p.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    symbols = cfg.get("symbols", [])
    if args.limit and args.limit > 0:
        symbols = symbols[: args.limit]

    failures: list[dict[str, Any]] = []
    checked = 0
    for sym in symbols:
        if not isinstance(sym, dict):
            continue
        code = str(sym.get("code") or "").strip()
        exchange = str(sym.get("exchange") or "").strip()
        if not code:
            continue
        for topic in _topic_variants(code, exchange):
            parsed = mod._extract_code_from_topic(topic)
            checked += 1
            if str(parsed) != code:
                failures.append({"code": code, "exchange": exchange, "topic": topic, "parsed": parsed})

    payload = {
        "config": args.config,
        "symbols": len(symbols),
        "topics_checked": checked,
        "failures": failures[:50],
        "failure_count": len(failures),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
