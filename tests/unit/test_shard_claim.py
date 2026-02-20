"""Tests for CE3-03: FileClaimRegistry."""
import os
import tempfile
import threading

import pytest

from hft_platform.recorder.shard_claim import FileClaimRegistry


def _make_registry(tmpdir, enabled=True):
    claim_dir = os.path.join(tmpdir, "claims")
    return FileClaimRegistry(claim_dir=claim_dir, enabled=enabled)


def test_try_claim_returns_true_when_unclaimed():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = _make_registry(tmpdir)
        assert reg.try_claim("file1.jsonl") is True
        reg.release_claim("file1.jsonl")


def test_second_claim_returns_false():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = _make_registry(tmpdir)
        assert reg.try_claim("file1.jsonl") is True
        # Same process — second claim returns False (already held)
        assert reg.try_claim("file1.jsonl") is False
        reg.release_claim("file1.jsonl")


def test_release_allows_reclaim():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = _make_registry(tmpdir)
        reg.try_claim("file1.jsonl")
        reg.release_claim("file1.jsonl")
        assert reg.try_claim("file1.jsonl") is True
        reg.release_claim("file1.jsonl")


def test_two_threads_only_one_claims():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = _make_registry(tmpdir)
        results = []
        lock = threading.Lock()

        def claim():
            ok = reg.try_claim("shared.jsonl")
            with lock:
                results.append(ok)

        t1 = threading.Thread(target=claim)
        t2 = threading.Thread(target=claim)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # Exactly one should have succeeded
        assert sum(results) == 1
        # Cleanup
        for r in results:
            if r:
                reg.release_claim("shared.jsonl")


def test_recover_stale_claims():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = _make_registry(tmpdir)
        # Create a .claim file without holding the lock (simulates stale crash)
        claim_dir = os.path.join(tmpdir, "claims")
        os.makedirs(claim_dir, exist_ok=True)
        stale_path = os.path.join(claim_dir, "stale.jsonl.claim")
        open(stale_path, "w").close()

        reg.recover_stale_claims()

        # The stale file should have been removed (and the directory is clean)
        assert not os.path.exists(stale_path)


def test_disabled_always_returns_true():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = _make_registry(tmpdir, enabled=False)
        assert reg.try_claim("any_file.jsonl") is True
        reg.release_claim("any_file.jsonl")  # Should not raise


def test_release_nonexistent_key_is_safe():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = _make_registry(tmpdir)
        # Releasing a key never claimed should not raise
        reg.release_claim("ghost.jsonl")
