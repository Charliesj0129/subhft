"""Tests for WAL dedup hash computation (EC-1).

Verifies:
1. Hash uses full 64-char SHA-256 (not truncated)
2. Hash is deterministic across calls with the same data
3. Hash uses sorted keys for dict ordering consistency
4. Fallback to json.dumps works when orjson unavailable
"""

import hashlib
import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_hash_via_insert_with_dedup(rows: list) -> str:
    """Exercise the hash computation path in insert_with_dedup and return the hash."""
    from hft_platform.recorder._loader_ch import insert_with_dedup

    captured: list[str] = []

    svc = MagicMock()
    svc._dedup_enabled = True
    svc.ch_client = MagicMock()
    # Make _is_duplicate return False so we proceed and capture what was recorded
    svc._is_duplicate.return_value = False
    svc.insert_batch.return_value = True

    def _capture_record_dedup(table: str, content_hash: str, row_count: int) -> None:
        captured.append(content_hash)

    svc._record_dedup.side_effect = _capture_record_dedup

    insert_with_dedup(svc, "hft.market_data", rows, "test.jsonl")
    assert captured, "Expected _record_dedup to be called with the hash"
    return captured[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_hash_is_full_sha256_length():
    """Hash must be exactly 64 hex characters (full SHA-256), not truncated."""
    rows = [{"symbol": "TXFD6", "price": 17500, "qty": 1}]
    content_hash = _compute_hash_via_insert_with_dedup(rows)
    assert len(content_hash) == 64, f"Expected 64-char SHA-256 hex digest, got {len(content_hash)}: {content_hash!r}"


def test_hash_is_deterministic():
    """Same rows must produce the same hash on repeated calls."""
    rows = [
        {"symbol": "TXFD6", "price": 17500, "qty": 1},
        {"symbol": "TMFD6", "price": 1750, "qty": 5},
    ]
    hash1 = _compute_hash_via_insert_with_dedup(rows)
    hash2 = _compute_hash_via_insert_with_dedup(rows)
    assert hash1 == hash2, f"Hash not deterministic: {hash1!r} != {hash2!r}"


def test_hash_uses_sorted_keys_for_dict_ordering():
    """Dicts with keys in different orders must hash to the same value."""
    rows_a = [{"b": 2, "a": 1}]
    rows_b = [{"a": 1, "b": 2}]
    hash_a = _compute_hash_via_insert_with_dedup(rows_a)
    hash_b = _compute_hash_via_insert_with_dedup(rows_b)
    assert hash_a == hash_b, f"Key ordering should not affect hash: {hash_a!r} != {hash_b!r}"


def test_different_data_produces_different_hash():
    """Different rows must produce different hashes."""
    rows_a = [{"symbol": "TXFD6", "price": 17500}]
    rows_b = [{"symbol": "TXFD6", "price": 17600}]
    hash_a = _compute_hash_via_insert_with_dedup(rows_a)
    hash_b = _compute_hash_via_insert_with_dedup(rows_b)
    assert hash_a != hash_b


def test_hash_fallback_when_orjson_unavailable():
    """When orjson is not importable, the json fallback must still produce a
    full-length, deterministic, key-sorted SHA-256 hash.
    """
    import json

    rows = [{"b": 2, "a": 1}, {"x": 99}]

    # Compute expected hash manually using the fallback path
    raw = "".join(json.dumps(r, sort_keys=True, default=str) for r in rows).encode()
    expected_hash = hashlib.sha256(raw).hexdigest()

    # Block orjson import inside the module under test
    with patch.dict(sys.modules, {"orjson": None}):
        result_hash = _compute_hash_via_insert_with_dedup(rows)

    assert result_hash == expected_hash, f"Fallback hash mismatch: {result_hash!r} != {expected_hash!r}"
    assert len(result_hash) == 64


def test_hash_fallback_sorted_keys_consistency():
    """Fallback path must also produce consistent hashes regardless of key order."""
    rows_a = [{"z": 3, "a": 1, "m": 2}]
    rows_b = [{"a": 1, "m": 2, "z": 3}]

    with patch.dict(sys.modules, {"orjson": None}):
        hash_a = _compute_hash_via_insert_with_dedup(rows_a)
        hash_b = _compute_hash_via_insert_with_dedup(rows_b)

    assert hash_a == hash_b, f"Fallback key ordering should not affect hash: {hash_a!r} != {hash_b!r}"


def test_empty_rows_returns_true_without_hashing():
    """Empty rows list must return True immediately without computing any hash."""
    from hft_platform.recorder._loader_ch import insert_with_dedup

    svc = MagicMock()
    svc._dedup_enabled = True

    result = insert_with_dedup(svc, "hft.market_data", [], "empty.jsonl")

    assert result is True
    svc._is_duplicate.assert_not_called()
    svc._record_dedup.assert_not_called()
