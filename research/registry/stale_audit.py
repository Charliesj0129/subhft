"""Stale-result detection over the defects registry (ADR-005 section 5).

``config/research/defects.yaml`` names each defect by the commit that fixed it
and the components it touched. A run is stale for a defect when it carries one
of those components and:

- ``open``: the defect has no fix yet;
- ``no_commit``: the run recorded no commit (every run before RunStamp v0);
- ``commit_unknown``: its commit is not in this checkout's history;
- ``before_fix``: its commit does not contain the fix commit;
- ``dataset_not_deduped``: the defect requires ``dataset_dedup`` and a dataset
  the run read cannot be shown to be deduplicated: its bytes no longer match
  the sha256 the RunStamp recorded, it has no readable sidecar, or a real one
  records no dedup rule (a run on a fixed commit can still read data exported
  before the fix).

No run record names its Gate C lane, so every run carries both lanes; a defect
of a component no run carries (mining) marks nothing here.

Audited runs: governed runs, one per (``run_id``, ``git_commit``) in
``ledger/verdicts.jsonl`` (a promotion can run on a later commit than the
validation it promotes, so each is judged by its own), and the legacy runs in
``research/experiments/runs`` except rows that name a pytest temporary
directory. An unreadable run meta stops the audit: it cannot be shown to be a
fixture. A run quarantined only as ``unknown_alpha`` (its
package is gone) is still audited: a missing mark would hide stale evidence,
an extra one only flags it. Marks are appended to ``ledger/stale.jsonl``;
evidence is never edited.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hft_platform.alpha.evidence import EvidenceWriter
from hft_platform.alpha.paths import repo_path, repo_root
from hft_platform.alpha.provenance import file_sha256
from hft_platform.alpha.status import check_event
from research.registry import legacy_evidence

DEFECTS_RELPATH = "config/research/defects.yaml"
DEFECTS_SCHEMA = "research_defects.v1"
REQUIREMENTS = frozenset({"dataset_dedup"})
# Components every research run carries: no run record names its lane, so both Gate C paths apply.
RUN_COMPONENTS = frozenset({"dataset", "verdict", "gate_c.taker", "gate_c.maker"})
# Legacy rows that are not evidence at all; every other quarantine reason is still audited.
_NOT_EVIDENCE = frozenset({"pytest_tmp_path"})
# The converted file the taker runner writes beside its input (hft_native_runner._hftbt_output_path).
_DERIVED_SUFFIX = "hftbt.npz"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_GIT_TIMEOUT_S = 10.0


@dataclass(frozen=True, slots=True)
class Defect:
    id: str
    components: frozenset[str]
    fix_commit: str | None
    requires: frozenset[str]


@dataclass(frozen=True, slots=True)
class Registry:
    sha256: str
    components: frozenset[str]
    defects: tuple[Defect, ...]


@dataclass(frozen=True, slots=True)
class AuditedRun:
    run_key: str
    origin: str
    alpha_id: str
    run_id: str | None
    git_commit: str | None
    datasets: tuple[tuple[str, str | None], ...] | None
    event_ids: tuple[str, ...] = ()
    components: frozenset[str] = RUN_COMPONENTS


def load_registry(path: Path | None = None) -> Registry:
    """The defects registry; anything malformed raises ``ValueError``."""
    path = path if path is not None else repo_path(DEFECTS_RELPATH)
    raw = path.read_bytes()
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: not YAML ({exc})") from exc
    if not isinstance(doc, dict) or doc.get("schema") != DEFECTS_SCHEMA:
        raise ValueError(f"{path}: not a {DEFECTS_SCHEMA} registry")
    components = doc.get("components")
    if not isinstance(components, dict) or not components:
        raise ValueError(f"{path}: no components")
    vocabulary = frozenset(str(c) for c in components)
    entries = doc.get("defects")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: no defects")
    defects: list[Defect] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ValueError(f"{path}: a defect has no id: {entry!r}")
        defect_id = entry["id"]
        if defect_id in seen:
            raise ValueError(f"{path}: duplicate defect id {defect_id}")
        seen.add(defect_id)
        if not isinstance(entry.get("summary"), str) or not entry["summary"].strip():
            raise ValueError(f"{path}: {defect_id} has no summary")
        tags = entry.get("components")
        if not isinstance(tags, list) or not tags or not set(tags) <= vocabulary:
            raise ValueError(f"{path}: {defect_id} components {tags!r} are not a subset of {sorted(vocabulary)}")
        if "fix_commit" not in entry:
            raise ValueError(f"{path}: {defect_id} has no fix_commit (null when open)")
        fix = entry["fix_commit"]
        if fix is not None and not (isinstance(fix, str) and _SHA_RE.fullmatch(fix)):
            raise ValueError(f"{path}: {defect_id} fix_commit {fix!r} is not a full 40-hex sha")
        requires = entry.get("requires", [])
        if not isinstance(requires, list) or not set(requires) <= REQUIREMENTS:
            raise ValueError(f"{path}: {defect_id} requires {requires!r}; known: {sorted(REQUIREMENTS)}")
        defects.append(Defect(defect_id, frozenset(tags), fix, frozenset(requires)))
    return Registry(hashlib.sha256(raw).hexdigest(), vocabulary, tuple(defects))


class GitHistory:
    """Commit ancestry in one checkout; a git failure raises ``RuntimeError``."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else repo_root()

    def _run(self, *args: str) -> int:
        try:
            proc = subprocess.run(
                ["git", *args], cwd=self.root, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"git {' '.join(args)} failed: {exc}") from exc
        return proc.returncode

    def has_commit(self, sha: str) -> bool:
        return self._run("cat-file", "-e", f"{sha}^{{commit}}") == 0

    def contains(self, commit: str, fix: str) -> bool:
        """True when ``fix`` is ``commit`` or one of its ancestors."""
        rc = self._run("merge-base", "--is-ancestor", fix, commit)
        if rc not in (0, 1):
            raise RuntimeError(f"git merge-base --is-ancestor {fix} {commit} exited {rc}")
        return rc == 0


def datasets_deduped(datasets: Iterable[tuple[str, str | None]] | None, *, base: Path | None = None) -> bool:
    """True when every dataset the run read is provably deduplicated.

    ``datasets`` are the RunStamp's (path, sha256) pairs. Each file must still
    have the recorded sha256 (an export rewritten in place no longer vouches for
    the run). Each input needs a readable ``<path>.meta.json`` sidecar, and a
    real one must record a dedup rule; a sidecar whose ``source_type`` is not
    ``real`` needs none. Only the converted ``hftbt.npz`` beside an input is
    exempt from the sidecar, and at least one input must be present.
    """
    if datasets is None:
        return False
    base = base if base is not None else repo_root()
    inputs = 0
    for raw, digest in datasets:
        data = Path(raw) if Path(raw).is_absolute() else base / raw
        try:
            if digest is None or file_sha256(data, cached=False) != digest:
                return False
        except OSError:
            return False
        if data.name.endswith(_DERIVED_SUFFIX):
            continue
        inputs += 1
        try:
            meta = json.loads(data.with_name(data.name + ".meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(meta, dict):
            return False
        if meta.get("source_type", "real") == "real" and meta.get("dedup_rule") in (None, "", "unrecorded"):
            return False
    return inputs > 0


def governed_run_key(event: Mapping[str, Any]) -> str:
    """The audited-run identity of a ledger event: its run (or the event itself) at its commit."""
    run_id = event.get("run_id")
    run = str(run_id) if run_id else f"event:{event.get('event_id', '')}"
    return f"governed:{run}@{event.get('git_commit') or 'none'}"


def governed_runs(root: Path) -> list[AuditedRun]:
    """One run per ``governed_run_key`` in the verdict ledger, in first-event order.

    An event without a ``run_id`` is a run of its own. Datasets come from the
    run's ``runs/<run_id>/meta.json`` RunStamp; None when it is not there. An
    unreadable governed meta raises ``ValueError``.
    """
    writer = EvidenceWriter(root)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for event in writer.read_verdicts():
        check_event(event)
        grouped.setdefault(governed_run_key(event), []).append(event)
    metas = legacy_evidence.read_runs(root / "runs", frozenset(), quarantine=False)
    _refuse_unreadable(metas, root / "runs")
    stamped = {r.run_id: r for r in metas}
    runs: list[AuditedRun] = []
    for run_key, events in grouped.items():
        run_id = events[0].get("run_id") or None
        commit = events[0].get("git_commit")
        meta = stamped.get(run_id) if run_id else None
        runs.append(
            AuditedRun(
                run_key=run_key,
                origin="governed",
                alpha_id=str(events[0]["alpha_id"]),
                run_id=run_id,
                git_commit=commit if isinstance(commit, str) and commit else None,
                datasets=meta.datasets if meta is not None else None,
                event_ids=tuple(str(e.get("event_id", "")) for e in events),
            )
        )
    return runs


def _refuse_unreadable(runs: Iterable[legacy_evidence.LegacyRun], where: Path) -> None:
    unreadable = sorted(r.run_id for r in runs if r.quarantine == "unreadable_meta")
    if unreadable:
        raise ValueError(f"unreadable run meta under {where}: {', '.join(unreadable)}; fix or move them, then rerun")


def legacy_runs(runs_root: Path | None = None, known: frozenset[str] | None = None) -> list[AuditedRun]:
    """The legacy runs to audit: all but pytest fixture rows; an unreadable meta raises ``ValueError``."""
    runs_root = runs_root if runs_root is not None else legacy_evidence.research_root() / "experiments" / "runs"
    known = known if known is not None else legacy_evidence.known_alpha_ids()
    rows = legacy_evidence.read_runs(runs_root, known)
    _refuse_unreadable(rows, runs_root)
    return [
        AuditedRun(
            run_key=f"legacy:{run.run_id}",
            origin="legacy",
            alpha_id=run.alpha_id,
            run_id=run.run_id,
            git_commit=run.git_commit,
            datasets=run.datasets,
        )
        for run in rows
        if run.quarantine not in _NOT_EVIDENCE and run.alpha_id and run.run_id
    ]


def stale_basis(run: AuditedRun, defect: Defect, git: GitHistory, *, base: Path | None = None) -> str | None:
    """Why ``run`` is stale for ``defect``, or None when it is not."""
    if not run.components & defect.components:
        return None
    if defect.fix_commit is None:
        return "open"
    if run.git_commit is None:
        return "no_commit"
    if not git.has_commit(run.git_commit):
        return "commit_unknown"
    if not git.contains(run.git_commit, defect.fix_commit):
        return "before_fix"
    if "dataset_dedup" in defect.requires and not datasets_deduped(run.datasets, base=base):
        return "dataset_not_deduped"
    return None


def mark_id(run_key: str, defect_id: str) -> str:
    return hashlib.sha256(f"{run_key}\0{defect_id}".encode()).hexdigest()[:32]


def audit(
    runs: Iterable[AuditedRun], registry: Registry, git: GitHistory, *, base: Path | None = None
) -> list[dict[str, Any]]:
    """One stale mark per (run, defect) the registry says is affected.

    Every fix commit must resolve in this checkout first (a shallow clone
    cannot audit); otherwise ``ValueError``.
    """
    missing = [d.id for d in registry.defects if d.fix_commit is not None and not git.has_commit(d.fix_commit)]
    if missing:
        raise ValueError(f"fix commits not in this checkout: {', '.join(missing)} (fetch full history)")
    marks: list[dict[str, Any]] = []
    for run in runs:
        for defect in registry.defects:
            basis = stale_basis(run, defect, git, base=base)
            if basis is None:
                continue
            marks.append(
                {
                    "mark_id": mark_id(run.run_key, defect.id),
                    "run_key": run.run_key,
                    "origin": run.origin,
                    "alpha_id": run.alpha_id,
                    "run_id": run.run_id,
                    "git_commit": run.git_commit,
                    "event_ids": list(run.event_ids),
                    "defect_id": defect.id,
                    "basis": basis,
                    "registry_sha256": registry.sha256,
                }
            )
    return marks
