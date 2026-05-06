"""Loop_v1 L4 — orchestrate ``hft run --mode replay`` against the Slice C harness.

Pipeline
========
1. Resolve ``session_date``, ``fixture_path``, ``strategy_id`` and ``loop_id``
   from CLI args + ``settings`` (post-loop-binding).
2. Classify the session via ``replay.eligibility.check_eligibility``:
   * ``IneligibleNoFixture``    -> write a ``no_fixture`` report, exit 1.
   * ``IneligiblePreRecorder``  -> without ``--allow-pre-recorder``: write a
                                   ``pre_recorder`` report (match_pct=null),
                                   exit 1.
   * ``Eligible`` (or pre-recorder + override) -> continue to step 3.
3. Run ``replay.strategy_replay.replay_strategy(...)`` with a strategy
   factory built from ``settings["strategy"]`` (legacy) or ``settings["loop_id"]``
   (loop-bound). If the factory cannot be built, write a
   ``strategy_unbuildable`` report and exit 1.
4. Load live intents from ``hft.order_intents`` (Eligible) or set them empty
   (pre-recorder + override). Project both streams to the canonical schema
   defined by ``replay.intent_log._intent_to_canonical``.
5. Compute ``IntentDiff(...).compute()`` -> ``ReplayParityReport``.
6. Write ``outputs/replay/<session>/{report.json, timeline.md,
   divergence_histogram.json}``. Exit 0 on parity completion.

Intentionally **does not** mutate ClickHouse, set environment variables,
or write to ``hft.replay_runs`` -- L4 is observation only. L11 will wrap
this with a daily Prometheus pipeline.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from hft_platform.alpha.replay_parity import IntentDiff, ReplayParityReport
from hft_platform.replay.eligibility import (
    Eligible,
    IneligibleNoFixture,
    IneligiblePreRecorder,
    check_eligibility,
)
from hft_platform.replay.intent_log import ReplayedIntentLog
from hft_platform.replay.strategy_replay import ReplayConfig, replay_strategy

HARNESS_VERSION = "slice-c.v1+l4.v1"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_to_canonical(row: tuple, columns: list[str]) -> dict[str, Any]:
    """Project a ClickHouse ``hft.order_intents`` row to the canonical
    schema produced by :func:`hft_platform.replay.intent_log._intent_to_canonical`.

    The canonical schema deliberately uses ``timestamp_us`` (microseconds)
    rather than nanoseconds so sub-microsecond scheduler jitter doesn't
    break parity.
    """
    d = dict(zip(columns, row, strict=False))
    return {
        "intent_id": int(d.get("intent_id", 0)),
        "strategy_id": str(d.get("strategy_id", "")),
        "symbol": str(d.get("symbol", "")),
        "intent_type": str(d.get("intent_type", "")),
        "side": str(d.get("side", "")),
        "tif": str(d.get("tif", "LIMIT")),
        "price": int(d.get("price_scaled", 0)),
        "qty": int(d.get("qty", 0)),
        "target_order_id": str(d.get("target_order_id", "") or ""),
        "timestamp_us": int(d.get("timestamp_ns", 0)) // 1000,
        "decision_price": int(d.get("decision_price", 0)),
        "price_type": str(d.get("price_type", "LMT")),
    }


def _load_live_intents(
    client: Any,
    session_date: date,
    strategy_id: str,
) -> list[dict[str, Any]]:
    """SELECT canonical-schema columns ORDER BY timestamp_ns, intent_id."""
    columns = [
        "intent_id",
        "strategy_id",
        "symbol",
        "intent_type",
        "side",
        "price_scaled",
        "qty",
        "tif",
        "target_order_id",
        "timestamp_ns",
        "decision_price",
        "price_type",
    ]
    # `columns` is a hardcoded module-level list of canonical intent fields;
    # user-controlled values (session_date, strategy_id) are bound via
    # clickhouse-connect parameters (%(d)s, %(s)s) — not interpolated.
    query = (
        f"SELECT {', '.join(columns)} FROM hft.order_intents "  # nosec B608
        "WHERE toDate(toDateTime64(ingest_ts/1e9, 3)) = %(d)s "
        "AND strategy_id = %(s)s "
        "ORDER BY timestamp_ns ASC, intent_id ASC"
    )
    params = {"d": session_date.isoformat(), "s": strategy_id}
    result = client.query(query, parameters=params)
    rows = getattr(result, "result_rows", None) or []
    return [_row_to_canonical(r, columns) for r in rows]


def _build_strategy_factory(settings: dict[str, Any]) -> Any | None:
    """Resolve ``settings["strategy"]`` to a callable ``factory(rng=...)``.

    Production strategies require a ``StrategyContext`` that the Slice C
    harness explicitly does not build (see ``strategy_replay.replay_strategy``
    docstring). Returning ``None`` here lets the caller emit a clear
    ``strategy_unbuildable`` report rather than crashing mid-replay.
    """
    strat_cfg = settings.get("strategy") or {}
    module_name = strat_cfg.get("module")
    class_name = strat_cfg.get("class")
    strategy_id = strat_cfg.get("id", "")
    if not module_name or not class_name:
        return None
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
    except Exception:  # noqa: BLE001
        return None

    def factory(rng: Any) -> Any:  # rng kw is the Slice C contract
        try:
            return cls(strategy_id=strategy_id, rng=rng)
        except TypeError:
            # Strategies that don't accept rng kw still satisfy the harness
            # contract as long as their handle_event(ctx, event) is sync.
            return cls(strategy_id=strategy_id)

    return factory


def _write_report(
    out_dir: Path,
    payload: dict[str, Any],
    parity: ReplayParityReport | None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
    if parity is not None:
        (out_dir / "divergence_histogram.json").write_text(
            json.dumps(parity.divergence_histogram, indent=2, sort_keys=True)
        )
        timeline = (
            f"# Replay timeline -- {payload['session']}\n\n"
            f"- Strategy: `{payload['strategy_id']}`\n"
            f"- Loop: `{payload['loop_id']}`\n"
            f"- Eligibility: `{payload['eligibility_status']}`\n"
            f"- Live intents: {payload['n_live_intents']}\n"
            f"- Replayed intents: {payload['n_replayed_intents']}\n"
            f"- Match pct: {payload['match_pct']}\n"
            f"- First divergence index: {payload['first_divergence_idx']}\n"
            f"- Harness: `{payload['harness_version']}`\n"
            f"- Fixture SHA-256: `{payload['fixture_sha256']}`\n"
        )
        (out_dir / "timeline.md").write_text(timeline)


def run_replay_session(
    settings: dict[str, Any],
    *,
    session_date: date,
    fixture_path: str | Path,
    allow_pre_recorder: bool = False,
    out_root: str | Path = "outputs/replay",
    ck_client: Any | None = None,
    strategy_factory_override: Any | None = None,
) -> int:
    """Drive an ad-hoc replay session and write the parity report.

    Returns a process exit code:
      * 0 -- parity check completed (eligible OR pre-recorder + override).
      * 1 -- fixture missing OR strategy unbuildable OR pre-recorder
             without ``--allow-pre-recorder``.
    """
    fp = Path(fixture_path)
    strategy_cfg = settings.get("strategy") or {}
    strategy_id = strategy_cfg.get("id") or ""
    loop_id = settings.get("loop_id") or ""
    out_dir = Path(out_root) / session_date.isoformat()

    base_payload: dict[str, Any] = {
        "session": session_date.isoformat(),
        "strategy_id": strategy_id,
        "loop_id": loop_id,
        "eligibility_status": "unknown",
        "n_market_events": 0,
        "n_live_intents": 0,
        "n_replayed_intents": 0,
        "match_pct": None,
        "first_divergence_idx": None,
        "harness_version": HARNESS_VERSION,
        "fixture_sha256": "",
    }

    eligibility = check_eligibility(
        session_date=session_date,
        strategy_id=strategy_id,
        fixture_path=fp,
        ck_client=ck_client,
    )

    if isinstance(eligibility, IneligibleNoFixture):
        base_payload["eligibility_status"] = "no_fixture"
        base_payload["error"] = f"fixture_not_found: {eligibility.fixture_path}"
        _write_report(out_dir, base_payload, parity=None)
        return 1

    base_payload["fixture_sha256"] = _file_sha256(fp)

    if isinstance(eligibility, IneligiblePreRecorder) and not allow_pre_recorder:
        base_payload["eligibility_status"] = "pre_recorder"
        base_payload["error"] = eligibility.reason
        _write_report(out_dir, base_payload, parity=None)
        return 1

    factory = strategy_factory_override or _build_strategy_factory(settings)
    if factory is None:
        base_payload["eligibility_status"] = (
            "pre_recorder" if isinstance(eligibility, IneligiblePreRecorder) else "eligible"
        )
        base_payload["error"] = (
            f"strategy_unbuildable: id={strategy_id!r} "
            f"module={strategy_cfg.get('module')!r} "
            f"class={strategy_cfg.get('class')!r}"
        )
        _write_report(out_dir, base_payload, parity=None)
        return 1

    rng_seed = int(os.getenv("HFT_REPLAY_RNG_SEED", "0"))
    cfg = ReplayConfig(
        fixture_path=str(fp),
        strategy_factory=factory,
        symbols=None,
        rng_seed=rng_seed,
    )
    log: ReplayedIntentLog = replay_strategy(cfg)
    replayed_records = log.canonical_records()

    if isinstance(eligibility, Eligible):
        live_records = _load_live_intents(
            ck_client if ck_client is not None else _client_or_none(),
            session_date,
            strategy_id,
        )
        eligibility_status = "eligible"
    else:
        # pre-recorder + --allow-pre-recorder: live stream is empty by definition.
        live_records = []
        eligibility_status = "pre_recorder"

    diff = IntentDiff(
        live=live_records,
        replayed=replayed_records,
        evidence_path=str(out_dir / "report.json"),
    )
    report: ReplayParityReport = diff.compute()

    base_payload["eligibility_status"] = eligibility_status
    base_payload["n_market_events"] = log.n_events_processed
    base_payload["n_live_intents"] = len(live_records)
    base_payload["n_replayed_intents"] = len(replayed_records)
    base_payload["match_pct"] = report.match_pct
    base_payload["first_divergence_idx"] = report.first_divergence_idx
    base_payload["divergence_histogram"] = dict(report.divergence_histogram)

    _write_report(out_dir, base_payload, parity=report)
    return 0


def _client_or_none() -> Any | None:
    """Best-effort ClickHouse client builder for the live-intents loader.

    The eligibility check may have used an injected client (tests) or a
    default one (production). When eligibility passed without an injected
    client we still need one for ``_load_live_intents``; this helper lets
    that path degrade to ``None`` instead of crashing if the database
    became unreachable between the eligibility query and the intents
    fetch.
    """
    from hft_platform.replay.eligibility import _default_ck_client

    try:
        return _default_ck_client()
    except Exception:  # noqa: BLE001
        return None
