"""Research evidence root: where governed runs publish (ADR-005 section 6).

Code lives in the public repository; evidence and data do not. Two roots,
both outside the checkout:

``$HFT_RESEARCH_EVIDENCE_ROOT`` (default ``~/hft_research_evidence``)
    ``runs/<run_id>/`` and ``validations/<alpha_id>/<stamp>/`` as the
    ExperimentTracker writes them, each sealed by a ``manifest.json`` that
    lists the sha256 of every file; ``ledger/verdicts.jsonl``, the
    append-only verdict events that alpha status is derived from;
    ``legacy/kills.jsonl``, kills recorded before the ledger existed, kept
    as an archive that never derives a status; ``ledger/stale.jsonl``, marks
    for runs that a registered defect affects (they flag evidence, never
    derive a status); ``ledger/holdout_access.jsonl``, every read of an
    out-of-sample holdout by any run, governed or exploratory (a second read
    is flagged, never gated); ``protocols/``, pre-registrations.
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
import subprocess
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
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
LEGACY_KILLS_RELPATH = "legacy/kills.jsonl"
LEGACY_KILL_SCHEMA = "legacy_kill.v1"
STALE_RELPATH = "ledger/stale.jsonl"
STALE_MARK_SCHEMA = "stale_mark.v1"
HOLDOUT_ACCESS_RELPATH = "ledger/holdout_access.jsonl"
HOLDOUT_ACCESS_SCHEMA = "holdout_access.v1"
ROOT_LAYOUT = ("runs", "validations", "ledger", "legacy", "protocols")

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


@contextmanager
def _locked(path: Path) -> Iterator[int]:
    """An append descriptor on ``path`` holding an exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _read_jsonl(path: Path, schema: str) -> Iterator[dict[str, Any]]:
    """The records of ``path`` in order; a line that is not a ``schema`` record raises ``ValueError``."""
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: not JSON ({exc})") from exc
            if not isinstance(record, dict) or record.get("schema") != schema:
                raise ValueError(f"{path}:{lineno}: not a {schema} record")
            yield record


def _compact(record: Mapping[str, Any]) -> bytes:
    return (json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def init_root(root: str | Path) -> list[str]:
    """Create the evidence layout under ``root`` and make it a local git repository.

    Idempotent: returns what was created (empty when the root was complete).
    No remote is configured; publishing the evidence repository is a separate,
    deliberate step.
    """
    root = Path(root)
    created: list[str] = []
    for name in ROOT_LAYOUT:
        path = root / name
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(f"{name}/")
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True, text=True)
        created.append(".git")
    return created


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
        with _locked(self.ledger_path) as fd:
            _append_whole(fd, _compact(record), self.ledger_path)
        return record

    def read_verdicts(self) -> Iterator[dict[str, Any]]:
        """The ledger's events in order; a malformed line raises ``ValueError``."""
        yield from _read_jsonl(self.ledger_path, VERDICT_EVENT_SCHEMA)

    def append_legacy_kills(self, kills: Iterable[Mapping[str, Any]]) -> int:
        """Archive pre-ledger kills under ``legacy/kills.jsonl``; return how many were new.

        Each kill needs ``alpha_id`` and ``kill_id``; one already archived
        (same ``kill_id``) is skipped, so a repeated import adds nothing. The
        archive never derives a status: those kills predate verdict rules v1.
        """
        kills = list(kills)
        for kill in kills:
            if not kill.get("alpha_id") or not kill.get("kill_id"):
                raise ValueError(f"legacy kill lacks alpha_id or kill_id: {dict(kill)!r}")
        return self._append_unique(
            self.root / LEGACY_KILLS_RELPATH, LEGACY_KILL_SCHEMA, "kill_id", "archived_at", kills
        )

    def read_legacy_kills(self) -> Iterator[dict[str, Any]]:
        """The archived pre-ledger kills in order; a malformed line raises ``ValueError``."""
        yield from _read_jsonl(self.root / LEGACY_KILLS_RELPATH, LEGACY_KILL_SCHEMA)

    def append_stale_marks(self, marks: Iterable[Mapping[str, Any]]) -> int:
        """Append stale marks to ``ledger/stale.jsonl``; return how many were new.

        Each mark needs ``mark_id``, ``run_key`` and ``defect_id``; one already
        there (same ``mark_id``) is skipped, so a repeated audit adds nothing.
        A mark flags evidence; the status rules never read it.
        """
        marks = list(marks)
        for mark in marks:
            if not mark.get("mark_id") or not mark.get("run_key") or not mark.get("defect_id"):
                raise ValueError(f"stale mark lacks mark_id, run_key or defect_id: {dict(mark)!r}")
        return self._append_unique(self.root / STALE_RELPATH, STALE_MARK_SCHEMA, "mark_id", "marked_at", marks)

    def read_stale_marks(self) -> Iterator[dict[str, Any]]:
        """The stale marks in order; a malformed line raises ``ValueError``."""
        yield from _read_jsonl(self.root / STALE_RELPATH, STALE_MARK_SCHEMA)

    def record_holdout_access(
        self, *, holdout_key: str, alpha_id: str, run_id: str | None, mode: RunMode
    ) -> dict[str, Any]:
        """Record one read of the holdout ``holdout_key`` and say how often it was read before.

        Every run that scores a holdout is recorded, exploratory ones too: a
        peek is a read. The result flags reuse (``reused``: this alpha read
        the same holdout before); it never changes a verdict.
        """
        if not holdout_key or not alpha_id:
            raise ValueError("holdout access needs holdout_key and alpha_id")
        path = self.root / HOLDOUT_ACCESS_RELPATH
        with _locked(path) as fd:
            prior = [r for r in _read_jsonl(path, HOLDOUT_ACCESS_SCHEMA) if r.get("holdout_key") == holdout_key]
            record = {
                "schema": HOLDOUT_ACCESS_SCHEMA,
                "access_id": uuid4().hex,
                "recorded_at": _now_iso(),
                "holdout_key": holdout_key,
                "alpha_id": alpha_id,
                "run_id": run_id,
                "mode": RunMode(mode).value,
            }
            _append_whole(fd, _compact(record), path)
        same_alpha = [r for r in prior if r.get("alpha_id") == alpha_id]
        return {
            "recorded": True,
            "holdout_key": holdout_key,
            "prior_reads": len(same_alpha),
            "prior_reads_any_alpha": len(prior),
            "prior_run_ids": [r.get("run_id") for r in same_alpha][-5:],
            "reused": bool(same_alpha),
        }

    def read_holdout_accesses(self) -> Iterator[dict[str, Any]]:
        """The recorded holdout reads in order; a malformed line raises ``ValueError``."""
        yield from _read_jsonl(self.root / HOLDOUT_ACCESS_RELPATH, HOLDOUT_ACCESS_SCHEMA)

    @staticmethod
    def _append_unique(path: Path, schema: str, key: str, stamp: str, records: list[Mapping[str, Any]]) -> int:
        """Append the ``records`` whose ``key`` is not yet in ``path``, under its lock, stamped at ``stamp``."""
        added = 0
        with _locked(path) as fd:
            seen = {r.get(key) for r in _read_jsonl(path, schema)}
            for record in records:
                if record[key] in seen:
                    continue
                seen.add(record[key])
                _append_whole(fd, _compact({"schema": schema, stamp: _now_iso(), **dict(record)}), path)
                added += 1
        return added


def record_exploratory_holdout_access(
    *, holdout_key: str, alpha_id: str, run_id: str | None, mode: RunMode = RunMode.EXPLORATORY
) -> dict[str, Any]:
    """Record an exploratory read of a holdout under the default evidence root, if that root exists.

    The rule every non-governed reader follows (Gate C in an exploratory
    validation, a generator's test split): a peek is a read, so it goes in the
    same ``ledger/holdout_access.jsonl`` a governed run writes, but an
    exploratory run never creates the root. Returns what
    :meth:`EvidenceWriter.record_holdout_access` returns, or ``recorded: False``
    with the reason.
    """
    try:
        root = evidence_root()
    except EvidenceRootError as exc:
        return {"recorded": False, "holdout_key": holdout_key, "reason": str(exc)}
    if not root.is_dir():
        return {"recorded": False, "holdout_key": holdout_key, "reason": f"no evidence root at {root}"}
    return EvidenceWriter(root).record_holdout_access(
        holdout_key=holdout_key, alpha_id=alpha_id, run_id=run_id, mode=mode
    )
