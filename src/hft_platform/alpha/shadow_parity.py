"""Replay parity evidence for the Shadow-exit obligation (ADR-003).

Gate C defers ``replay_parity`` to ``shadow_exit`` when a candidate has no live
intents yet; promotion records it as a ``replay_parity_audit`` obligation. The
obligation is discharged only by ``report.json`` files that
``hft run --mode replay`` (``replay/cli_runner.py``) wrote over sessions with
recorded live intents. A typed-in percentage is not evidence.

A report is bound to the code it replayed: promotion pins the runtime strategy
(``module:Class`` and the sha256 of its module source) into the obligation,
the replay runner writes the same fingerprint into each report, and the two
must agree. A report from an earlier version of a strategy that kept its id
does not certify the current one.

Pure functions: they read the named files and judge them; nothing is written.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from hft_platform.alpha.outcomes import Outcome
from hft_platform.alpha.provenance import file_sha256

PARITY_GATE = "replay_parity_audit"
SHADOW_EXIT = "shadow_exit"
# ``replay/cli_runner.HARNESS_VERSION``; a report from any other harness is
# not Shadow-exit evidence (a test keeps the two equal).
REPLAY_HARNESS = "slice-c.v1+l4.v1"
_CERTIFIED_REPORT_KEYS = ("path", "sha256", "session", "match_pct", "n_live_intents", "config_sha256")
_PINNED_KEYS = ("module", "class", "code_sha256")


def strategy_fingerprint(module: Any, class_name: Any) -> dict[str, str] | None:
    """The runtime strategy as code: ``module``, ``class``, sha256 of the module source.

    None when the module cannot be located without importing it; the caller
    then has nothing to bind a report to.
    """
    if not isinstance(module, str) or not module or not isinstance(class_name, str) or not class_name:
        return None
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return None
    origin = Path(spec.origin) if spec is not None and spec.origin else None
    if origin is None or not origin.is_file():
        return None
    # Uncached: an edit that keeps the file size within one mtime tick keeps the cache key.
    return {"module": module, "class": class_name, "code_sha256": file_sha256(origin, cached=False)}


def pin_strategy(spec: str | None) -> dict[str, str] | None:
    """Fingerprint of a ``module:Class`` spec; None when absent, malformed or unresolvable."""
    module, sep, class_name = (spec or "").partition(":")
    return strategy_fingerprint(module, class_name) if sep else None


def strategy_config_sha256(strategy_cfg: Any) -> str:
    """sha256 of the strategy settings block (id, module, class, params) as canonical JSON."""
    blob = json.dumps(strategy_cfg if isinstance(strategy_cfg, dict) else {}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _valid_minimum(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value) and 0 < value <= 100


def is_parity_obligation(entry: Any) -> bool:
    return isinstance(entry, dict) and entry.get("gate") == PARITY_GATE and entry.get("stage") == SHADOW_EXIT


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def judge_report(
    path_str: str,
    root: Path,
    strategy_id: str,
    min_match_pct: float,
    strategy_code: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One replay report against the pinned ``strategy_code``; ``problem`` is empty when it passes."""
    path = Path(path_str)
    if not path.is_absolute():
        path = root / path
    row: dict[str, Any] = {"path": str(path)}
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, ValueError) as exc:
        row["problem"] = f"unreadable: {type(exc).__name__}"
        return row
    row["sha256"] = hashlib.sha256(raw).hexdigest()  # the bytes judged, not a cached hash
    if not isinstance(payload, dict):
        row["problem"] = "not a JSON object"
        return row
    match_pct = _as_float(payload.get("match_pct"))
    n_live = payload.get("n_live_intents")
    fingerprint = payload.get("strategy_fingerprint")
    fingerprint = fingerprint if isinstance(fingerprint, dict) else {}
    row.update(
        {
            "session": str(payload.get("session") or ""),
            "match_pct": match_pct,
            "n_live_intents": n_live,
            "n_replayed_intents": payload.get("n_replayed_intents"),
            "code_sha256": fingerprint.get("code_sha256"),
            "config_sha256": fingerprint.get("config_sha256"),
        }
    )
    # Each check fails closed: a report that cannot show it compared this
    # strategy's live intents is not evidence that the replay reproduces them.
    if payload.get("error"):
        row["problem"] = f"replay error: {payload.get('error')}"
    elif payload.get("harness_version") != REPLAY_HARNESS:
        row["problem"] = f"harness {payload.get('harness_version')!r} is not {REPLAY_HARNESS!r}"
    elif payload.get("eligibility_status") != "eligible":
        row["problem"] = f"eligibility {payload.get('eligibility_status')!r}, not 'eligible'"
    elif payload.get("strategy_id") != strategy_id:
        row["problem"] = f"strategy_id {payload.get('strategy_id')!r} is not {strategy_id!r}"
    elif not row["session"]:
        row["problem"] = "no session"
    elif not fingerprint.get("code_sha256") or not fingerprint.get("config_sha256"):
        row["problem"] = "report does not fingerprint the strategy it replayed"
    elif strategy_code is None or any(fingerprint.get(k) != strategy_code.get(k) for k in _PINNED_KEYS):
        row["problem"] = (
            f"replayed {fingerprint.get('module')}:{fingerprint.get('class')} "
            f"code {str(fingerprint.get('code_sha256'))[:12]}, not the promoted strategy code"
        )
    elif isinstance(n_live, bool) or not isinstance(n_live, int) or n_live <= 0:
        # IntentDiff reports 100% when both streams are empty.
        row["problem"] = "no live intents compared"
    elif match_pct is None:
        row["problem"] = "no numeric match_pct"
    elif match_pct < min_match_pct:
        row["problem"] = f"match_pct {match_pct} below {min_match_pct}"
    else:
        row["problem"] = ""
    return row


def judge_reports(
    paths: Iterable[str],
    root: Path,
    *,
    strategy_id: str,
    min_match_pct: float,
    strategy_code: dict[str, Any] | None,
) -> dict[str, Any]:
    """Every report must pass, at least one is required, one report per session, one strategy config.

    ``strategy_code`` is the obligation's pinned fingerprint. Both it and the
    threshold are checked here, so no caller can certify without them: a
    threshold that is not a finite percentage in (0, 100] fails, since
    ``match_pct < nan`` is never true.
    """
    paths = list(paths)
    problems: list[str] = []
    if not _valid_minimum(min_match_pct):
        problems.append(f"min_match_pct {min_match_pct!r} is not a finite percentage in (0, 100]")
    if not isinstance(strategy_code, dict) or not all(strategy_code.get(k) for k in _PINNED_KEYS):
        problems.append("obligation does not pin the strategy code (promote with --replay-strategy module:Class)")
        strategy_code = None
    reports = [judge_report(p, root, strategy_id, min_match_pct, strategy_code) for p in paths]
    problems += [f"{r['path']}: {r['problem']}" for r in reports if r.get("problem")]
    if not reports:
        problems.append("no replay parity report (hft run --mode replay)")
    sessions = [r["session"] for r in reports if r.get("session")]
    if len(sessions) != len(set(sessions)):
        problems.append("two reports for one session")
    if len({r.get("config_sha256") for r in reports}) > 1:
        problems.append("reports replayed different strategy settings")
    passed = not problems
    return {
        "stage": SHADOW_EXIT,
        "strategy_id": strategy_id,
        "min": min_match_pct,
        "reports": reports,
        "outcome": (Outcome.PASS if passed else Outcome.FAIL).value,
        "pass": passed,
        "detail": f"{len(reports)} report(s) >= {min_match_pct}%" if passed else "; ".join(problems),
    }


def obligation_min_match_pct(obligation: dict[str, Any], default: float) -> float:
    minimum = _as_float(obligation.get("min_match_pct"))
    return default if minimum is None else minimum


def certified(obligation: dict[str, Any], judged: dict[str, Any], *, certified_by: str) -> dict[str, Any]:
    """The obligation as discharged: which step certified it, on which files."""
    return {
        **obligation,
        "certified_by": certified_by,
        "reports": [{k: r.get(k) for k in _CERTIFIED_REPORT_KEYS} for r in judged.get("reports", [])],
    }
