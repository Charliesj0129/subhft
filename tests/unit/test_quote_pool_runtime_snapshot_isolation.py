"""Regression tests for QuoteConnectionPool runtime snapshot path isolation.

2026-04-27 fix-rc4 (B2 metadata gap):

``QuoteConnectionPool._refresh_options_inner`` historically used
``SYMBOLS_CONFIG`` for the OUTPUT path of its TXO chain auto-refresh.
With the (very common) operator setting ``SYMBOLS_CONFIG=config/symbols.yaml``,
the canonical 1868-line metadata file was overwritten by a 370-line
transient snapshot lacking ``product_type`` / ``tick_size`` / ``price_scale``
/ ``point_value`` — silently degrading every downstream
``SymbolMetadata()`` user (PositionStore, OrderAdapter, CLI).

These tests pin the new contract:

1. The writer never honours ``SYMBOLS_CONFIG`` as an OUTPUT path.
2. ``HFT_SYMBOLS_RUNTIME_SNAPSHOT`` controls the OUTPUT path.
3. Self-clobber detection: if the resolved OUTPUT equals the canonical
   INPUT (the path passed at pool construction), the writer falls back
   to ``data/live_with_options.yaml`` instead of overwriting.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
import yaml

from hft_platform.feed_adapter.shioaji import quote_connection_pool as qcp_mod
from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
    _DEFAULT_RUNTIME_SNAPSHOT_PATH,
    QuoteConnectionPool,
    _derive_sidecar_path,
    _resolve_runtime_snapshot_path,
)

# ---------------------------------------------------------------------------
# Pure path resolver tests (no pool, no broker)
# ---------------------------------------------------------------------------


class TestResolveRuntimeSnapshotPath:
    def test_default_path_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", raising=False)
        assert _resolve_runtime_snapshot_path() == _DEFAULT_RUNTIME_SNAPSHOT_PATH

    def test_env_override_honoured(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        target = str(tmp_path / "snapshot.yaml")
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", target)
        assert _resolve_runtime_snapshot_path() == target

    def test_symbols_config_env_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SYMBOLS_CONFIG MUST NOT influence the output path — that conflation
        was the 2026-04-27 root cause."""
        monkeypatch.setenv("SYMBOLS_CONFIG", "config/symbols.yaml")
        monkeypatch.delenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", raising=False)
        assert _resolve_runtime_snapshot_path() == _DEFAULT_RUNTIME_SNAPSHOT_PATH

    def test_self_clobber_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If operator points HFT_SYMBOLS_RUNTIME_SNAPSHOT at the canonical
        input file, the resolver must refuse and use the safe default."""
        canonical = tmp_path / "symbols.yaml"
        canonical.write_text("symbols: []\n", encoding="utf-8")
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", str(canonical))
        resolved = _resolve_runtime_snapshot_path(str(canonical))
        assert resolved == _DEFAULT_RUNTIME_SNAPSHOT_PATH

    def test_distinct_paths_pass_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        canonical = tmp_path / "symbols.yaml"
        canonical.write_text("symbols: []\n", encoding="utf-8")
        snapshot = tmp_path / "snapshot.yaml"
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", str(snapshot))
        assert _resolve_runtime_snapshot_path(str(canonical)) == str(snapshot)

    def test_resolve_runtime_snapshot_path_collision_with_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """codex P2 #9 regression: operator points SYMBOLS_CONFIG at the
        same file the resolver would default to (``data/live_with_options.yaml``).
        The resolver MUST pick a path different from the input — falling
        back to the default would re-clobber the canonical file."""
        # Simulate the collision: input == _DEFAULT_RUNTIME_SNAPSHOT_PATH.
        # Use a tmp file to keep realpath deterministic, then point the
        # default constant at it for the duration of the test.
        canonical = tmp_path / "live_with_options.yaml"
        canonical.write_text("symbols: []\n", encoding="utf-8")
        monkeypatch.setattr(qcp_mod, "_DEFAULT_RUNTIME_SNAPSHOT_PATH", str(canonical))
        monkeypatch.delenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", raising=False)

        resolved = _resolve_runtime_snapshot_path(str(canonical))

        assert resolved != str(canonical), (
            "P2 #9: resolver returned the input path as the snapshot output — "
            "would self-clobber the canonical symbols file"
        )
        # The default branch must not silently re-clobber, so resolver
        # must escalate to the sidecar derived from the input.
        assert resolved == _derive_sidecar_path(str(canonical))

    def test_resolve_runtime_snapshot_path_double_collision_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """If the env override AND the default AND the derived sidecar all
        collide with the input (pathological operator config), the resolver
        must refuse rather than silently overwrite the canonical file."""
        canonical = tmp_path / "symbols.yaml"
        canonical.write_text("symbols: []\n", encoding="utf-8")

        # Force env + default to point at the canonical file.
        monkeypatch.setattr(qcp_mod, "_DEFAULT_RUNTIME_SNAPSHOT_PATH", str(canonical))
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", str(canonical))
        # Force the derived sidecar to also resolve to the canonical file
        # (in real life this requires an unrealistic operator setup; we
        # simulate it via monkeypatch to exercise the safety branch).
        monkeypatch.setattr(qcp_mod, "_derive_sidecar_path", lambda _p: str(canonical))

        with pytest.raises(RuntimeError, match="HFT_SYMBOLS_RUNTIME_SNAPSHOT"):
            _resolve_runtime_snapshot_path(str(canonical))


# ---------------------------------------------------------------------------
# End-to-end test through ``refresh_options_symbols``
# ---------------------------------------------------------------------------


class TestRefreshDoesNotOverwriteCanonical:
    def _make_pool(self, tmp_path: Path, num_conns: int = 2) -> QuoteConnectionPool:
        """Build a pool with a tiny canonical symbols file."""
        canonical = tmp_path / "symbols.yaml"
        canonical.write_text(
            yaml.safe_dump(
                {
                    "symbols": [
                        {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
                    ]
                }
            ),
            encoding="utf-8",
        )
        pool = QuoteConnectionPool(str(canonical), {}, num_conns=num_conns)
        # Pretend we're in steady state with one client per group.
        pool._clients = [mock.MagicMock() for _ in range(num_conns)]
        for c in pool._clients:
            c.logged_in = False
        return pool

    def test_writer_targets_runtime_snapshot_not_canonical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: refresh_options_symbols writes to the snapshot path
        and leaves the canonical input file byte-identical."""
        pool = self._make_pool(tmp_path)
        canonical_path = Path(pool._symbols_input_path)
        canonical_before = canonical_path.read_bytes()

        snapshot_path = tmp_path / "runtime_snapshot.yaml"
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", str(snapshot_path))

        opts = []
        for s in (20000, 21000):
            opts.append(
                {
                    "code": f"TXO{s}D9",
                    "right": "C",
                    "strike": str(s),
                    "delivery_date": "2099/12/15",  # far future, always active
                    "reference": "20500",
                }
            )
            opts.append(
                {
                    "code": f"TXO{s}P9",
                    "right": "P",
                    "strike": str(s),
                    "delivery_date": "2099/12/15",
                    "reference": "20500",
                }
            )

        with mock.patch.object(type(pool), "_load_options_from_cache", return_value=opts):
            assert pool.refresh_options_symbols() is True

        # Canonical untouched.
        assert canonical_path.read_bytes() == canonical_before, (
            "QuoteConnectionPool overwrote the canonical input file — fix-rc4 regression"
        )
        # Snapshot got written.
        assert snapshot_path.is_file()
        snapshot_text = snapshot_path.read_text(encoding="utf-8")
        assert "Auto-refreshed by QuoteConnectionPool" in snapshot_text
        assert "Source canonical:" in snapshot_text

    def test_self_clobber_via_env_is_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If an operator sets HFT_SYMBOLS_RUNTIME_SNAPSHOT to the canonical
        path (mistake), the writer must refuse to overwrite it."""
        pool = self._make_pool(tmp_path)
        canonical_path = Path(pool._symbols_input_path)
        canonical_before = canonical_path.read_bytes()

        # Try to point the snapshot at the canonical file.
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", str(canonical_path))

        opts = [
            {
                "code": "TXO20000D9",
                "right": "C",
                "strike": "20000",
                "delivery_date": "2099/12/15",
                "reference": "20000",
            },
            {
                "code": "TXO20000P9",
                "right": "P",
                "strike": "20000",
                "delivery_date": "2099/12/15",
                "reference": "20000",
            },
        ]
        with mock.patch.object(type(pool), "_load_options_from_cache", return_value=opts):
            pool.refresh_options_symbols()

        # Canonical input untouched even though env tried to clobber it.
        assert canonical_path.read_bytes() == canonical_before, (
            "self-clobber guard failed — canonical was overwritten"
        )

    def test_legacy_symbols_config_env_does_not_overwrite_canonical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The historical (broken) behaviour was: SYMBOLS_CONFIG env var
        also drove the OUTPUT path. Ensure that's gone — setting it must
        leave canonical untouched."""
        pool = self._make_pool(tmp_path)
        canonical_path = Path(pool._symbols_input_path)
        canonical_before = canonical_path.read_bytes()

        monkeypatch.setenv("SYMBOLS_CONFIG", str(canonical_path))
        monkeypatch.delenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", raising=False)

        opts = [
            {
                "code": "TXO20000D9",
                "right": "C",
                "strike": "20000",
                "delivery_date": "2099/12/15",
                "reference": "20000",
            },
            {
                "code": "TXO20000P9",
                "right": "P",
                "strike": "20000",
                "delivery_date": "2099/12/15",
                "reference": "20000",
            },
        ]
        with mock.patch.object(type(pool), "_load_options_from_cache", return_value=opts):
            pool.refresh_options_symbols()

        # Canonical untouched — proves SYMBOLS_CONFIG no longer drives output.
        assert canonical_path.read_bytes() == canonical_before
