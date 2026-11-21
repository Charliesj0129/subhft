"""CLI: python -m research.data_pipeline.hist_ingest {ticks,kbars,status}.

ticks  Shioaji whole-day ticks, newest day first, quota-guarded (refuses Taipei weekday 08:30-13:50 unless allowed).
kbars  1-minute K bars from Fubon (default) or Shioaji, in windows from the oldest pending weekday.
status Watermark summary per source/product/kind/status.
Credentials come from the environment; --env-file (default ./.env) is loaded without overriding it and never printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import structlog

from . import fubon_source, shioaji_source
from .model import TAIPEI, IntradayRefusedError, SourceError, intraday_tick_cap, last_completed_trading_day
from .store import HistStore

log = structlog.get_logger("hist_ingest")
MIB = 1024 * 1024


def _date(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def build_parser() -> argparse.ArgumentParser:
    env = argparse.ArgumentParser(add_help=False)
    env.add_argument("--env-file", default=".env", help="dotenv file to load if it exists (default: ./.env)")
    common = argparse.ArgumentParser(add_help=False, parents=[env])
    common.add_argument("--products", default="TXF,MXF,TMF", help="comma separated futures products")
    common.add_argument("--since", type=_date, help="first trading day (default: today - 30 days)")
    common.add_argument("--until", type=_date, help="last trading day (default: last completed trading day)")
    common.add_argument("--force", action="store_true", help="refetch days already DONE or EMPTY")
    common.add_argument("--dry-run", action="store_true", help="fetch and convert, print counts, write nothing")
    common.add_argument("--min-remaining-mb", type=float, default=50.0, help="Shioaji quota floor in MiB")
    common.add_argument("--max-tick-days", type=int, default=20, help="Shioaji tick calls per run")
    common.add_argument("--allow-intraday", action="store_true", help="allow Shioaji ticks 08:30-13:50 (capped at 8)")
    parser = argparse.ArgumentParser(prog="python -m research.data_pipeline.hist_ingest", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ticks", parents=[common], help="Shioaji historical ticks")
    kbars = sub.add_parser("kbars", parents=[common], help="1-minute K bars")
    kbars.add_argument("--source", choices=("fubon", "shioaji"), default="fubon")
    sub.add_parser("status", parents=[env], help="watermark summary")
    return parser


def load_env_file(path: str) -> None:
    """Load KEY=VALUE pairs without overriding the environment (python-dotenv if installed, else a minimal parser)."""
    target = Path(path)
    if not target.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        for line in target.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key and not key.startswith("#"):
                value = value.strip()
                if value[:1] in {'"', "'"}:
                    value = value[1:].split(value[0], 1)[0]
                else:
                    value = value.split(" #", 1)[0].strip()
                os.environ.setdefault(key.strip(), value)
    else:
        load_dotenv(target, override=False)


def _ingest(args: argparse.Namespace, src: Any, store: HistStore) -> Counter[str]:
    common = {"products": args.products, "since": args.since, "until": args.until, "force": args.force}
    if args.command == "kbars" and args.source == "fubon":
        return fubon_source.ingest_kbars(src, store, **common)
    if args.command == "ticks":
        return shioaji_source.ingest_ticks(src, store, **common, min_remaining_bytes=args.floor, max_calls=args.cap)
    return shioaji_source.ingest_kbars(src, store, **common, min_remaining_bytes=args.floor)


def main(argv: Sequence[str] | None = None, *, now: dt.datetime | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = now or dt.datetime.now(TAIPEI)
    load_env_file(args.env_file)
    if args.command == "status":
        for row in HistStore.from_env().summary():
            sys.stdout.write("\t".join(str(c) for c in row) + "\n")
        return 0
    args.products = [p.strip().upper() for p in args.products.split(",") if p.strip()]
    args.since = args.since or now.date() - dt.timedelta(days=30)
    args.until = min(args.until or dt.date.max, last_completed_trading_day(now))
    args.floor = int(args.min_remaining_mb * MIB)
    try:
        args.cap = intraday_tick_cap(now, args.allow_intraday, args.max_tick_days) if args.command == "ticks" else 0
    except IntradayRefusedError as exc:
        log.error("intraday_refused", reason=str(exc))
        return 2
    store = HistStore.from_env(dry_run=args.dry_run)
    try:
        fubon = args.command == "kbars" and args.source == "fubon"
        src = fubon_source.FubonSource.connect() if fubon else shioaji_source.ShioajiSource.connect()
    except SourceError as exc:
        log.error("connect_failed", error=str(exc))
        return 1
    try:
        stats = _ingest(args, src, store)
    except SourceError as exc:
        log.error("ingest_aborted", error=str(exc))
        return 1
    finally:
        src.close()
    log.info("ingest_done", command=args.command, dry_run=args.dry_run, **stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
