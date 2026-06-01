"""Unit tests for ReplayedIntentLog canonical hash + jsonl round-trip."""

from __future__ import annotations

import json
from pathlib import Path

from hft_platform.replay.intent_log import (
    ReplayedIntentLog,
    _DictIntent,
)


def _make_intent(**overrides):
    base = dict(
        intent_id=1,
        strategy_id="r47_maker",
        symbol="TMFD6",
        intent_type="NEW",
        side="BUY",
        tif="LIMIT",
        price=1234500,
        qty=1,
        target_order_id="",
        timestamp_ns=1_700_000_000_000_000_000,
        decision_price=1234500,
        price_type="LMT",
    )
    base.update(overrides)
    return _DictIntent(**base)


def test_canonical_form_excludes_volatile_fields() -> None:
    """Two intents differing only in volatile fields (trace_id etc.) must hash equal."""
    log_a = ReplayedIntentLog()
    log_b = ReplayedIntentLog()

    intent_a = _make_intent()
    intent_b = _make_intent()
    # Inject volatile attributes that the canonical schema must ignore.
    object.__setattr__(intent_a, "trace_id", "trace-aaaa")
    object.__setattr__(intent_b, "trace_id", "trace-bbbb")
    object.__setattr__(intent_a, "idempotency_key", "idem-1")
    object.__setattr__(intent_b, "idempotency_key", "idem-2")
    object.__setattr__(intent_a, "ttl_ns", 5_000_000)
    object.__setattr__(intent_b, "ttl_ns", 9_000_000)
    object.__setattr__(intent_a, "reason", "alpha")
    object.__setattr__(intent_b, "reason", "beta")
    object.__setattr__(intent_a, "ingest_ts", 111)
    object.__setattr__(intent_b, "ingest_ts", 222)
    object.__setattr__(intent_a, "source_ts_ns", 333)
    object.__setattr__(intent_b, "source_ts_ns", 444)

    log_a.append(intent_a)
    log_b.append(intent_b)

    assert log_a.hash() == log_b.hash()


def test_canonical_form_changes_on_price() -> None:
    """Intents differing only in price must produce different hashes."""
    log_a = ReplayedIntentLog()
    log_b = ReplayedIntentLog()

    log_a.append(_make_intent(price=1234500))
    log_b.append(_make_intent(price=1234600))

    assert log_a.hash() != log_b.hash()


def test_canonical_form_rounds_timestamp_to_us() -> None:
    """Sub-microsecond timestamp jitter (<=999 ns) must collapse to same hash."""
    base_ns = 1_700_000_000_000_000_000
    log_a = ReplayedIntentLog()
    log_b = ReplayedIntentLog()

    log_a.append(_make_intent(timestamp_ns=base_ns))
    log_b.append(_make_intent(timestamp_ns=base_ns + 500))

    assert log_a.hash() == log_b.hash()

    # Cross-microsecond jitter (+1000 ns) must NOT collapse.
    log_c = ReplayedIntentLog()
    log_c.append(_make_intent(timestamp_ns=base_ns + 1_000))
    assert log_a.hash() != log_c.hash()


def test_load_from_jsonl(tmp_path: Path) -> None:
    """Writing canonical records as JSONL and reading via from_jsonl
    preserves count and reproduces the same hash. Unknown forward-compat
    fields and blank lines must be tolerated."""
    log = ReplayedIntentLog()
    log.append(_make_intent(intent_id=1, price=1000000))
    log.append(_make_intent(intent_id=2, price=1000100, side="SELL"))
    log.append(_make_intent(intent_id=3, price=1000200, qty=2))

    expected_hash = log.hash()
    canonical = log.canonical_records()
    assert len(canonical) == 3

    fixture = tmp_path / "intents.jsonl"
    with fixture.open("w", encoding="utf-8") as f:
        for rec in canonical:
            f.write(json.dumps(rec) + "\n")
        # Stray blank line (loader must skip).
        f.write("\n")

    loaded = ReplayedIntentLog.from_jsonl(fixture)
    assert loaded.n_intents() == 3
    assert loaded.hash() == expected_hash

    # Forward-compat: extra unknown key on first record must be dropped.
    extra_record = dict(canonical[0])
    extra_record["future_field_for_slice_d"] = "ignored"
    extra_path = tmp_path / "intents_with_extra.jsonl"
    with extra_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(extra_record) + "\n")
        for rec in canonical[1:]:
            f.write(json.dumps(rec) + "\n")

    loaded_extra = ReplayedIntentLog.from_jsonl(extra_path)
    assert loaded_extra.n_intents() == 3
    assert loaded_extra.hash() == expected_hash


# --- Round 15: optional parity fields (goal §7) ----------------------


def test_optional_fields_omitted_by_default_keeps_legacy_hash() -> None:
    """Legacy intent without session/risk/force-flat attrs must hash
    identically before and after the Round 15 schema extension."""
    intent = _make_intent()
    rec = ReplayedIntentLog([intent]).canonical_records()[0]
    # None of the new keys should leak into the canonical record.
    for name in ("session_phase", "risk_filter_active", "force_flat_triggered"):
        assert name not in rec, f"{name} leaked into legacy canonical record"
    # And key count is exactly the original 12-field shape.
    assert len(rec) == 12


def test_session_phase_emitted_when_set() -> None:
    intent = _make_intent(session_phase="cash_open")
    rec = ReplayedIntentLog([intent]).canonical_records()[0]
    assert rec["session_phase"] == "cash_open"


def test_risk_filter_active_emitted_when_set() -> None:
    intent = _make_intent(risk_filter_active=True)
    rec = ReplayedIntentLog([intent]).canonical_records()[0]
    assert rec["risk_filter_active"] is True


def test_force_flat_triggered_emitted_when_set() -> None:
    intent = _make_intent(force_flat_triggered=False)
    rec = ReplayedIntentLog([intent]).canonical_records()[0]
    # False is a meaningful value (the strategy explicitly recorded
    # "force-flat did NOT fire"); must round-trip, not be skipped.
    assert rec["force_flat_triggered"] is False


def test_optional_field_drift_changes_hash() -> None:
    """Two otherwise-identical intents differing only in session_phase
    must hash differently — that's the whole point of adding the field."""
    a = ReplayedIntentLog([_make_intent(session_phase="cash_open")])
    b = ReplayedIntentLog([_make_intent(session_phase="cash_close")])
    assert a.hash() != b.hash()


def test_jsonl_round_trip_preserves_optional_fields(tmp_path: Path) -> None:
    log = ReplayedIntentLog(
        [
            _make_intent(
                session_phase="cash_open",
                risk_filter_active=True,
                force_flat_triggered=False,
            )
        ]
    )
    expected = log.hash()
    canonical = log.canonical_records()

    path = tmp_path / "intents.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in canonical:
            f.write(json.dumps(rec) + "\n")

    loaded = ReplayedIntentLog.from_jsonl(path)
    assert loaded.n_intents() == 1
    assert loaded.hash() == expected
    rec = loaded.canonical_records()[0]
    assert rec["session_phase"] == "cash_open"
    assert rec["risk_filter_active"] is True
    assert rec["force_flat_triggered"] is False


def test_legacy_jsonl_fixture_still_loads_unchanged(tmp_path: Path) -> None:
    """JSONL without the new keys (representative of historical fixtures)
    must load and hash exactly as before — the optional-field rollout is
    backward compatible by construction."""
    legacy = ReplayedIntentLog([_make_intent()])
    expected = legacy.hash()
    canonical = legacy.canonical_records()
    path = tmp_path / "legacy.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in canonical:
            f.write(json.dumps(rec) + "\n")

    loaded = ReplayedIntentLog.from_jsonl(path)
    assert loaded.hash() == expected
