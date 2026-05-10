"""Replay-session eligibility check (loop_v1 L4).

Slice C's intent recorder is **opt-in, default-off** -- see
``HFT_INTENT_RECORDER_ENABLED`` checks in
``src/hft_platform/strategy/runner.py:351`` and
``src/hft_platform/recorder/worker.py:535``. As a result,
``hft.order_intents`` (created by migration
``20260504_001_create_order_intents.sql``) contains zero rows for any
session that ran before the recorder was first enabled.

This module distinguishes:

* ``Eligible``           -- live intents exist; replay parity is meaningful.
* ``IneligiblePreRecorder`` -- no rows for the session; replay would compare
                              against an empty live stream and report 100%
                              match trivially. The user must opt in via
                              ``--allow-pre-recorder`` to run anyway.
* ``IneligibleNoFixture`` -- fixture path missing; surfaced separately so
                            callers can give actionable error text.

The eligibility classification is **observation-only** -- it does not
write to ClickHouse and gracefully degrades to ``IneligiblePreRecorder``
when the database is unreachable, rather than masking a real divergence
as an environmental error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Union


@dataclass(frozen=True, slots=True)
class Eligible:
    n_live_intents: int


@dataclass(frozen=True, slots=True)
class IneligiblePreRecorder:
    reason: str


@dataclass(frozen=True, slots=True)
class IneligibleNoFixture:
    fixture_path: str


Eligibility = Union[Eligible, IneligiblePreRecorder, IneligibleNoFixture]


def _default_ck_client() -> Any:
    """Build a ``clickhouse_connect`` client via the canonical factory.

    F2: delegates to ``hft_platform.infra.ch_client.get_ch_client`` which
    carries the full username/password env precedence chain. The previous
    implementation mirrored ``alpha.audit._get_client`` and propagated its
    auth-bypass bug — see ``docs/runbooks/alpha-factory-dogfood-2026-05-06.md`` §F2.
    """
    from hft_platform.infra.ch_client import get_ch_client  # noqa: PLC0415  (heavy import; defer)

    return get_ch_client()


def _count_live_intents(client: Any, session_date: date, strategy_id: str) -> int:
    """SELECT count() ... WHERE toDate(ingest_ts/1e9) = %(d)s AND strategy_id = %(s)s.

    Uses ``ingest_ts`` (the recorder's wall-clock arrival timestamp) for
    the date filter because the table is partitioned by
    ``toYYYYMMDD(toDateTime64(ingest_ts/1e9, 3))`` per migration
    ``20260504_001_create_order_intents.sql:28``. Filtering on
    ``timestamp_ns`` would force a full-partition scan.
    """
    query = (
        "SELECT count() FROM hft.order_intents "
        "WHERE toDate(toDateTime64(ingest_ts/1e9, 3)) = %(d)s "
        "AND strategy_id = %(s)s"
    )
    params = {"d": session_date.isoformat(), "s": strategy_id}
    result = client.query(query, parameters=params)
    rows = getattr(result, "result_rows", None) or []
    if not rows:
        return 0
    return int(rows[0][0])


def check_eligibility(
    *,
    session_date: date,
    strategy_id: str,
    fixture_path: str | Path,
    ck_client: Any | None = None,
) -> Eligibility:
    """Classify a replay session as eligible / pre-recorder / no-fixture.

    Args:
        session_date: trading date for the live session.
        strategy_id: live strategy whose intents are being compared.
        fixture_path: WAL fixture archive (.tar.gz) path.
        ck_client: optional pre-built ClickHouse client. When ``None``
            a default client is built from ``HFT_CLICKHOUSE_*`` env vars.

    Returns:
        ``Eligible`` when ``hft.order_intents`` has >=1 matching row,
        ``IneligibleNoFixture`` when the fixture file is missing,
        ``IneligiblePreRecorder`` otherwise (including the database-
        unreachable branch -- fail-safe to "no live intents").
    """
    fp = Path(fixture_path)
    if not fp.exists():
        return IneligibleNoFixture(fixture_path=str(fp))

    # Codex adversarial-review (2026-05-07) finding [HIGH]: building the
    # default client outside the exception boundary lets connection / auth /
    # import / env-parse failures from get_ch_client() propagate out of
    # ``check_eligibility`` instead of degrading to ``IneligiblePreRecorder``.
    # Distinct ``intent_recorder_client_init_failed:`` reason prefix so
    # operators can tell client-construction failures apart from query
    # failures during incident triage.
    if ck_client is not None:
        client = ck_client
    else:
        try:
            client = _default_ck_client()
        except Exception as exc:  # noqa: BLE001
            return IneligiblePreRecorder(reason=f"intent_recorder_client_init_failed: {type(exc).__name__}: {exc}")

    try:
        n = _count_live_intents(client, session_date, strategy_id)
    except Exception as exc:  # noqa: BLE001
        return IneligiblePreRecorder(reason=f"intent_recorder_query_failed: {type(exc).__name__}: {exc}")

    if n == 0:
        return IneligiblePreRecorder(
            reason=(f"no_intents_recorded_for_{session_date.isoformat()}_strategy={strategy_id}")
        )
    return Eligible(n_live_intents=n)
