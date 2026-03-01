#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hft_platform.diagnostics.replay import build_timeline, filter_traces, load_traces, render_timeline_markdown


def main() -> int:
    ap = argparse.ArgumentParser(description="Render incident timeline from decision trace JSONL")
    ap.add_argument("trace_file", help="Trace JSONL path")
    ap.add_argument("--trace-id", help="Filter trace_id")
    ap.add_argument("--stage", help="Filter stage")
    ap.add_argument("--format", choices=["json", "md"], default="md")
    ap.add_argument("--out", help="Output file path")
    args = ap.parse_args()

    records = load_traces(args.trace_file)
    records = filter_traces(records, trace_id=args.trace_id, stage=args.stage)
    payload = build_timeline(records)
    if args.format == "md":
        text = render_timeline_markdown(payload)
    else:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

