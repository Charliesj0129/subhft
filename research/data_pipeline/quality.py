"""Source-layer data quality audit for the raw ClickHouse ``hft.market_data`` table.

Everything upstream of this module already has governance: ``research.data_pipeline``
validates exported artifact contracts, and ``research.combinatorial.smma_dataset``
writes a rich per-dataset sidecar. What was missing is a check on the *source table
itself* -- which is why the ``exch_ts`` +8h shift over partitions 20260126..20260205
survived roughly six months undetected.

This module is offline and strictly read-only against ClickHouse. It is advisory by
default: it produces a report and a verdict, and never blocks an export. The verdict
is stamped into dataset sidecars so every research artifact carries the data-quality
state it was built on (see :func:`stamp_payload`).

Design notes
------------
* The per-day statistics for almost every check come from a *single* table scan
  (:func:`fetch_day_stats`), because a full-corpus audit otherwise costs one pass per
  check.
* Query execution and evaluation are deliberately separated: every ``evaluate_*``
  function is pure over :class:`DayStats`, so the check logic is testable without a
  running ClickHouse.
* Trading-day eligibility is *never* re-implemented here. It is delegated to
  ``research.combinatorial.taifex_trading_dates`` when importable, and reported as
  ``unavailable`` when not. A previous SQL re-implementation of that rule produced 92
  and then 33 eligible days where the real answer was 60; approximating it is worse
  than omitting it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Literal

REPORT_SCHEMA = "hft_source_quality.v1"
DEFAULT_REPORT_DIR = Path("research/reports/data_quality")
SOURCE_TABLE = "hft.market_data"

Severity = Literal["error", "warn", "info"]
Status = Literal["pass", "fail", "unavailable"]
Verdict = Literal["CLEAN", "DEGRADED", "BROKEN"]

# TAIFEX session windows as minutes-from-Taipei-midnight. Day 08:45-13:45,
# night 15:00-05:00 (crossing midnight).
DAY_SESSION_OPEN_MINUTE = 8 * 60 + 45
DAY_SESSION_CLOSE_MINUTE = 13 * 60 + 45
NIGHT_SESSION_OPEN_MINUTE = 15 * 60
NIGHT_SESSION_CLOSE_MINUTE = 5 * 60

# Post-repair measurement put 99.1% of the previously-shifted days inside a real
# session window, so ~1% out-of-window is normal (pre-open auction prints, clock
# skew). A genuine whole-day timestamp shift lands far above this.
SESSION_WINDOW_OUTSIDE_MAX_RATIO = 0.03

# Causality tolerance: an exchange timestamp later than the ingest timestamp is
# physically impossible. 1s absorbs host/broker clock skew.
CAUSALITY_TOLERANCE_NS = 1_000_000_000

# Coverage classification thresholds, relative to a *local* baseline. The symbol
# universe and daily volume both change legitimately over a multi-month corpus
# (observed: ~100 -> 368 -> 357 -> 296), so a single global baseline would condemn
# whole eras as degraded. The baseline is the median of a centred window of
# populated days, which re-bases itself across a genuine universe step.
COVERAGE_BASELINE_WINDOW_DAYS = 11
COVERAGE_CLEAN_ROW_RATIO = 0.50
COVERAGE_CLEAN_SYMBOL_RATIO = 0.90
COVERAGE_DEGRADED_SYMBOL_RATIO = 0.50
COVERAGE_DEGRADED_ROW_RATIO = 0.10

# A day-over-day change in the count of distinct symbols is reported as a universe
# *step* only when it clears both an absolute and a relative floor. Subscribed
# symbols range from tens to >500 across this corpus, so an absolute floor alone
# fired on half of all sessions.
#
# Known limitation, stated rather than tuned away: monthly contract rollover churns
# the symbol set every day, at the same magnitude as a small pool-config change. A
# ~3% step such as the documented 368 -> 357 is therefore below the noise floor and
# is NOT detectable from counts; only large steps (357 -> 296 and bigger) surface
# here. Use the per-day symbol counts in the coverage profile for finer questions.
UNIVERSE_STEP_MIN_DELTA = 5
UNIVERSE_STEP_MIN_RATIO = 0.15

# A partition missing from the local archive is merely "behind" while the upstream copy
# still exists. Once the upstream TTL fires it is unrecoverable, so a missing partition
# inside this horizon escalates from warn to error.
ARCHIVE_SYNC_URGENT_HORIZON_DAYS = 30

QUERY_SETTINGS: dict[str, Any] = {"max_memory_usage": 2_500_000_000, "max_threads": 2}


@dataclass(frozen=True, slots=True)
class DayStats:
    """One row of the single-scan per-trading-day aggregate."""

    day: str
    rows: int
    symbols: int
    causality_violations: int
    max_skew_ns: int
    outside_session: int
    nonpositive_trade_price: int
    negative_bid: int
    crossed_book: int
    ragged_depth: int
    empty_book: int
    duplicate_rows: int
    first_exch_ts: int
    last_exch_ts: int


@dataclass(frozen=True, slots=True)
class MonthFieldStats:
    """Per-month field population, used for family-eligibility constraints."""

    month: int
    rows: int
    ticks: int
    bidasks: int
    snapshots: int
    directed_ticks: int


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    severity: Severity
    status: Status
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == "fail"


@dataclass(frozen=True, slots=True)
class QualityReport:
    schema: str
    generated_at: str
    source: str
    date_from: str
    date_to: str
    verdict: Verdict
    checks: tuple[CheckResult, ...]
    extent: dict[str, Any]
    report_sha256: str

    @property
    def findings(self) -> list[str]:
        return [f"{check.check_id}:{_slug(check.summary)}" for check in self.checks if check.failed]

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "generated_at": self.generated_at,
            "source": self.source,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "verdict": self.verdict,
            "extent": self.extent,
            "findings": self.findings,
            "checks": [asdict(check) for check in self.checks],
        }
        payload["report_sha256"] = self.report_sha256
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> QualityReport:
        checks = tuple(
            CheckResult(
                check_id=str(item["check_id"]),
                severity=item["severity"],
                status=item["status"],
                summary=str(item.get("summary", "")),
                detail=dict(item.get("detail") or {}),
            )
            for item in payload.get("checks") or ()
        )
        return cls(
            schema=str(payload.get("schema", REPORT_SCHEMA)),
            generated_at=str(payload.get("generated_at", "")),
            source=str(payload.get("source", SOURCE_TABLE)),
            date_from=str(payload["date_from"]),
            date_to=str(payload["date_to"]),
            verdict=payload.get("verdict", "DEGRADED"),
            checks=checks,
            extent=dict(payload.get("extent") or {}),
            report_sha256=str(payload.get("report_sha256", "")),
        )


def _slug(text: str) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in text.lower())
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")[:60]


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    """Hash a payload independently of key order.

    Mirrors ``smma_dataset._metadata_hash`` so a sidecar fingerprint and a report
    fingerprint mean the same thing.
    """
    canonical = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Pure evaluators -- no ClickHouse required.
# ---------------------------------------------------------------------------


def evaluate_causality(days: Sequence[DayStats]) -> CheckResult:
    """``exch_ts`` may never lead ``ingest_ts``. This is the invariant the +8h shift broke."""
    offenders = [d for d in days if d.causality_violations > 0]
    total = sum(d.causality_violations for d in offenders)
    worst_skew = max((d.max_skew_ns for d in days), default=0)
    detail: dict[str, Any] = {
        "violating_rows": total,
        "violating_days": [d.day for d in offenders],
        "max_skew_ns": worst_skew,
        "tolerance_ns": CAUSALITY_TOLERANCE_NS,
    }
    if not days:
        return CheckResult("ts_causality", "error", "unavailable", "no rows in range", detail)
    if offenders:
        summary = f"{total} rows with exch_ts ahead of ingest_ts across {len(offenders)} days"
        return CheckResult("ts_causality", "error", "fail", summary, detail)
    return CheckResult("ts_causality", "error", "pass", "no exch_ts/ingest_ts causality violations", detail)


def evaluate_session_window(days: Sequence[DayStats]) -> CheckResult:
    """Catch timestamp shifts the causality check cannot see (e.g. a negative offset)."""
    offenders: list[dict[str, Any]] = []
    for day in days:
        if day.rows <= 0:
            continue
        ratio = day.outside_session / day.rows
        if ratio > SESSION_WINDOW_OUTSIDE_MAX_RATIO:
            offenders.append({"day": day.day, "outside_ratio": round(ratio, 4), "outside_rows": day.outside_session})
    detail: dict[str, Any] = {
        "max_outside_ratio": SESSION_WINDOW_OUTSIDE_MAX_RATIO,
        "offending_days": offenders,
        "session_windows": ["08:45-13:45", "15:00-05:00"],
    }
    if not days:
        return CheckResult("ts_session_window", "warn", "unavailable", "no rows in range", detail)
    if offenders:
        summary = (
            f"{len(offenders)} days have over {SESSION_WINDOW_OUTSIDE_MAX_RATIO:.0%} of rows outside a session"
        )
        return CheckResult("ts_session_window", "warn", "fail", summary, detail)
    return CheckResult("ts_session_window", "warn", "pass", "all days within session-window tolerance", detail)


def evaluate_price_sanity(days: Sequence[DayStats]) -> CheckResult:
    bad_trades = sum(d.nonpositive_trade_price for d in days)
    bad_bids = sum(d.negative_bid for d in days)
    offenders = [d.day for d in days if d.nonpositive_trade_price > 0 or d.negative_bid > 0]
    detail: dict[str, Any] = {
        "nonpositive_trade_price_rows": bad_trades,
        "negative_bid_rows": bad_bids,
        "offending_days": offenders,
    }
    if not days:
        return CheckResult("price_sanity", "error", "unavailable", "no rows in range", detail)
    if bad_trades or bad_bids:
        summary = f"{bad_trades} non-positive trade prices, {bad_bids} negative bids"
        return CheckResult("price_sanity", "error", "fail", summary, detail)
    return CheckResult("price_sanity", "error", "pass", "all trade prices and bids positive", detail)


def evaluate_book_crossed(days: Sequence[DayStats]) -> CheckResult:
    total = sum(d.crossed_book for d in days)
    offenders = [{"day": d.day, "crossed_rows": d.crossed_book} for d in days if d.crossed_book > 0]
    detail: dict[str, Any] = {"crossed_rows": total, "offending_days": offenders}
    if not days:
        return CheckResult("book_crossed", "warn", "unavailable", "no rows in range", detail)
    if total:
        return CheckResult("book_crossed", "warn", "fail", f"{total} crossed best bid/ask snapshots", detail)
    return CheckResult("book_crossed", "warn", "pass", "no crossed books", detail)


def evaluate_depth_shape(days: Sequence[DayStats]) -> CheckResult:
    ragged = sum(d.ragged_depth for d in days)
    empty = sum(d.empty_book for d in days)
    detail: dict[str, Any] = {
        "ragged_depth_rows": ragged,
        "empty_bidask_rows": empty,
        "offending_days": [d.day for d in days if d.ragged_depth > 0 or d.empty_book > 0],
    }
    if not days:
        return CheckResult("depth_shape", "warn", "unavailable", "no rows in range", detail)
    if ragged or empty:
        summary = f"{ragged} rows with price/volume length mismatch, {empty} empty BidAsk rows"
        return CheckResult("depth_shape", "warn", "fail", summary, detail)
    return CheckResult("depth_shape", "warn", "pass", "depth arrays well-formed", detail)


def evaluate_duplicate_keys(days: Sequence[DayStats]) -> CheckResult:
    """``market_data`` is a plain MergeTree: a re-import duplicates rather than replaces."""
    total = sum(d.duplicate_rows for d in days)
    offenders = [{"day": d.day, "duplicate_rows": d.duplicate_rows} for d in days if d.duplicate_rows > 0]
    detail: dict[str, Any] = {"duplicate_rows": total, "offending_days": offenders}
    if not days:
        return CheckResult("duplicate_keys", "warn", "unavailable", "no rows in range", detail)
    if total:
        summary = f"{total} duplicate (symbol, exch_ts, ingest_ts, seq_no) rows"
        return CheckResult("duplicate_keys", "warn", "fail", summary, detail)
    return CheckResult("duplicate_keys", "warn", "pass", "no duplicate row keys", detail)


def classify_coverage(
    days: Sequence[DayStats],
    *,
    expected_days: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Label each day ``clean`` / ``partial`` / ``degraded`` / ``missing`` / ``non_session``.

    Days are **ingest calendar dates in Asia/Taipei**, not TAIFEX trading days. The
    trading day rolls at 15:00 and its night-session mapping is calendar-derived, so a
    Friday night session's post-midnight rows land on Saturday here. That is why an
    observed date outside the exchange calendar is labelled ``non_session`` and kept
    out of the quality tally rather than being scored as a bad day. Trading-day-grained
    questions belong to the eligibility layer, which delegates to the mining stack.

    Thresholds are relative to a local baseline rather than absolute, so the
    classification stays meaningful as venue volume and the symbol universe change.
    """
    populated = sorted((day for day in days if day.rows > 0), key=lambda stats: stats.day)
    observed = {day.day: day for day in populated}
    index_of = {day.day: position for position, day in enumerate(populated)}
    half = COVERAGE_BASELINE_WINDOW_DAYS // 2

    expected = set(expected_days) if expected_days is not None else None
    profile: list[dict[str, Any]] = []
    calendar = sorted(set(observed) | (expected or set()))
    for day_str in calendar:
        stats = observed.get(day_str)
        if stats is None:
            profile.append({"day": day_str, "status": "missing", "rows": 0, "symbols": 0})
            continue
        if expected is not None and day_str not in expected:
            profile.append(
                {"day": day_str, "status": "non_session", "rows": stats.rows, "symbols": stats.symbols}
            )
            continue
        position = index_of[day_str]
        window = populated[max(0, position - half) : position + half + 1]
        baseline_rows = median(item.rows for item in window)
        baseline_symbols = median(item.symbols for item in window)

        if stats.symbols < baseline_symbols * COVERAGE_DEGRADED_SYMBOL_RATIO or (
            stats.rows < baseline_rows * COVERAGE_DEGRADED_ROW_RATIO
        ):
            status = "degraded"
        elif (
            stats.rows >= baseline_rows * COVERAGE_CLEAN_ROW_RATIO
            and stats.symbols >= baseline_symbols * COVERAGE_CLEAN_SYMBOL_RATIO
        ):
            status = "clean"
        else:
            status = "partial"
        profile.append(
            {
                "day": day_str,
                "status": status,
                "rows": stats.rows,
                "symbols": stats.symbols,
                "baseline_rows": int(baseline_rows),
                "baseline_symbols": int(baseline_symbols),
            }
        )
    return profile


def evaluate_coverage(days: Sequence[DayStats], *, expected_days: Sequence[str] | None = None) -> CheckResult:
    profile = classify_coverage(days, expected_days=expected_days)
    counts = {"clean": 0, "partial": 0, "degraded": 0, "missing": 0, "non_session": 0}
    for entry in profile:
        counts[str(entry["status"])] += 1
    detail: dict[str, Any] = {
        "counts": counts,
        "expected_days_known": expected_days is not None,
        "day_grain": "ingest calendar date (Asia/Taipei), not TAIFEX trading day",
        "days": profile,
    }
    if not profile:
        return CheckResult("coverage_profile", "warn", "unavailable", "no days in range", detail)
    imperfect = counts["partial"] + counts["degraded"] + counts["missing"]
    if imperfect:
        summary = (
            f"{counts['clean']} clean, {counts['partial']} partial, "
            f"{counts['degraded']} degraded, {counts['missing']} missing"
        )
        return CheckResult("coverage_profile", "warn", "fail", summary, detail)
    return CheckResult("coverage_profile", "warn", "pass", f"all {counts['clean']} days clean", detail)


def evaluate_universe_drift(
    days: Sequence[DayStats],
    *,
    expected_days: Sequence[str] | None = None,
) -> CheckResult:
    """Detect pool-config changes in the subscribed symbol universe.

    Restricted to exchange sessions when a calendar is available: a non-session
    calendar date holds only the post-midnight tail of the previous night session, so
    including those dates turns every weekend into a spurious pair of steps.
    """
    sessions = set(expected_days) if expected_days is not None else None
    populated = [d for d in days if d.rows > 0 and (sessions is None or d.day in sessions)]
    steps: list[dict[str, Any]] = []
    for previous, current in zip(populated, populated[1:], strict=False):
        delta = current.symbols - previous.symbols
        threshold = max(UNIVERSE_STEP_MIN_DELTA, previous.symbols * UNIVERSE_STEP_MIN_RATIO)
        if abs(delta) >= threshold:
            steps.append(
                {
                    "day": current.day,
                    "previous_day": previous.day,
                    "symbols": current.symbols,
                    "previous_symbols": previous.symbols,
                    "delta": delta,
                }
            )
    detail: dict[str, Any] = {
        "min_delta": UNIVERSE_STEP_MIN_DELTA,
        "min_ratio": UNIVERSE_STEP_MIN_RATIO,
        "sessions_only": sessions is not None,
        "steps": steps,
    }
    if not populated:
        return CheckResult("universe_drift", "warn", "unavailable", "no populated days in range", detail)
    if steps:
        summary = f"{len(steps)} day-over-day symbol-universe steps"
        return CheckResult("universe_drift", "warn", "fail", summary, detail)
    return CheckResult("universe_drift", "warn", "pass", "symbol universe stable", detail)


def evaluate_field_coverage(months: Sequence[MonthFieldStats], *, trade_direction_present: bool) -> CheckResult:
    """Report ``trade_direction`` population per month.

    Informational by design: the tick family's aggressor split simply cannot use a
    month where the column was never written, and that is a data fact, not a defect.
    """
    detail: dict[str, Any] = {"trade_direction_column_present": trade_direction_present, "months": []}
    if not trade_direction_present:
        return CheckResult(
            "field_coverage",
            "info",
            "unavailable",
            "trade_direction column absent from this table",
            detail,
        )
    if not months:
        return CheckResult("field_coverage", "info", "unavailable", "no rows in range", detail)
    for entry in months:
        ratio = entry.directed_ticks / entry.ticks if entry.ticks else 0.0
        detail["months"].append(
            {
                "month": entry.month,
                "rows": entry.rows,
                "ticks": entry.ticks,
                "bidasks": entry.bidasks,
                "snapshots": entry.snapshots,
                "trade_direction_ratio": round(ratio, 4),
            }
        )
    unusable = [item["month"] for item in detail["months"] if float(item["trade_direction_ratio"]) < 0.99]
    detail["months_below_full_direction_coverage"] = unusable
    summary = f"{len(months)} months profiled, {len(unusable)} below full trade_direction coverage"
    return CheckResult("field_coverage", "info", "pass", summary, detail)


def evaluate_eligibility(date_from: str, date_to: str) -> CheckResult:
    """Delegate to the mining stack's authoritative rule, or report ``unavailable``.

    Deliberately never approximated here: an SQL re-implementation of the
    5-day-bar / 14-night-bar rule produced 92 and then 33 eligible days where the real
    answer was 60. Being silent is honest; being wrong is not.
    """
    detail: dict[str, Any] = {"date_from": date_from, "date_to": date_to}
    try:
        from research.combinatorial.taifex_trading_dates import (  # noqa: PLC0415
            full_session_eligibility,
        )
    except ImportError as exc:
        detail["reason"] = f"mining stack not importable: {exc}"
        detail["authority"] = "research.combinatorial.taifex_trading_dates.full_session_eligibility"
        return CheckResult(
            "eligibility",
            "info",
            "unavailable",
            "trading-day eligibility not computed (mining stack unavailable)",
            detail,
        )
    detail["authority"] = f"{full_session_eligibility.__module__}.{full_session_eligibility.__qualname__}"
    detail["reason"] = "importable, but eligibility needs a bar export; run the mining exporter to populate"
    return CheckResult(
        "eligibility",
        "info",
        "unavailable",
        "eligibility authority available; bar export required to evaluate",
        detail,
    )


def evaluate_archive_sync(
    local_partitions: Mapping[str, int],
    reference: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> CheckResult:
    """Compare the local archive against a reference inventory of the upstream table.

    This check exists because the local corpus is the only durable copy of research
    market data -- its TTL was removed on 2026-07-25 -- while the upstream production
    table still enforces a 6-month TTL. A partition that is missing locally and whose
    upstream TTL has fired is gone permanently, and nothing previously measured that.

    Severity is deliberately dynamic. Being merely behind is a ``warn``; being behind on
    a partition whose upstream copy expires within
    ``ARCHIVE_SYNC_URGENT_HORIZON_DAYS`` is an ``error``, because that is the window in
    which the loss becomes irreversible.

    Offline by construction: the reference inventory is produced separately by
    ``scripts/sync_market_data_archive.py --emit-inventory``. The auditor never opens a
    connection to the upstream host itself.
    """
    detail: dict[str, Any] = {"local_partitions": len(local_partitions)}
    if reference is None:
        detail["reason"] = "no reference inventory supplied"
        detail["producer"] = "scripts/sync_market_data_archive.py --emit-inventory <path>"
        return CheckResult(
            "archive_sync",
            "warn",
            "unavailable",
            "archive sync state unknown (no reference inventory)",
            detail,
        )

    moment = now or datetime.now(UTC)
    entries = list(reference.get("partitions") or ())
    detail["reference_generated_at"] = reference.get("generated_at")
    detail["reference_partitions"] = len(entries)

    missing: list[dict[str, Any]] = []
    deltas: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry["partition"])
        remote_rows = int(entry.get("rows") or 0)
        if remote_rows <= 0:
            continue
        local_rows = local_partitions.get(name)
        if local_rows is None:
            days_left = _days_until(entry.get("ttl_expiry"), moment)
            missing.append(
                {"partition": name, "rows": remote_rows, "days_until_upstream_expiry": days_left}
            )
        elif local_rows != remote_rows:
            deltas.append({"partition": name, "local_rows": local_rows, "remote_rows": remote_rows})

    urgent = [
        entry
        for entry in missing
        if entry["days_until_upstream_expiry"] is not None
        and entry["days_until_upstream_expiry"] <= ARCHIVE_SYNC_URGENT_HORIZON_DAYS
    ]
    horizons = [
        entry["days_until_upstream_expiry"] for entry in missing if entry["days_until_upstream_expiry"] is not None
    ]
    detail["missing_partitions"] = missing
    detail["row_delta_partitions"] = deltas
    detail["urgent_partitions"] = [entry["partition"] for entry in urgent]
    detail["days_until_permanent_loss"] = min(horizons) if horizons else None
    detail["urgent_horizon_days"] = ARCHIVE_SYNC_URGENT_HORIZON_DAYS

    if urgent:
        summary = f"{len(urgent)} unsynced partitions expire upstream within {ARCHIVE_SYNC_URGENT_HORIZON_DAYS} days"
        return CheckResult("archive_sync", "error", "fail", summary, detail)
    if missing or deltas:
        summary = f"{len(missing)} partitions missing locally, {len(deltas)} with row-count deltas"
        return CheckResult("archive_sync", "warn", "fail", summary, detail)
    return CheckResult("archive_sync", "warn", "pass", "archive matches the reference inventory", detail)


def _days_until(expiry: Any, moment: datetime) -> int | None:
    """Whole days from ``moment`` to an upstream TTL expiry, or ``None`` when unknown."""
    if not expiry or str(expiry).startswith("1970-01-01"):
        return None
    try:
        parsed = datetime.fromisoformat(str(expiry))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed - moment).days


def classify_verdict(checks: Sequence[CheckResult]) -> Verdict:
    if any(check.failed and check.severity == "error" for check in checks):
        return "BROKEN"
    if any(check.failed and check.severity == "warn" for check in checks):
        return "DEGRADED"
    return "CLEAN"


def build_report(
    *,
    date_from: str,
    date_to: str,
    days: Sequence[DayStats],
    months: Sequence[MonthFieldStats],
    trade_direction_present: bool,
    expected_days: Sequence[str] | None = None,
    generated_at: str | None = None,
    local_partitions: Mapping[str, int] | None = None,
    reference_inventory: Mapping[str, Any] | None = None,
) -> QualityReport:
    checks = (
        evaluate_causality(days),
        evaluate_session_window(days),
        evaluate_price_sanity(days),
        evaluate_book_crossed(days),
        evaluate_depth_shape(days),
        evaluate_duplicate_keys(days),
        evaluate_coverage(days, expected_days=expected_days),
        evaluate_universe_drift(days, expected_days=expected_days),
        evaluate_field_coverage(months, trade_direction_present=trade_direction_present),
        evaluate_archive_sync(local_partitions or {}, reference_inventory),
        evaluate_eligibility(date_from, date_to),
    )
    populated = [d for d in days if d.rows > 0]
    extent = {
        "rows": sum(d.rows for d in days),
        "days_observed": len(populated),
        "first_day": populated[0].day if populated else None,
        "last_day": populated[-1].day if populated else None,
        "max_symbols": max((d.symbols for d in populated), default=0),
        "min_symbols": min((d.symbols for d in populated), default=0),
    }
    body: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "source": SOURCE_TABLE,
        "date_from": date_from,
        "date_to": date_to,
        "verdict": classify_verdict(checks),
        "extent": extent,
        "checks": [asdict(check) for check in checks],
    }
    return QualityReport(
        schema=REPORT_SCHEMA,
        generated_at=str(body["generated_at"]),
        source=SOURCE_TABLE,
        date_from=date_from,
        date_to=date_to,
        verdict=body["verdict"],
        checks=checks,
        extent=extent,
        report_sha256=canonical_sha256(body),
    )


# ---------------------------------------------------------------------------
# ClickHouse access
# ---------------------------------------------------------------------------


_DAY_EXPR = "toDate(fromUnixTimestamp64Nano(ingest_ts), 'Asia/Taipei')"
_EXCH_MINUTE_EXPR = (
    "toHour(fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei')) * 60 "
    "+ toMinute(fromUnixTimestamp64Nano(exch_ts, 'Asia/Taipei'))"
)
_IN_SESSION_EXPR = (
    f"(exch_ts > 0 AND ("
    f"({_EXCH_MINUTE_EXPR} >= {DAY_SESSION_OPEN_MINUTE} AND {_EXCH_MINUTE_EXPR} <= {DAY_SESSION_CLOSE_MINUTE}) "
    f"OR {_EXCH_MINUTE_EXPR} >= {NIGHT_SESSION_OPEN_MINUTE} "
    f"OR {_EXCH_MINUTE_EXPR} < {NIGHT_SESSION_CLOSE_MINUTE}))"
)

DAY_STATS_QUERY = f"""
    SELECT
        {_DAY_EXPR} AS day,
        count() AS rows,
        uniqExact(symbol) AS symbols,
        countIf(exch_ts > ingest_ts + {CAUSALITY_TOLERANCE_NS}) AS causality_violations,
        max(exch_ts - ingest_ts) AS max_skew_ns,
        countIf(NOT {_IN_SESSION_EXPR}) AS outside_session,
        countIf(type = 'Tick' AND price_scaled <= 0) AS nonpositive_trade_price,
        countIf(length(bids_price) > 0 AND bids_price[1] < 0) AS negative_bid,
        countIf(
            length(bids_price) > 0 AND length(asks_price) > 0
            AND bids_price[1] > 0 AND asks_price[1] > 0
            AND asks_price[1] < bids_price[1]
        ) AS crossed_book,
        countIf(
            length(bids_price) != length(bids_vol) OR length(asks_price) != length(asks_vol)
        ) AS ragged_depth,
        countIf(type = 'BidAsk' AND length(bids_price) = 0 AND length(asks_price) = 0) AS empty_book,
        count() - uniqExact(cityHash64(symbol, exch_ts, ingest_ts, seq_no)) AS duplicate_rows,
        min(exch_ts) AS first_exch_ts,
        max(exch_ts) AS last_exch_ts
    FROM {SOURCE_TABLE}
    WHERE ingest_ts >= toUnixTimestamp64Nano(toDateTime64(%(date_from)s, 9, 'Asia/Taipei'))
      AND ingest_ts < toUnixTimestamp64Nano(toDateTime64(%(date_to_next)s, 9, 'Asia/Taipei'))
    GROUP BY day
    ORDER BY day
"""

MONTH_FIELD_QUERY = f"""
    SELECT
        toYYYYMM({_DAY_EXPR}) AS month,
        count() AS rows,
        countIf(type = 'Tick') AS ticks,
        countIf(type = 'BidAsk') AS bidasks,
        countIf(type = 'Snapshot') AS snapshots,
        countIf(type = 'Tick' AND trade_direction != 0) AS directed_ticks
    FROM {SOURCE_TABLE}
    WHERE ingest_ts >= toUnixTimestamp64Nano(toDateTime64(%(date_from)s, 9, 'Asia/Taipei'))
      AND ingest_ts < toUnixTimestamp64Nano(toDateTime64(%(date_to_next)s, 9, 'Asia/Taipei'))
    GROUP BY month
    ORDER BY month
"""


def _range_parameters(date_from: str, date_to: str) -> dict[str, str]:
    end = date.fromisoformat(date_to) + timedelta(days=1)
    return {"date_from": f"{date_from} 00:00:00", "date_to_next": f"{end.isoformat()} 00:00:00"}


def _iter_date_chunks(date_from: str, date_to: str, chunk_days: int) -> Iterator[tuple[str, str]]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    step = max(1, chunk_days)
    while start <= end:
        stop = min(start + timedelta(days=step - 1), end)
        yield start.isoformat(), stop.isoformat()
        start = stop + timedelta(days=1)


def _is_memory_limit_error(exc: Exception) -> bool:
    return "MEMORY_LIMIT_EXCEEDED" in str(exc)


def _query_chunked(
    client: Any,
    query: str,
    date_from: str,
    date_to: str,
    *,
    chunk_days: int,
) -> list[Sequence[Any]]:
    """Run a range-bounded aggregate in chunks, halving on ClickHouse memory pressure.

    The per-day aggregates carry ``uniqExact`` states, whose memory grows with the
    number of groups held concurrently. Halving down to a single day keeps the audit
    exact instead of degrading to an approximate cardinality estimator.
    """
    rows: list[Sequence[Any]] = []
    for chunk_from, chunk_to in _iter_date_chunks(date_from, date_to, chunk_days):
        try:
            result = client.query(
                query,
                parameters=_range_parameters(chunk_from, chunk_to),
                settings=QUERY_SETTINGS,
            )
        except Exception as exc:  # noqa: BLE001 - narrowed immediately; re-raised when unrelated
            if not _is_memory_limit_error(exc) or chunk_from == chunk_to:
                raise
            span = (date.fromisoformat(chunk_to) - date.fromisoformat(chunk_from)).days + 1
            rows.extend(_query_chunked(client, query, chunk_from, chunk_to, chunk_days=max(1, span // 2)))
            continue
        rows.extend(result.result_rows)
    return rows


def has_trade_direction_column(client: Any) -> bool:
    database, _, table = SOURCE_TABLE.partition(".")
    result = client.query(
        "SELECT count() FROM system.columns "
        "WHERE database = %(db)s AND table = %(tbl)s AND name = 'trade_direction'",
        parameters={"db": database, "tbl": table},
    )
    return bool(result.result_rows and int(result.result_rows[0][0]) > 0)


def fetch_day_stats(client: Any, date_from: str, date_to: str, *, chunk_days: int = 4) -> list[DayStats]:
    rows = _query_chunked(client, DAY_STATS_QUERY, date_from, date_to, chunk_days=chunk_days)
    return sorted(
        (
            DayStats(
                day=str(row[0]),
                rows=int(row[1]),
                symbols=int(row[2]),
                causality_violations=int(row[3]),
                max_skew_ns=int(row[4]),
                outside_session=int(row[5]),
                nonpositive_trade_price=int(row[6]),
                negative_bid=int(row[7]),
                crossed_book=int(row[8]),
                ragged_depth=int(row[9]),
                empty_book=int(row[10]),
                duplicate_rows=int(row[11]),
                first_exch_ts=int(row[12]),
                last_exch_ts=int(row[13]),
            )
            for row in rows
        ),
        key=lambda stats: stats.day,
    )


def fetch_month_field_stats(
    client: Any,
    date_from: str,
    date_to: str,
    *,
    chunk_days: int = 4,
) -> list[MonthFieldStats]:
    rows = _query_chunked(client, MONTH_FIELD_QUERY, date_from, date_to, chunk_days=chunk_days)
    merged: dict[int, list[int]] = {}
    for row in rows:
        bucket = merged.setdefault(int(row[0]), [0, 0, 0, 0, 0])
        for index in range(5):
            bucket[index] += int(row[index + 1])
    return [
        MonthFieldStats(
            month=month,
            rows=totals[0],
            ticks=totals[1],
            bidasks=totals[2],
            snapshots=totals[3],
            directed_ticks=totals[4],
        )
        for month, totals in sorted(merged.items())
    ]


CALENDAR_NAME = "XTAI"


def expected_trading_days(date_from: str, date_to: str) -> list[str] | None:
    """Exchange sessions in the range, or ``None`` when no calendar is installed.

    Uses ``exchange_calendars`` -- the same package and calendar the mining dataset
    sidecar records as ``calendar_name``/``calendar_package_version`` -- so the
    auditor's notion of an expected trading day matches the exporter's.

    Without a calendar the auditor cannot distinguish "market was shut" from "we
    failed to record", so it reports coverage over observed days only rather than
    inventing an expectation.
    """
    try:
        import exchange_calendars as xcals  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415
    except ImportError:
        return None
    try:
        calendar = xcals.get_calendar(CALENDAR_NAME)
        sessions = calendar.sessions_in_range(pd.Timestamp(date_from), pd.Timestamp(date_to))
    except Exception:  # noqa: BLE001 - a calendar lookup failure must not fail the audit
        return None
    return [str(value.date()) for value in sessions]


def fetch_local_partitions(client: Any) -> dict[str, int]:
    """Partition -> row count from ``system.parts``. Metadata only; no data is scanned."""
    database, table = SOURCE_TABLE.split(".", 1)
    query = (
        "SELECT partition, sum(rows) FROM system.parts "
        f"WHERE database = '{database}' AND table = '{table}' AND active "
        "GROUP BY partition ORDER BY partition"
    )
    result = client.query(query, settings=QUERY_SETTINGS)
    return {str(row[0]): int(row[1]) for row in result.result_rows}


def load_reference_inventory(path: Path | None) -> dict[str, Any] | None:
    """Load an upstream inventory emitted by ``scripts/sync_market_data_archive.py``."""
    if path is None or not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return dict(payload) if isinstance(payload, dict) else None


def run_audit(
    client: Any,
    *,
    date_from: str,
    date_to: str,
    use_calendar: bool = True,
    chunk_days: int = 4,
    reference_inventory: Path | None = None,
) -> QualityReport:
    days = fetch_day_stats(client, date_from, date_to, chunk_days=chunk_days)
    trade_direction_present = has_trade_direction_column(client)
    months = (
        fetch_month_field_stats(client, date_from, date_to, chunk_days=chunk_days)
        if trade_direction_present
        else []
    )
    expected = expected_trading_days(date_from, date_to) if use_calendar else None
    return build_report(
        date_from=date_from,
        date_to=date_to,
        days=days,
        months=months,
        trade_direction_present=trade_direction_present,
        expected_days=expected,
        local_partitions=fetch_local_partitions(client),
        reference_inventory=load_reference_inventory(reference_inventory),
    )


# ---------------------------------------------------------------------------
# Report persistence and provenance stamping
# ---------------------------------------------------------------------------


def render_markdown(report: QualityReport) -> str:
    lines = [
        f"# Source data quality — {report.source}",
        "",
        f"- Range: `{report.date_from}` → `{report.date_to}`",
        f"- Generated: `{report.generated_at}`",
        f"- Verdict: **{report.verdict}**",
        f"- Report SHA256: `{report.report_sha256}`",
        "",
        "## Checks",
        "",
        "| Check | Severity | Status | Summary |",
        "|---|---|---|---|",
    ]
    for check in report.checks:
        lines.append(f"| `{check.check_id}` | {check.severity} | **{check.status}** | {check.summary} |")
    lines += ["", "## Extent", "", "| Property | Value |", "|---|---|"]
    for key, value in sorted(report.extent.items()):
        lines.append(f"| {key} | {value} |")

    coverage = next((c for c in report.checks if c.check_id == "coverage_profile"), None)
    if coverage is not None and coverage.detail.get("days"):
        lines += ["", "## Daily coverage", "", "| Day | Status | Rows | Symbols |", "|---|---|---|---|"]
        for entry in coverage.detail["days"]:
            lines.append(
                f"| {entry['day']} | {entry['status']} | {int(entry['rows']):,} | {entry['symbols']} |"
            )
    return "\n".join(lines) + "\n"


def write_report(report: QualityReport, out_dir: Path = DEFAULT_REPORT_DIR) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    json_path = out_dir / f"{stamp}_source_audit.json"
    md_path = out_dir / f"{stamp}_source_audit.md"
    json_path.write_text(json.dumps(report.to_payload(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def iter_reports(reports_dir: Path = DEFAULT_REPORT_DIR) -> Iterator[Path]:
    if not reports_dir.exists():
        return
    yield from sorted(reports_dir.glob("*_source_audit.json"))


def load_latest_report(reports_dir: Path = DEFAULT_REPORT_DIR) -> QualityReport | None:
    paths = list(iter_reports(reports_dir))
    if not paths:
        return None
    payload = json.loads(paths[-1].read_text(encoding="utf-8"))
    return QualityReport.from_payload(payload)


def stamp_payload(
    report: QualityReport | None,
    *,
    requested_from: str,
    requested_to: str,
) -> dict[str, Any]:
    """Provenance keys to merge into a dataset sidecar.

    Advisory: the verdict is recorded, never enforced. The verdict field is always a
    concrete string -- ``unstamped`` and ``unstamped_range_mismatch`` are real values,
    so a missing audit is visible in the artifact rather than silently absent.
    """
    if report is None:
        return {"source_quality_schema": REPORT_SCHEMA, "source_quality_verdict": "unstamped"}
    covers = report.date_from <= requested_from and report.date_to >= requested_to
    stamp: dict[str, Any] = {
        "source_quality_schema": report.schema,
        "source_quality_report_sha256": report.report_sha256,
        "source_quality_generated_at": report.generated_at,
        "source_quality_range": [report.date_from, report.date_to],
    }
    if not covers:
        stamp["source_quality_verdict"] = "unstamped_range_mismatch"
        stamp["source_quality_requested_range"] = [requested_from, requested_to]
        return stamp
    stamp["source_quality_verdict"] = report.verdict
    stamp["source_quality_findings"] = report.findings
    return stamp
