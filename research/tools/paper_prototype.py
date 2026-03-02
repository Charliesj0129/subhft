from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAPER_INDEX = PROJECT_ROOT / "research" / "knowledge" / "paper_index.json"


def _normalize_alpha_id(text: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "_" for ch in text.strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned[:48].strip("_")


def _load_index() -> dict[str, Any]:
    if not PAPER_INDEX.exists():
        return {}
    try:
        payload = json.loads(PAPER_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_index(index: dict[str, Any]) -> None:
    PAPER_INDEX.parent.mkdir(parents=True, exist_ok=True)
    PAPER_INDEX.write_text(json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _resolve_paper(index: dict[str, Any], ref_or_arxiv: str) -> tuple[str | None, dict[str, Any] | None]:
    key = str(ref_or_arxiv).strip()
    if key in index and isinstance(index[key], dict):
        return key, index[key]
    for ref, row in index.items():
        if not isinstance(row, dict):
            continue
        if str(row.get("arxiv_id", "")).strip() == key:
            return str(ref), row
    return None, None


def cmd_paper_to_prototype(args: argparse.Namespace) -> int:
    index = _load_index()
    ref, row = _resolve_paper(index, str(args.paper_ref))
    if ref is None or row is None:
        print(f"[paper_to_prototype] paper_ref not found: {args.paper_ref}")
        return 2

    title = str(row.get("title", "alpha_prototype"))
    alpha_id = _normalize_alpha_id(str(args.alpha_id or title))
    if not alpha_id:
        print("[paper_to_prototype] failed to derive alpha_id")
        return 2

    cmd = [
        sys.executable,
        "-m",
        "research",
        "scaffold",
        alpha_id,
        "--paper",
        str(ref),
        "--complexity",
        str(args.complexity),
    ]
    if bool(args.force):
        cmd.append("--force")
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False)
    if proc.stdout.strip():
        print(proc.stdout.strip())
    if proc.returncode != 0:
        if proc.stderr.strip():
            print(proc.stderr.strip())
        return int(proc.returncode or 1)

    alphas = row.get("alphas")
    if not isinstance(alphas, list):
        alphas = []
    if alpha_id not in {str(x) for x in alphas}:
        alphas.append(alpha_id)
    row["alphas"] = sorted({str(x) for x in alphas})
    row["status"] = str(row.get("status", "reviewed") or "reviewed")
    index[str(ref)] = row
    _save_index(index)
    print(
        "[paper_to_prototype] linked "
        f"paper_ref={ref} arxiv_id={row.get('arxiv_id')} -> alpha_id={alpha_id}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge paper index to governed prototype scaffold.")
    sub = parser.add_subparsers(dest="command", required=True)

    p2p = sub.add_parser("paper-to-prototype")
    p2p.add_argument("paper_ref", help="paper ref key (e.g. 120) or arxiv_id")
    p2p.add_argument("--alpha-id", default=None, help="Optional alpha id override")
    p2p.add_argument("--complexity", default="O1")
    p2p.add_argument("--force", action="store_true")
    p2p.set_defaults(func=cmd_paper_to_prototype)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
