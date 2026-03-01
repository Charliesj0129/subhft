from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from research.tools.paper_autofill import infer_spec_from_text, suggest_alpha_id

ARXIV_API = "https://export.arxiv.org/api/query"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = PROJECT_ROOT / "research" / "knowledge"
PAPER_INDEX = KNOWLEDGE_DIR / "paper_index.json"
NOTES_DIR = KNOWLEDGE_DIR / "notes"
NS = {"atom": "http://www.w3.org/2005/Atom"}


def _fetch_xml(url: str) -> ET.Element:
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
        payload = resp.read()
    return ET.fromstring(payload)  # noqa: S314


def _safe_text(parent: ET.Element, path: str, default: str = "") -> str:
    node = parent.find(path, NS)
    if node is None or node.text is None:
        return default
    return node.text.strip()


def _parse_entry(entry: ET.Element) -> dict[str, Any]:
    raw_id = _safe_text(entry, "atom:id")
    arxiv_id = raw_id.split("/abs/")[-1].strip()
    title = _safe_text(entry, "atom:title").replace("\n", " ").strip()
    abstract = _safe_text(entry, "atom:summary").replace("\n", " ").strip()
    published = _safe_text(entry, "atom:published")
    updated = _safe_text(entry, "atom:updated")
    authors = [
        _safe_text(author, "atom:name")
        for author in entry.findall("atom:author", NS)
        if _safe_text(author, "atom:name")
    ]
    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "abstract": abstract,
        "authors": authors,
        "published": published,
        "updated": updated,
    }


def _load_index() -> dict[str, Any]:
    if not PAPER_INDEX.exists():
        return {}
    try:
        payload = json.loads(PAPER_INDEX.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _save_index(index: dict[str, Any]) -> None:
    PAPER_INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAPER_INDEX.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(index, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(PAPER_INDEX)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    return slug[:60] or "paper"


def _normalize_arxiv_id(value: str) -> str:
    text = str(value or "").strip()
    if "/abs/" in text:
        text = text.split("/abs/")[-1].strip()
    return re.sub(r"v\d+$", "", text)


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _find_existing_paper_ref(index: dict[str, Any], paper: dict[str, Any]) -> str | None:
    incoming_id = _normalize_arxiv_id(str(paper.get("arxiv_id", "")))
    incoming_title = _normalize_title(str(paper.get("title", "")))
    for ref, row in index.items():
        if not isinstance(row, dict):
            continue
        row_id = _normalize_arxiv_id(str(row.get("arxiv_id", "")))
        if incoming_id and row_id and incoming_id == row_id:
            return str(ref)
        if (not row_id) and incoming_title and _normalize_title(str(row.get("title", ""))) == incoming_title:
            return str(ref)
    return None


def _next_ref(index: dict[str, Any]) -> str:
    numeric = [int(key) for key in index.keys() if str(key).isdigit()]
    return str(max(numeric, default=119) + 1).zfill(3)


def _note_path(ref: str, title: str) -> Path:
    return NOTES_DIR / f"{ref}_{_slug(title)}.md"


def _note_template(paper: dict[str, Any], ref: str) -> str:
    authors = list(paper.get("authors", []))
    authors_str = ", ".join(authors[:3])
    if len(authors) > 3:
        authors_str += " et al."
    title = str(paper.get("title", ""))
    abstract = str(paper.get("abstract", ""))
    spec = infer_spec_from_text(title, abstract, arxiv_ids=(str(paper.get("arxiv_id", "")),))
    feature_lines = "".join(f"- `{field}`\n" for field in spec.data_fields) or "- `spread_scaled`\n"
    alpha_id = suggest_alpha_id(title)
    return (
        f"# {title}\n\n"
        f"ref: {ref}\n"
        f"arxiv: https://arxiv.org/abs/{paper['arxiv_id']}\n"
        f"Authors: {authors_str}\n"
        f"Published: {paper.get('published', '')}\n\n"
        "## Abstract\n"
        f"{abstract}\n\n"
        "## Hypothesis\n"
        f"- {spec.hypothesis}\n\n"
        "## Candidate Formula\n"
        f"- `{spec.formula}`\n\n"
        "## Relevant Features (lob_shared_v1)\n"
        f"{feature_lines}\n"
        "## Implementation Notes\n"
        f"- Suggested alpha_id: `{alpha_id}`\n"
        f"- Scaffold: `python -m research scaffold {alpha_id} --paper {ref}`\n"
        f"- Bridge flow: `python -m research paper-to-prototype {ref} --alpha-id {alpha_id}`\n"
    )


def cmd_fetch_paper(args: argparse.Namespace) -> int:
    arxiv_id = str(args.arxiv_id).split("/abs/")[-1].strip()
    url = f"{ARXIV_API}?id_list={urllib.parse.quote(arxiv_id)}"
    root = _fetch_xml(url)
    entries = root.findall("atom:entry", NS)
    if not entries:
        print(f"[fetch_paper] Not found: {arxiv_id}", flush=True)
        return 1

    paper = _parse_entry(entries[0])
    index = _load_index()
    existing_ref = _find_existing_paper_ref(index, paper)
    if existing_ref is not None:
        row = index.get(existing_ref, {})
        if isinstance(row, dict) and not row.get("arxiv_id"):
            row["arxiv_id"] = str(paper["arxiv_id"])
            index[existing_ref] = row
            _save_index(index)
        existing_title = row.get("title", paper["title"]) if isinstance(row, dict) else paper["title"]
        print(f"[fetch_paper] Already indexed as ref={existing_ref}: {existing_title}")
        return 0

    ref = _next_ref(index)
    note_path = _note_path(ref, str(paper["title"]))
    note_file = str(note_path.relative_to(PROJECT_ROOT))
    index[ref] = {
        "ref": ref,
        "arxiv_id": paper["arxiv_id"],
        "title": paper["title"],
        "note_file": note_file,
        "status": "reviewed",
        "alphas": [],
        "tags": [],
    }
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    if not note_path.exists():
        note_path.write_text(_note_template(paper, ref), encoding="utf-8")
    _save_index(index)
    print(f"[fetch_paper] Indexed ref={ref}: {paper['title']}")
    print(f"[fetch_paper] Note: {note_path}")
    return 0


def cmd_search_papers(args: argparse.Namespace) -> int:
    query = urllib.parse.quote(str(args.query))
    max_results = max(1, int(args.max))
    url = f"{ARXIV_API}?search_query=all:{query}&max_results={max_results}&sortBy=relevance"
    root = _fetch_xml(url)
    entries = root.findall("atom:entry", NS)
    if not entries:
        print("[search_papers] No results.")
        return 0

    for i, entry in enumerate(entries, 1):
        paper = _parse_entry(entry)
        abstract = str(paper["abstract"])
        if len(abstract) > 120:
            abstract = abstract[:120].rstrip() + "..."
        print(f"{i:2}. [{paper['arxiv_id']}] {paper['title']}")
        print(f"    {abstract}")
    print("\nFetch a paper: python -m research fetch-paper <arxiv_id>")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Arxiv paper fetch and index tool.")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch-paper")
    fetch.add_argument("arxiv_id")
    fetch.set_defaults(func=cmd_fetch_paper)

    search = sub.add_parser("search-papers")
    search.add_argument("query")
    search.add_argument("--max", type=int, default=10)
    search.set_defaults(func=cmd_search_papers)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
