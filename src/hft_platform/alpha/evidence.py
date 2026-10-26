"""Research evidence root: where governed runs publish (ADR-005 section 6).

Code lives in the public repository; evidence and data do not. Two roots,
both outside the checkout:

``$HFT_RESEARCH_EVIDENCE_ROOT`` (default ``~/hft_research_evidence``)
    ``runs/<run_id>/`` and ``validations/<alpha_id>/<stamp>/`` as the
    ExperimentTracker writes them, each sealed by a ``manifest.json`` that
    lists the sha256 of every file; ``ledger/verdicts.jsonl``, the
    append-only verdict events that alpha status will be derived from.
``$HFT_RESEARCH_DATA_ROOT`` (default ``~/hft_research_data``)
    datasets; not in git.

A governed run needs the evidence root set explicitly (no default), a clean
tree with a known commit, and a resolved profile; anything else may run only
as exploratory, which is labelled and never reaches the ledger.

The roots are read from the environment when called, never at import, and a
relative value is refused: it would resolve against the cwd.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from hft_platform.alpha.paths import RepoRootNotFoundError, repo_root
from hft_platform.alpha.provenance import file_sha256
from hft_platform.core import timebase

EVIDENCE_ROOT_ENV = "HFT_RESEARCH_EVIDENCE_ROOT"
DATA_ROOT_ENV = "HFT_RESEARCH_DATA_ROOT"
DEFAULT_EVIDENCE_ROOT = "~/hft_research_evidence"
DEFAULT_DATA_ROOT = "~/hft_research_data"

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "evidence_manifest.v1"
VERDICT_EVENT_SCHEMA = "verdict_event.v1"
LEDGER_RELPATH = "ledger/verdicts.jsonl"

_REQUIRED_EVENT_FIELDS = ("alpha_id", "stage", "passed")


class RunMode(StrEnum):
    GOVERNED = "governed"
    EXPLORATORY = "exploratory"


class EvidenceRootError(ValueError):
    """An evidence or data root is unset where required, relative, or inside the checkout."""


class GovernedRunRefused(RuntimeError):
    """A governed run's preconditions do not hold; ``reasons`` names each one."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("governed run refused: " + "; ".join(reasons))


class EvidenceSealedError(FileExistsError):
    """The directory already carries a manifest; sealed evidence is never rewritten."""


def _resolve_root(env: str, default: str, *, required: bool) -> Path:
    raw = os.environ.get(env, "").strip()
    if not raw:
        if required:
            raise EvidenceRootError(f"{env} is not set")
        raw = default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise EvidenceRootError(f"{env}={raw!r} is relative; it would resolve against the cwd")
    path = path.resolve()
    try:
        checkout: Path | None = repo_root()
    except RepoRootNotFoundError:
        checkout = None
    if checkout is not None and (path == checkout or checkout in path.parents):
        raise EvidenceRootError(f"{env}={path} is inside the repository {checkout}")
    return path


def evidence_root(*, required: bool = False) -> Path:
    """The evidence root; ``required`` (governed runs) refuses the default."""
    return _resolve_root(EVIDENCE_ROOT_ENV, DEFAULT_EVIDENCE_ROOT, required=required)


def data_root(*, required: bool = False) -> Path:
    """The research data root; ``required`` refuses the default."""
    return _resolve_root(DATA_ROOT_ENV, DEFAULT_DATA_ROOT, required=required)


def governed_refusals(*, git_commit: str | None, git_dirty: bool | None, profile_name: str | None) -> list[str]:
    """Every reason a governed run may not start (empty when it may)."""
    reasons: list[str] = []
    try:
        evidence_root(required=True)
    except EvidenceRootError as exc:
        reasons.append(f"evidence_root: {exc}")
    if git_commit is None:
        reasons.append("git_commit_unknown")
    if git_dirty is None:
        reasons.append("git_state_unknown")
    elif git_dirty:
        reasons.append("dirty_tree")
    if not profile_name:
        reasons.append("profile_unresolved")
    return reasons


def require_governed(*, git_commit: str | None, git_dirty: bool | None, profile_name: str | None) -> Path:
    """Raise ``GovernedRunRefused`` unless a governed run may start; return the evidence root."""
    reasons = governed_refusals(git_commit=git_commit, git_dirty=git_dirty, profile_name=profile_name)
    if reasons:
        raise GovernedRunRefused(reasons)
    return evidence_root(required=True)


def canonical_sha256(payload: Any) -> str:
    """sha256 of ``payload`` as sorted, compact JSON."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _now_iso() -> str:
    return datetime.fromtimestamp(timebase.now_s(), tz=UTC).isoformat()


def _append_whole(fd: int, line: bytes, path: Path) -> None:
    """Append ``line`` and fsync, or leave the file as it was and raise.

    The caller holds an exclusive lock. A short write (disk full, quota, file
    size limit) is truncated away, so a later append never continues a
    half-written event; a ledger whose tail is already incomplete is refused.
    """
    start = os.fstat(fd).st_size
    if start and os.pread(fd, 1, start - 1) != b"\n":
        raise ValueError(f"{path} ends in an incomplete line; repair the ledger before appending")
    written = 0
    try:
        while written < len(line):
            n = os.write(fd, line[written:])
            if n <= 0:
                raise OSError(f"short write to {path}: {written} of {len(line)} bytes")
            written += n
        os.fsync(fd)
    except BaseException:
        os.ftruncate(fd, start)
        os.fsync(fd)
        raise


def is_sealed(directory: str | Path) -> bool:
    """True when ``directory`` carries an evidence manifest and must not be rewritten."""
    return (Path(directory) / MANIFEST_NAME).is_file()


class EvidenceWriter:
    """Seals evidence directories and appends verdict events under one evidence root."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.ledger_path = self.root / LEDGER_RELPATH

    def relpath(self, path: str | Path) -> str:
        """``path`` relative to the root; raises ``ValueError`` for a path outside it."""
        return Path(path).resolve().relative_to(self.root.resolve()).as_posix()

    def seal(
        self, directory: str | Path, *, alpha_id: str, mode: RunMode, extra: Mapping[str, Any] | None = None
    ) -> Path:
        """Write ``manifest.json`` with the sha256 of every file under ``directory``.

        Raises ``EvidenceSealedError`` when a manifest is already there, and
        ``ValueError`` when ``directory`` is outside the root.
        """
        directory = Path(directory)
        self.relpath(directory)
        manifest_path = directory / MANIFEST_NAME
        if is_sealed(directory):
            raise EvidenceSealedError(f"{manifest_path} exists; sealed evidence is not rewritten")
        files = {
            p.relative_to(directory).as_posix(): file_sha256(p, cached=False)
            for p in sorted(directory.rglob("*"))
            if p.is_file() and p != manifest_path
        }
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "alpha_id": alpha_id,
            "mode": RunMode(mode).value,
            "sealed_at": _now_iso(),
            "files": files,
            **dict(extra or {}),
        }
        with manifest_path.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest, indent=2, sort_keys=True, default=str))
        return manifest_path

    @staticmethod
    def verify(directory: str | Path) -> list[str]:
        """Files whose bytes no longer match the manifest (missing ones included)."""
        directory = Path(directory)
        manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
        changed: list[str] = []
        for rel, digest in sorted(manifest.get("files", {}).items()):
            path = directory / rel
            if not path.is_file() or file_sha256(path, cached=False) != digest:
                changed.append(rel)
        present = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
        changed += sorted(present - set(manifest.get("files", {})) - {MANIFEST_NAME})
        return changed

    def append_verdict(self, event: Mapping[str, Any], *, mode: RunMode) -> dict[str, Any]:
        """Append one verdict event to the ledger and return it as written.

        Only governed runs reach the ledger; an exploratory event raises.
        """
        if RunMode(mode) is not RunMode.GOVERNED:
            raise ValueError("only governed runs append to the verdict ledger")
        missing = [k for k in _REQUIRED_EVENT_FIELDS if k not in event]
        if missing:
            raise ValueError(f"verdict event lacks {', '.join(missing)}")
        record = {
            "schema": VERDICT_EVENT_SCHEMA,
            "event_id": uuid4().hex,
            "recorded_at": _now_iso(),
            "mode": RunMode.GOVERNED.value,
            **dict(event),
        }
        line = (json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.ledger_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _append_whole(fd, line, self.ledger_path)
        finally:
            os.close(fd)
        return record

    def read_verdicts(self) -> Iterator[dict[str, Any]]:
        """The ledger's events in order; a malformed line raises ``ValueError``."""
        if not self.ledger_path.exists():
            return
        with self.ledger_path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{self.ledger_path}:{lineno}: not JSON ({exc})") from exc
                if not isinstance(record, dict) or record.get("schema") != VERDICT_EVENT_SCHEMA:
                    raise ValueError(f"{self.ledger_path}:{lineno}: not a {VERDICT_EVENT_SCHEMA} record")
                yield record
