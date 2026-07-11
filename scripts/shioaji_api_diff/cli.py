"""Argparse CLI for the shioaji-api-diff tool.

Subcommands:
  orchestrate   Install each version in a throwaway venv and write its surface.
  diff          Print a classified diff between two captured surfaces (JSON).
  report        Write machine diff JSON(s) + the Markdown runbook.
  guard-regen   Recapture the CURRENTLY-installed shioaji surface into its golden.

``diff``/``report`` read only committed JSON (no venv, no network).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import report
from ._capture_entrypoint import build_surface_snapshot, canonical_json
from .paths import DEFAULT_VERSIONS, GOLDEN_DIR, RUNBOOK_PATH


def _load_surface(version: str) -> dict[str, Any]:
    path = GOLDEN_DIR / f"surface_{version}.json"
    if not path.exists():
        raise SystemExit(f"missing surface snapshot: {path} (run `orchestrate` first)")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _cmd_orchestrate(args: argparse.Namespace) -> int:
    from .orchestrator import orchestrate
    orchestrate(args.versions, refresh=args.refresh, keep_venv=args.keep_venv, jobs=args.jobs)
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    doc = report.build_diff_doc(args.from_v, args.to_v,
                                _load_surface(args.from_v), _load_surface(args.to_v))
    sys.stdout.write(report.canonical_json(doc))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    pairs = args.pairs or _consecutive_pairs(args.versions)
    docs: list[dict[str, Any]] = []
    for from_v, to_v in pairs:
        doc = report.build_diff_doc(from_v, to_v, _load_surface(from_v), _load_surface(to_v))
        out = GOLDEN_DIR / f"diff_{from_v}_to_{to_v}.json"
        _write(out, report.canonical_json(doc))
        sys.stderr.write(f"[ok]   wrote {out.name} (verdict {doc['verdict']})\n")
        docs.append(doc)
    _write(RUNBOOK_PATH, report.render_markdown(docs, generated_on=args.date))
    sys.stderr.write(f"[ok]   wrote {RUNBOOK_PATH}\n")
    return 0


def _cmd_guard_regen(args: argparse.Namespace) -> int:
    snapshot = build_surface_snapshot()
    version = (snapshot.get("dist") or {}).get("version")
    if not version:
        raise SystemExit("could not resolve installed shioaji version")
    out = GOLDEN_DIR / f"surface_{version}.json"
    _write(out, canonical_json(snapshot))
    sys.stderr.write(f"[ok]   regenerated {out.name} (sha {snapshot['snapshot_sha256'][:12]})\n")
    return 0


def _consecutive_pairs(versions: list[str]) -> list[tuple[str, str]]:
    return [(versions[i], versions[i + 1]) for i in range(len(versions) - 1)]


def _pair(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise argparse.ArgumentTypeError("pair must be FROM:TO, e.g. 1.3.3:1.5.3")
    a, b = text.split(":", 1)
    return a, b


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.shioaji_api_diff",
                                     description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_orch = sub.add_parser("orchestrate", help="capture surfaces in throwaway venvs")
    p_orch.add_argument("--versions", nargs="+", default=list(DEFAULT_VERSIONS))
    p_orch.add_argument("--refresh", action="store_true", help="rebuild even if snapshot exists")
    p_orch.add_argument("--jobs", type=int, default=3)
    p_orch.add_argument("--keep-venv", dest="keep_venv", action="store_true", default=True)
    p_orch.add_argument("--no-keep-venv", dest="keep_venv", action="store_false")
    p_orch.set_defaults(func=_cmd_orchestrate)

    p_diff = sub.add_parser("diff", help="classified diff between two captured surfaces")
    p_diff.add_argument("--from", dest="from_v", required=True)
    p_diff.add_argument("--to", dest="to_v", required=True)
    p_diff.set_defaults(func=_cmd_diff)

    p_rep = sub.add_parser("report", help="write diff JSONs + the Markdown runbook")
    p_rep.add_argument("--versions", nargs="+", default=list(DEFAULT_VERSIONS),
                       help="consecutive pairs are diffed unless --pair is given")
    p_rep.add_argument("--pair", dest="pairs", type=_pair, action="append",
                       help="explicit FROM:TO pair (repeatable)")
    p_rep.add_argument("--date", default=None, help="snapshot date stamped in the runbook")
    p_rep.set_defaults(func=_cmd_report)

    p_guard = sub.add_parser("guard-regen", help="recapture the installed surface into its golden")
    p_guard.set_defaults(func=_cmd_guard_regen)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
