"""Negative regression tests for the shared replay-parity diff engine.

These prove the fail-open blind spots the prior implementation had:
empty/empty -> trivial 100%, generated-id in the hash, no ordering /
schema / missing / unexpected taxonomy, and an un-localizable divergence.
Every test here asserts a *fail-closed* outcome with a populated
``first_divergence`` — happy-path-only coverage is explicitly insufficient.
"""

from __future__ import annotations

from hft_platform.replay.intent_diff import (
    HASH_VERSION,
    INTENT_SCHEMA_VERSION,
    MismatchType,
    canonicalize_intent,
    diff_intent_streams,
    stable_intent_hash,
)


def _intent(**overrides):
    base = dict(
        intent_id=1,
        strategy_id="r47_maker",
        symbol="TMFD6",
        intent_type="NEW",
        side="BUY",
        tif="LIMIT",
        price=1_234_500,
        qty=1,
        target_order_id="",
        timestamp_ns=1_700_000_000_000_000_000,
        source_ts_ns=1_700_000_000_000_000_000,
        decision_price=1_234_500,
        price_type="LMT",
        reason="alpha",
    )
    base.update(overrides)
    return base


def _canon(**overrides):
    return canonicalize_intent(_intent(**overrides))


# --------------------------------------------------------------------------
# Hash stability / sensitivity
# --------------------------------------------------------------------------


def test_hash_invariant_to_generated_id_localts_uuid_reason() -> None:
    """Only changing intent_id / local emission ts / uuid / reason must NOT
    change the stable hash (these are non-deterministic across live vs replay)."""
    a = _canon(intent_id=1, timestamp_ns=111, reason="alpha")
    b = _canon(intent_id=999, timestamp_ns=222, reason="beta")
    # Inject a uuid-like field that must be ignored by the hash.
    a["intent_uuid"] = "aaaa"
    b["intent_uuid"] = "bbbb"
    assert stable_intent_hash(a) == stable_intent_hash(b)


def test_hash_changes_on_meaningful_fields() -> None:
    base = _canon()
    for fld, newval in (
        ("side", "SELL"),
        ("qty", 2),
        ("price", 1_234_600),
        ("symbol", "TXFD6"),
        ("intent_type", "CANCEL"),
    ):
        assert stable_intent_hash(_canon(**{fld: newval})) != stable_intent_hash(base), fld


def test_hash_embeds_version_and_is_not_builtin_hash() -> None:
    canonical = _canon()
    # Deterministic 64-char hex SHA-256 (built-in hash() would be a small int).
    digest = stable_intent_hash(canonical)
    assert isinstance(digest, str) and len(digest) == 64
    assert canonical["intent_schema_version"] == INTENT_SCHEMA_VERSION


def test_canonicalize_is_idempotent() -> None:
    once = _canon()
    twice = canonicalize_intent(once)
    assert stable_intent_hash(once) == stable_intent_hash(twice)


# --------------------------------------------------------------------------
# Diff engine fail-closed cases
# --------------------------------------------------------------------------


def test_identical_streams_ok() -> None:
    stream = [_canon(intent_id=i, source_ts_ns=1_700_000_000_000_000_000 + i * 1000) for i in range(5)]
    res = diff_intent_streams(list(stream), list(stream))
    assert res.ok is True
    assert res.first_divergence is None
    assert res.match_pct == 100.0


def test_missing_expected_intent() -> None:
    expected = [_canon(intent_id=i, price=1000 + i) for i in range(5)]
    actual = expected[:4]  # replay dropped the last intent
    res = diff_intent_streams(expected, actual)
    assert res.ok is False
    assert res.first_divergence.mismatch_type == MismatchType.MISSING_EXPECTED_INTENT.value
    assert res.first_divergence.event_index == 4


def test_unexpected_actual_intent() -> None:
    expected = [_canon(intent_id=i, price=1000 + i) for i in range(4)]
    actual = expected + [_canon(intent_id=99, price=9999)]  # replay emitted an extra
    res = diff_intent_streams(expected, actual)
    assert res.ok is False
    assert res.first_divergence.mismatch_type == MismatchType.UNEXPECTED_ACTUAL_INTENT.value
    assert res.first_divergence.event_index == 4


def test_ordering_mismatch() -> None:
    a = _canon(intent_id=1, price=1000)
    b = _canon(intent_id=2, price=2000)
    res = diff_intent_streams([a, b], [b, a])  # same multiset, swapped order
    assert res.ok is False
    assert res.first_divergence.mismatch_type == MismatchType.ORDERING_MISMATCH.value
    assert res.first_divergence.event_index == 0


def test_intent_hash_mismatch() -> None:
    expected = [_canon(intent_id=0, price=1000), _canon(intent_id=1, price=2000)]
    actual = [_canon(intent_id=0, price=1000), _canon(intent_id=1, price=2500)]
    res = diff_intent_streams(expected, actual)
    assert res.ok is False
    assert res.first_divergence.mismatch_type == MismatchType.INTENT_HASH_MISMATCH.value
    assert res.first_divergence.event_index == 1


def test_empty_actual_is_empty_replay_not_100() -> None:
    expected = [_canon()]
    res = diff_intent_streams(expected, [])
    assert res.ok is False
    assert res.match_pct != 100.0
    assert res.first_divergence.mismatch_type == MismatchType.EMPTY_REPLAY.value


def test_empty_expected_is_missing_log_not_100() -> None:
    actual = [_canon()]
    res = diff_intent_streams([], actual)
    assert res.ok is False
    assert res.match_pct != 100.0
    assert res.first_divergence.mismatch_type == MismatchType.MISSING_INTENT_LOG.value


def test_both_empty_not_100() -> None:
    res = diff_intent_streams([], [])
    assert res.ok is False
    assert res.match_pct != 100.0


def test_missing_intent_log_none() -> None:
    res = diff_intent_streams(None, [_canon()])
    assert res.ok is False
    assert res.first_divergence.mismatch_type == MismatchType.MISSING_INTENT_LOG.value


def test_schema_mismatch() -> None:
    expected = [_canon()]
    skewed = _canon()
    skewed["intent_schema_version"] = "v1"  # version skew
    res = diff_intent_streams(expected, [skewed])
    assert res.ok is False
    assert res.first_divergence.mismatch_type == MismatchType.SCHEMA_MISMATCH.value


def test_first_divergence_is_fully_localizable() -> None:
    expected = [_canon(intent_id=0, price=1000), _canon(intent_id=1, symbol="TXFD6", price=2000)]
    actual = [_canon(intent_id=0, price=1000), _canon(intent_id=1, symbol="TXFD6", price=2500)]
    fd = diff_intent_streams(expected, actual, path_pair="shadow_vs_replay").first_divergence
    assert fd.path_pair == "shadow_vs_replay"
    assert fd.event_index == 1
    assert fd.symbol == "TXFD6"
    assert fd.strategy_id == "r47_maker"
    assert fd.expected_hash and fd.actual_hash
    assert fd.expected_hash != fd.actual_hash
    assert fd.mismatch_type == MismatchType.INTENT_HASH_MISMATCH.value
    d = diff_intent_streams(expected, actual).to_dict()
    assert d["hash_version"] == HASH_VERSION
    assert d["first_divergence"]["event_index"] == 1


def test_optional_context_surfaced_in_first_divergence() -> None:
    e = _canon(intent_id=0, price=1000)
    a = _canon(intent_id=0, price=2000)
    e["feature_set_id"] = "fs-7"
    e["session_phase"] = "day"
    fd = diff_intent_streams([e], [a]).first_divergence
    assert fd.context.get("feature_set_id") == "fs-7"
    assert fd.context.get("session_phase") == "day"
