"""Report-only hint: which KILLED manifests describe a mechanism like a new candidate's (candidate I2 / R05).

A hint is not a verdict. A KILL rests on that candidate's own backtest (kill-criteria skill; memory
``feedback_no_cross_candidate_kill_shortcuts``), so this tool never reads a result as evidence about the candidate, never
writes a manifest or a status, and always exits 0 on a normal run. Its only job is to say "an earlier candidate that
looks like this one was killed, and here is the stated reason" so the same ground is not re-walked by accident.

What is compared, per manifest: ``name``, ``hypothesis``, ``signal``, ``signal_type``, ``formula``, ``dsl_formula``,
``data_source`` and ``data_fields``. Text is lower-cased, split on non-letters, stripped of stopwords and of tokens that
start with a digit (horizons such as ``30m`` or ``15min``), of futures contract codes (``TXFD6``; the instrument is
shown, not scored), and a trailing plural ``s`` is dropped (not after ``ss``, ``is`` or ``us``). The score is the Jaccard index of the
two token sets; a hint needs at least ``--min-shared`` shared tokens (default 2) and ``--min-score`` (default 0.10).
``kill_reason`` is shown, never matched: it is full of generic words (cost, edge, trades) that every kill shares.

Source of the KILLED set: ``status == KILLED`` (any letter case) in ``research/alphas/*/manifest.yaml`` and
``research/archive/alphas_*/*/manifest.yaml``. ``research/alphas/_kill_ledger.jsonl`` is not read: it is an ignored,
local-only file, so a hint built on it would differ between checkouts.

Candidate input (exactly one): ``--manifest PATH``, ``--alpha-id ID`` (looked up in the same trees), or ad hoc
``--hypothesis TEXT`` with optional ``--name``, ``--data-fields a,b``, ``--instrument``.

Exit codes: 0 report (also when nothing is similar); 2 for a bad argument, an unreadable candidate or an unwritable
``--json-out``. An unreadable manifest in the scanned trees is skipped and listed, never fatal. Run via ``make research-similar-hint``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

NOTE = (
    "Hint only. A KILL rests on this candidate's own backtest; an earlier kill is context for what to check, "
    "not evidence about this candidate. Nothing was changed."
)

_TEXT_KEYS = ("name", "hypothesis", "signal", "signal_type", "formula", "dsl_formula", "data_source")
_REASON_KEYS = ("kill_reason", "correction_note")
_MIN_TOKEN_LEN = 2
_WORD = re.compile(r"(?<![a-z0-9])[a-z][a-z0-9]*")  # a word that starts with a letter: "15min" and "30m" yield nothing
_CONTRACT_CODE = re.compile(r"(txf|tmf|mtx)[a-z]?\d?")
_REASON_CHARS = 200

# Function words plus words every manifest uses; nothing that names a mechanism (reversion, imbalance, momentum, ...).
_STOPWORDS = frozenset(
    """
    a about above across after against all also an and any are as at be been before between both but by can could
    did do does each for from had has have how if in into is it its may more most not of on only or other our over per
    same should so some such than that the their then there these they this those through to under up use used using
    very was we were what when where which while who will with within without would
    alpha candidate class cost costs data day days edge feature features horizon hypothesis level levels model
    predict predicts predicted price prices pt pts retail return returns rt signal signals strategy test tests trade
    trades trading value values
    """.split()
)


@dataclass(frozen=True)
class ManifestDoc:
    alpha_id: str
    path: str  # repo-relative, posix
    location: str  # "active" | "archive"
    status: str | None
    instrument: str | None
    tokens: frozenset[str]
    data_fields: frozenset[str]
    reason: str | None  # kill_reason (or correction_note), whitespace collapsed and truncated


@dataclass(frozen=True)
class Hint:
    alpha_id: str
    path: str
    score: float
    shared_tokens: tuple[str, ...]
    shared_data_fields: tuple[str, ...]
    instrument: str | None
    same_instrument: bool | None  # None when either side names no instrument
    reason: str | None


def tokenize(text: str) -> frozenset[str]:
    """Mechanism words of ``text``: lower-case letters-led words, stopwords out, trailing plural ``s`` dropped."""
    out: set[str] = set()
    for word in _WORD.findall(text.lower().replace("_", " ")):
        if len(word) < _MIN_TOKEN_LEN or word in _STOPWORDS or _CONTRACT_CODE.fullmatch(word):
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "is", "us")):
            word = word[:-1]
        if word in _STOPWORDS:
            continue
        out.add(word)
    return frozenset(out)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value)
    return str(value)


def _field_names(value: Any) -> frozenset[str]:
    if isinstance(value, str):
        items: list[Any] = [part for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return frozenset()
    return frozenset(str(v).strip() for v in items if str(v).strip())


def _reason(body: dict[str, Any]) -> str | None:
    for key in _REASON_KEYS:
        text = " ".join(_as_text(body.get(key)).split())
        if text:
            return text if len(text) <= _REASON_CHARS else text[: _REASON_CHARS - 3] + "..."
    return None


def doc_from_mapping(body: dict[str, Any], *, path: str, location: str, fallback_id: str) -> ManifestDoc:
    fields = _field_names(body.get("data_fields"))
    text = " ".join(_as_text(body.get(k)) for k in _TEXT_KEYS) + " " + " ".join(sorted(fields))
    status = body.get("status")
    instrument = body.get("instrument")
    return ManifestDoc(
        alpha_id=str(body.get("alpha_id") or fallback_id),
        path=path,
        location=location,
        status=None if status is None else str(status).strip(),
        instrument=str(instrument).strip() if instrument else None,
        tokens=tokenize(text),
        data_fields=fields,
        reason=_reason(body),
    )


def _manifest_paths(root: Path) -> list[tuple[Path, str]]:
    found = [(p, "active") for p in sorted((root / "research" / "alphas").glob("*/manifest.yaml"))]
    found += [(p, "archive") for p in sorted((root / "research" / "archive").glob("alphas_*/*/manifest.yaml"))]
    return found


def scan(root: Path) -> tuple[list[ManifestDoc], list[str]]:
    """(documents, repo-relative paths that could not be read as a mapping)."""
    docs: list[ManifestDoc] = []
    skipped: list[str] = []
    for path, location in _manifest_paths(root):
        rel = path.relative_to(root).as_posix()
        try:
            body = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            skipped.append(rel)
            continue
        if not isinstance(body, dict):
            skipped.append(rel)
            continue
        docs.append(doc_from_mapping(body, path=rel, location=location, fallback_id=path.parent.name))
    return docs, skipped


def is_killed(doc: ManifestDoc) -> bool:
    return doc.status is not None and doc.status.upper() == "KILLED"


def rank(
    candidate: ManifestDoc,
    killed: list[ManifestDoc],
    *,
    top: int,
    min_score: float,
    min_shared: int,
) -> list[Hint]:
    hints: list[Hint] = []
    for doc in killed:
        if doc.alpha_id == candidate.alpha_id:
            continue
        shared = candidate.tokens & doc.tokens
        union = candidate.tokens | doc.tokens
        if not union or len(shared) < min_shared:
            continue
        score = len(shared) / len(union)
        if score < min_score:
            continue
        same = None
        if candidate.instrument and doc.instrument:
            same = candidate.instrument == doc.instrument
        hints.append(
            Hint(
                alpha_id=doc.alpha_id,
                path=doc.path,
                score=round(score, 3),
                shared_tokens=tuple(sorted(shared)),
                shared_data_fields=tuple(sorted(candidate.data_fields & doc.data_fields)),
                instrument=doc.instrument,
                same_instrument=same,
                reason=doc.reason,
            )
        )
    hints.sort(key=lambda h: (-h.score, h.alpha_id))
    return hints[:top]


def format_report(
    candidate: ManifestDoc, source: str, hints: list[Hint], scanned: int, killed: int, skipped: list[str]
) -> str:
    lines = [f"candidate: {candidate.alpha_id} ({source})", f"scanned {scanned} manifests, {killed} KILLED"]
    if skipped:
        lines.append(f"skipped (unreadable): {', '.join(skipped)}")
    if not hints:
        lines.append("no KILLED manifest passes the similarity threshold")
    for i, h in enumerate(hints, 1):
        inst = (
            ""
            if h.same_instrument is None
            else (" same instrument" if h.same_instrument else f" instrument {h.instrument}")
        )
        lines.append(f"{i}. {h.alpha_id}  score {h.score:.3f}{inst}")
        lines.append(f"   shared: {', '.join(h.shared_tokens)}")
        if h.shared_data_fields:
            lines.append(f"   shared data fields: {', '.join(h.shared_data_fields)}")
        lines.append(f"   stated reason: {h.reason or '(none in the manifest)'}")
        lines.append(f"   manifest: {h.path}")
    lines.append(NOTE)
    return "\n".join(lines)


def _candidate(args: argparse.Namespace, docs: list[ManifestDoc], root: Path) -> tuple[ManifestDoc, str]:
    if args.manifest is not None:
        path = Path(args.manifest)
        try:
            body = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ValueError(f"cannot read candidate manifest {path}: {exc}") from exc
        if not isinstance(body, dict):
            raise ValueError(f"candidate manifest {path} must be a YAML mapping")
        return doc_from_mapping(body, path=str(path), location="candidate", fallback_id=path.parent.name), str(path)
    if args.alpha_id is not None:
        for doc in docs:
            if doc.alpha_id == args.alpha_id:
                return doc, doc.path
        raise ValueError(f"alpha_id {args.alpha_id!r} is in no manifest under {root}")
    body = {
        "alpha_id": "(ad hoc)",
        "name": args.name,
        "hypothesis": args.hypothesis,
        "data_fields": args.data_fields,
        "instrument": args.instrument,
    }
    return doc_from_mapping(body, path="(ad hoc)", location="candidate", fallback_id="(ad hoc)"), "ad hoc"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--manifest", help="path to the candidate's manifest.yaml")
    who.add_argument("--alpha-id", help="alpha_id of a manifest already in research/alphas or research/archive")
    who.add_argument("--hypothesis", help="ad hoc candidate text (before a manifest exists)")
    ap.add_argument("--name", help="ad hoc candidate name")
    ap.add_argument("--data-fields", help="ad hoc candidate data fields, comma separated")
    ap.add_argument("--instrument", help="ad hoc candidate instrument (shown, not scored)")
    ap.add_argument("--root", default=str(REPO_ROOT), help="repository root to scan (default: this checkout)")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--min-score", type=float, default=0.10)
    ap.add_argument("--min-shared", type=int, default=2)
    ap.add_argument("--json-out", help="write the hints as JSON to this path")
    args = ap.parse_args(argv)

    if args.top < 1 or args.min_shared < 1 or not 0.0 <= args.min_score <= 1.0:
        print("--top and --min-shared must be >= 1 and --min-score within [0, 1]", file=sys.stderr)
        return 2
    if args.hypothesis is None and (args.name or args.data_fields or args.instrument):
        print(
            "--name, --data-fields and --instrument describe an ad hoc candidate and need --hypothesis", file=sys.stderr
        )
        return 2
    root = Path(args.root)
    docs, skipped = scan(root)
    try:
        candidate, source = _candidate(args, docs, root)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    killed = [d for d in docs if is_killed(d)]
    hints = rank(candidate, killed, top=args.top, min_score=args.min_score, min_shared=args.min_shared)
    print(format_report(candidate, source, hints, len(docs), len(killed), skipped))
    if args.json_out:
        payload = {
            "candidate": candidate.alpha_id,
            "source": source,
            "scanned": len(docs),
            "killed": len(killed),
            "skipped": skipped,
            "note": NOTE,
            "hints": [asdict(h) for h in hints],
        }
        try:
            Path(args.json_out).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"cannot write {args.json_out}: {exc}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
