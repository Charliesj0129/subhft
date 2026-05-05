"""Slice-D alpha-factory kill ledger.

Append-only record of every Gate-A..F / pre_screen / cluster / manual rejection.

Two parallel sinks:
  * ClickHouse table ``audit.alpha_kill_ledger`` (durable, queryable).
  * JSON-lines file ``research/alphas/_kill_ledger.jsonl`` (offline fallback;
    gitignored per plan §7 T4).

Idempotency contract (plan §5):
  ``kill_id = sha256(alpha_id || ":" || gate || ":" || stable_artifact_hash)``

Both sinks dedupe on ``(alpha_id, kill_id)``. CH path runs a
``SELECT count() WHERE alpha_id=? AND kill_id=?`` pre-check; jsonl path
combines an in-memory cache with a one-shot grep on append. Same
``KillRecord`` written twice yields exactly one row in each sink.

The ``stable_artifact_hash`` is computed via
``stable_artifact_hash(manifest)`` over a canonical JSON of the manifest
with ``kill_reason``/``cluster_id`` keys excluded — those keys are not on
the manifest after the §5 narrow, but the exclusion list is defensive
against future reintroduction.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from structlog import get_logger

from hft_platform.alpha import audit
from hft_platform.core import timebase
from research.registry.schemas import AlphaManifest

logger = get_logger("alpha_kill_ledger")

_VALID_GATES: frozenset[str] = frozenset(
    {"A", "B", "C", "D", "E", "F", "pre_screen", "cluster", "manual"}
)

# Excluded from stable_artifact_hash (plan §5 idempotency contract).
# Defensive even though the §5 narrow already removed kill_reason / cluster_id
# from the manifest — re-add here if any future schema mutation lands.
_HASH_EXCLUDED_KEYS: frozenset[str] = frozenset({"kill_reason", "cluster_id"})

_DEFAULT_JSONL_PATH = Path("research/alphas/_kill_ledger.jsonl")


def _jsonl_path() -> Path:
    """Resolve the jsonl path; ``HFT_ALPHA_KILL_LEDGER_PATH`` overrides for tests."""
    override = os.getenv("HFT_ALPHA_KILL_LEDGER_PATH")
    return Path(override) if override else _DEFAULT_JSONL_PATH


@dataclass(frozen=True, slots=True)
class KillRecord:
    """One row of the kill ledger.

    ``killed_at`` is nanoseconds since epoch; ``0`` means "fill in via
    ``timebase.now_ns()`` at append time" so callers don't need to plumb
    a clock through.
    """

    alpha_id: str
    gate: str
    reason: str
    stable_artifact_hash: str = ""
    scorecard_id: str = ""
    killed_by: str = "system"
    killed_at: int = 0  # ns; 0 means "fill in now_ns()"

    def __post_init__(self) -> None:
        if not self.alpha_id:
            raise ValueError("alpha_id must be non-empty")
        if self.gate not in _VALID_GATES:
            raise ValueError(
                f"gate must be one of {sorted(_VALID_GATES)}, got {self.gate!r}"
            )
        if not self.reason:
            raise ValueError("reason must be non-empty")

    def kill_id(self) -> str:
        """Deterministic dedupe key. Identical inputs → identical kill_id."""
        payload = f"{self.alpha_id}:{self.gate}:{self.stable_artifact_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_jsonl_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["kill_id"] = self.kill_id()
        return d


def stable_artifact_hash(manifest: AlphaManifest) -> str:
    """Canonical sha256 of the manifest with mutable run-outcome keys excluded.

    The exclusion list is defensive: ``kill_reason`` / ``cluster_id`` are not
    on ``AlphaManifest`` after the §5 narrow, but if a future schema change
    re-adds them, the hash MUST stay invariant under their mutation so
    duplicate kills don't silently bypass the dedupe pre-check.
    """
    payload = manifest.to_dict()
    cleaned = {k: v for k, v in payload.items() if k not in _HASH_EXCLUDED_KEYS}
    canonical = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class _JsonlCache:
    """Per-process cache of ``(alpha_id, kill_id)`` already in the jsonl file."""

    seen: set[tuple[str, str]] = field(default_factory=set)
    warmed_for: Path | None = None

    def warm(self, path: Path) -> None:
        if self.warmed_for == path and self.seen:
            return
        self.seen = set()
        self.warmed_for = path
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                        key = (str(row.get("alpha_id", "")), str(row.get("kill_id", "")))
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if key[0] and key[1]:
                        self.seen.add(key)
        except OSError:
            logger.warning("kill_ledger jsonl warm failed", path=str(path), exc_info=True)

    def contains(self, alpha_id: str, kill_id: str) -> bool:
        return (alpha_id, kill_id) in self.seen

    def remember(self, alpha_id: str, kill_id: str) -> None:
        self.seen.add((alpha_id, kill_id))


_CACHE = _JsonlCache()


def _reset_cache_for_tests() -> None:
    """Test-only hook: clear the in-memory cache."""
    global _CACHE  # noqa: PLW0603
    _CACHE = _JsonlCache()


def append_kill(record: KillRecord) -> bool:
    """Append one ``KillRecord``.

    Returns ``True`` if the row was newly inserted in *either* sink.
    Returns ``False`` only if a dedupe hit prevented insertion in the
    selected sink. CH and jsonl are kept independent: when CH is enabled
    and not duplicate, we insert there and skip the jsonl sink (the table
    is the durable record). When CH is disabled or fails, we fall back to
    jsonl with the same dedupe semantics.
    """
    finalized = record if record.killed_at != 0 else replace(record, killed_at=timebase.now_ns())
    kill_id = finalized.kill_id()

    if audit._is_enabled():  # noqa: SLF001 — audit is the canonical gate
        ch_result = _try_append_ch(finalized, kill_id)
        if ch_result == "inserted":
            return True
        if ch_result == "duplicate":
            return False
        # "failed" → fall through to jsonl

    return _try_append_jsonl(finalized, kill_id)


def _try_append_ch(record: KillRecord, kill_id: str) -> str:
    """Returns 'inserted', 'duplicate', or 'failed'."""
    try:
        client = audit._get_client()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        logger.warning("kill_ledger CH client init failed", exc_info=True)
        return "failed"
    try:
        existing = client.query(
            "SELECT count() FROM audit.alpha_kill_ledger WHERE alpha_id = %(a)s AND kill_id = %(k)s",
            parameters={"a": record.alpha_id, "k": kill_id},
        ).result_rows
        if existing and existing[0] and int(existing[0][0]) > 0:
            return "duplicate"
    except Exception:  # noqa: BLE001
        logger.warning("kill_ledger CH dedupe pre-check failed", alpha_id=record.alpha_id, exc_info=True)
        return "failed"

    try:
        client.insert(
            "audit.alpha_kill_ledger",
            [[
                kill_id,
                record.killed_at,
                record.alpha_id,
                record.gate,
                record.reason,
                record.stable_artifact_hash,
                record.scorecard_id,
                record.killed_by,
            ]],
            column_names=[
                "kill_id",
                "killed_at",
                "alpha_id",
                "gate",
                "reason",
                "stable_artifact_hash",
                "scorecard_id",
                "killed_by",
            ],
        )
    except Exception:  # noqa: BLE001
        logger.warning("kill_ledger CH insert failed", alpha_id=record.alpha_id, exc_info=True)
        return "failed"
    return "inserted"


def _try_append_jsonl(record: KillRecord, kill_id: str) -> bool:
    """Returns True iff a new line was appended."""
    path = _jsonl_path()
    _CACHE.warm(path)
    if _CACHE.contains(record.alpha_id, kill_id):
        return False

    row = record.to_jsonl_dict()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        logger.warning("kill_ledger jsonl append failed", path=str(path), exc_info=True)
        return False
    _CACHE.remember(record.alpha_id, kill_id)
    return True


def read_kills(alpha_id: str | None = None) -> list[KillRecord]:
    """Read all kills (or the subset for ``alpha_id``) from the jsonl sink.

    The CH sink is queried via ``audit.py``; this helper covers the offline
    case used by the CLI and migration script. Order is the file's natural
    append order.
    """
    path = _jsonl_path()
    if not path.exists():
        return []
    out: list[KillRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if alpha_id is not None and row.get("alpha_id") != alpha_id:
                continue
            try:
                out.append(KillRecord(
                    alpha_id=str(row["alpha_id"]),
                    gate=str(row["gate"]),
                    reason=str(row["reason"]),
                    stable_artifact_hash=str(row.get("stable_artifact_hash", "")),
                    scorecard_id=str(row.get("scorecard_id", "")),
                    killed_by=str(row.get("killed_by", "system")),
                    killed_at=int(row.get("killed_at", 0)),
                ))
            except (KeyError, ValueError, TypeError):
                continue
    return out


def latest_reason(alpha_id: str) -> str | None:
    """Return the most-recently-appended kill reason for ``alpha_id``, or None."""
    kills = read_kills(alpha_id=alpha_id)
    return kills[-1].reason if kills else None
