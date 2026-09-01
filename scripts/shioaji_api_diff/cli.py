"""Argparse CLI for the shioaji-api-diff tool.

Subcommands:
  orchestrate   Install each version in a throwaway venv and write its surface.
  diff          Print a classified diff between two captured surfaces (JSON).
  report        Write machine diff JSON(s) + the Markdown runbook.
  guard-regen   Recapture the CURRENTLY-installed shioaji surface into its golden.
  watch         List PyPI releases newer than the pin, with the decision recorded for each.
  decision-evidence  Rebuild the pin-to-candidate diff every ledger entry cites.

``diff``/``report`` read only committed JSON (no venv, no network).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import decisions as decisions_mod
from . import report
from . import watch as watch_mod
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
    doc = report.build_diff_doc(args.from_v, args.to_v, _load_surface(args.from_v), _load_surface(args.to_v))
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


def _cmd_watch(args: argparse.Namespace) -> int:
    """Report PyPI releases newer than the pin and what was decided about each.

    Exits non-zero under ``--strict`` when any release still needs attention:
    no captured surface, no recorded decision, or a deferral that has come due.
    A scheduled job uses that as its failure condition. ``--releases-json``
    reads a saved PyPI payload instead of the network.
    """
    pin = watch_mod.read_pin()
    ledger_pin, decisions = decisions_mod.load_decisions(args.ledger)
    if ledger_pin != pin:
        # Every verdict in the ledger was reached relative to ledger_pin. If the
        # requirement moved, none of them describe the upgrade in front of us --
        # and silently reporting them against the new pin would be worse than
        # reporting nothing.
        raise SystemExit(
            f"decision ledger is written against shioaji=={ledger_pin} but pyproject.toml pins "
            f"{pin}; rewrite docs/runbooks/shioaji-release-decisions.yaml against the new pin"
        )
    if args.releases_json:
        payload = json.loads(Path(args.releases_json).read_text(encoding="utf-8"))
        versions = watch_mod.releases_from_payload(payload)
    else:
        versions = watch_mod.fetch_releases()
    releases = watch_mod.newer_releases(pin, versions, decisions)
    doc = watch_mod.build_report(pin, releases)
    sys.stdout.write(report.canonical_json(doc) if args.json else watch_mod.render_text(doc))
    return 1 if doc["counts"]["needs_action"] and args.strict else 0


def _cmd_decision_evidence(args: argparse.Namespace) -> int:
    """Rebuild, from committed surfaces, the diff each ledger decision cites.

    The ledger requires ``diff_<pin>_to_<version>.json`` because that is the
    upgrade actually being decided; ``report`` writes consecutive hops, which
    tell the story of the SDK's evolution instead. Both are derived from the
    same surface snapshots, so this needs no network and no broker login.
    """
    pin, records = decisions_mod.load_decisions(args.ledger)
    for version in sorted(records):
        doc = report.build_diff_doc(pin, version, _load_surface(pin), _load_surface(version))
        out = GOLDEN_DIR / f"diff_{pin}_to_{version}.json"
        _write(out, report.canonical_json(doc))
        sys.stderr.write(f"[ok]   wrote {out.name} (verdict {doc['verdict']})\n")
    return 0


def _consecutive_pairs(versions: list[str]) -> list[tuple[str, str]]:
    return [(versions[i], versions[i + 1]) for i in range(len(versions) - 1)]


def _pair(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise argparse.ArgumentTypeError("pair must be FROM:TO, e.g. 1.3.3:1.5.3")
    a, b = text.split(":", 1)
    return a, b


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.shioaji_api_diff", description=__doc__)
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
    p_rep.add_argument(
        "--versions",
        nargs="+",
        default=list(DEFAULT_VERSIONS),
        help="consecutive pairs are diffed unless --pair is given",
    )
    p_rep.add_argument("--pair", dest="pairs", type=_pair, action="append", help="explicit FROM:TO pair (repeatable)")
    p_rep.add_argument("--date", default=None, help="snapshot date stamped in the runbook")
    p_rep.set_defaults(func=_cmd_report)

    p_watch = sub.add_parser("watch", help="list PyPI releases newer than the pin")
    p_watch.add_argument("--json", action="store_true", help="emit the machine report")
    p_watch.add_argument(
        "--releases-json", default=None, help="read a saved PyPI payload instead of calling the network"
    )
    p_watch.add_argument(
        "--ledger", default=None, help="decision ledger path (default docs/runbooks/shioaji-release-decisions.yaml)"
    )
    p_watch.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 when a release lacks a surface, lacks a decision, or its deferral came due",
    )
    p_watch.set_defaults(func=_cmd_watch)

    p_eviden = sub.add_parser("decision-evidence", help="rebuild the pin-to-candidate diff each ledger decision cites")
    p_eviden.add_argument(
        "--ledger", default=None, help="decision ledger path (default docs/runbooks/shioaji-release-decisions.yaml)"
    )
    p_eviden.set_defaults(func=_cmd_decision_evidence)

    p_guard = sub.add_parser("guard-regen", help="recapture the installed surface into its golden")
    p_guard.set_defaults(func=_cmd_guard_regen)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))
