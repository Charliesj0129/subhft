"""Generator-side JSONL drop ingestion + provenance header (spec §11).

The generator contract: a model (or the template expander in
``tools/make_smoke_batch.py``) produces a raw JSONL drop of candidate objects;
``generate`` validates the drop SHAPE only (one JSON object per line — content
validation is the runner's job), prepends a provenance header line

    {"_header": true, "prompt_id", "prompt_sha256", "generation_model",
     "generated_at", "generation_run_id"}

and writes ``candidates/<gen_run>/family=<f>.jsonl``.  The runner reads that
file back with :func:`read_family_jsonl` and stamps the header's provenance
fields onto every CH row for the batch.

Prompts live at ``prompts/v1/<family>.md`` with YAML frontmatter declaring
``prompt_id`` (``<family>__v1``), ``schema_ref`` and ``primitive_version``;
``prompt_sha256`` hashes the exact prompt file bytes so a silently edited
prompt is distinguishable in provenance.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from research.candidate_loop.schema import FAMILIES

HEADER_KEY = "_header"
DEFAULT_CANDIDATES_ROOT = Path("research/candidate_loop/candidates")
DEFAULT_PROMPTS_DIR = Path("research/candidate_loop/prompts/v1")


class DropShapeError(ValueError):
    """A generator drop or prompt violates the §11 shape contract."""


# ---------------------------------------------------------------------------
# Prompt provenance.
# ---------------------------------------------------------------------------


def prompt_sha256(prompt_path: Path) -> str:
    return hashlib.sha256(prompt_path.read_bytes()).hexdigest()


def read_prompt_frontmatter(prompt_path: Path) -> dict[str, Any]:
    """Parse the leading ``---`` YAML frontmatter block of a prompt file."""
    text = prompt_path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise DropShapeError(f"prompt missing YAML frontmatter: {prompt_path}")
    end = text.find("\n---", 4)
    if end < 0:
        raise DropShapeError(f"prompt frontmatter unterminated: {prompt_path}")
    data = yaml.safe_load(text[4:end])
    if not isinstance(data, dict) or "prompt_id" not in data:
        raise DropShapeError(f"prompt frontmatter must define prompt_id: {prompt_path}")
    return data


def build_header(
    prompt_path: Path,
    generation_model: str,
    generation_run_id: str,
    generated_at: str,
) -> dict[str, Any]:
    frontmatter = read_prompt_frontmatter(prompt_path)
    return {
        HEADER_KEY: True,
        "prompt_id": str(frontmatter["prompt_id"]),
        "prompt_sha256": prompt_sha256(prompt_path),
        "generation_model": generation_model,
        "generated_at": generated_at,
        "generation_run_id": generation_run_id,
    }


# ---------------------------------------------------------------------------
# Drop shape validation + family file IO.
# ---------------------------------------------------------------------------


def ingest_jsonl(src_path: Path, expected_count: int | None = None) -> list[str]:
    """Read a raw drop: every non-empty line must be a JSON object.

    Pre-existing header lines are skipped (re-generating from an already
    headered file is idempotent).  Content validation (schema, formulas,
    dedupe) is intentionally NOT done here — invalid candidates must reach the
    runner so they are recorded as INVALID rows with death reasons (spec §13).
    """
    lines: list[str] = []
    for lineno, raw in enumerate(src_path.read_text(encoding="utf-8").splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DropShapeError(f"{src_path.name}:{lineno} is not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise DropShapeError(f"{src_path.name}:{lineno} is not a JSON object")
        if obj.get(HEADER_KEY):
            continue
        lines.append(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    if expected_count is not None and len(lines) != expected_count:
        raise DropShapeError(
            f"{src_path.name}: expected {expected_count} candidates, got {len(lines)}"
        )
    return lines


def family_jsonl_path(candidates_root: Path, gen_run_id: str, family: str) -> Path:
    return candidates_root / gen_run_id / f"family={family}.jsonl"


def family_from_filename(path: Path) -> str:
    """``family=trade_flow.jsonl`` -> ``trade_flow``."""
    stem = path.stem
    if not stem.startswith("family="):
        raise DropShapeError(f"batch file must be named family=<family>.jsonl: {path.name}")
    return stem.split("=", 1)[1]


def write_family_jsonl(out_path: Path, header: dict[str, Any], candidate_lines: list[str]) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join([json.dumps(header, sort_keys=True), *candidate_lines])
    out_path.write_text(body + "\n", encoding="utf-8")
    return out_path


def read_family_jsonl(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    """Read back ``(header|None, candidate_lines)``.

    Undecodable candidate lines are passed through verbatim so the validator
    can assign them ``SCHEMA_INVALID`` (the runner never drops lines).
    """
    header: dict[str, Any] | None = None
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            lines.append(raw)
            continue
        if isinstance(obj, dict) and obj.get(HEADER_KEY):
            if header is None:
                header = obj
            continue
        lines.append(raw)
    return header, lines


def generate_drop(
    *,
    gen_run_id: str,
    family: str,
    prompt_path: Path,
    from_jsonl: Path,
    expected_count: int | None = None,
    generation_model: str,
    generated_at: str,
    candidates_root: Path = DEFAULT_CANDIDATES_ROOT,
) -> Path:
    """The ``generate`` CLI body: ingest a raw drop into a headered family file."""
    if family not in FAMILIES:
        raise DropShapeError(f"unknown family {family!r} (want one of {sorted(FAMILIES)})")
    lines = ingest_jsonl(from_jsonl, expected_count)
    header = build_header(prompt_path, generation_model, gen_run_id, generated_at)
    return write_family_jsonl(family_jsonl_path(candidates_root, gen_run_id, family), header, lines)


__all__ = [
    "DEFAULT_CANDIDATES_ROOT",
    "DEFAULT_PROMPTS_DIR",
    "DropShapeError",
    "HEADER_KEY",
    "build_header",
    "family_from_filename",
    "family_jsonl_path",
    "generate_drop",
    "ingest_jsonl",
    "prompt_sha256",
    "read_family_jsonl",
    "read_prompt_frontmatter",
    "write_family_jsonl",
]
