"""CI unit tests for lob_shape alpha and LobShapeStrategy.

Covers:
- Gate A manifest validation via run_gate_a()
- LobShapeStrategy construction and on_features() hot path
- Feature index consistency with live registry
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pytest


# ── Gate A manifest validation ────────────────────────────────────────────────

class TestGateAManifest:
    def test_gate_a_passes_with_correct_fields(self, tmp_path: Path) -> None:
        from hft_platform.alpha.validation import run_gate_a
        from research.alphas.lob_shape.impl import LobShapeAlpha

        path = tmp_path / "feed.npy"
        arr = np.zeros(8, dtype=[
            ("bids", "i8"), ("asks", "i8"),
            ("ofi_l1_ema8", "i8"), ("depth_imbalance_ema8_ppm", "i8"),
        ])
        np.save(str(path), arr)

        report = run_gate_a(LobShapeAlpha().manifest, [str(path)])
        assert report.passed, f"Gate A failed: {report.details}"

    def test_gate_a_complexity_acceptable(self) -> None:
        from research.alphas.lob_shape.impl import LobShapeAlpha

        # Gate A only accepts O(1) or O(N)
        assert LobShapeAlpha().manifest.complexity in {"O(1)", "O(N)"}

    def test_gate_a_fails_missing_ofi_field(self, tmp_path: Path) -> None:
        from hft_platform.alpha.validation import run_gate_a
        from research.alphas.lob_shape.impl import LobShapeAlpha

        path = tmp_path / "feed.npy"
        arr = np.zeros(8, dtype=[("bids", "i8"), ("asks", "i8")])
        np.save(str(path), arr)

        report = run_gate_a(LobShapeAlpha().manifest, [str(path)])
        assert not report.passed
        assert "ofi_l1_ema8" in report.details["missing_fields"]

    def test_gate_a_alias_bids_lob_bids(self, tmp_path: Path) -> None:
        from hft_platform.alpha.validation import run_gate_a
        from research.alphas.lob_shape.impl import LobShapeAlpha

        path = tmp_path / "feed.npy"
        # Use the alias "lob_bids" / "lob_asks" instead of "bids" / "asks"
        arr = np.zeros(8, dtype=[
            ("lob_bids", "i8"), ("lob_asks", "i8"),
            ("ofi_l1_ema8", "i8"), ("depth_imbalance_ema8_ppm", "i8"),
        ])
        np.save(str(path), arr)

        report = run_gate_a(LobShapeAlpha().manifest, [str(path)])
        assert report.passed, f"Gate A should accept alias 'lob_bids': {report.details}"

    def test_manifest_latency_profile(self) -> None:
        """Manifest must declare latency_profile — missing value blocks Gate D promotion."""
        from research.alphas.lob_shape.impl import LobShapeAlpha

        alpha = LobShapeAlpha()
        assert alpha.manifest.latency_profile is not None, (
            "latency_profile must be set — None blocks Gate D (CLAUDE.md constitution)"
        )


# ── Feature index consistency ─────────────────────────────────────────────────

def test_feature_indices_match_registry() -> None:
    """lob_shape_strategy hardcoded indices must match the live registry."""
    from hft_platform.feature.registry import build_default_lob_feature_set_v1, feature_id_to_index
    from hft_platform.strategies.alpha.lob_shape_strategy import (
        _IDX_BEST_BID,
        _IDX_BEST_ASK,
        _IDX_OFI_L1_EMA8,
        _IDX_DEPTH_IMBALANCE_EMA8_PPM,
    )

    fs = build_default_lob_feature_set_v1()
    assert feature_id_to_index(fs, "best_bid") == _IDX_BEST_BID
    assert feature_id_to_index(fs, "best_ask") == _IDX_BEST_ASK
    assert feature_id_to_index(fs, "ofi_l1_ema8") == _IDX_OFI_L1_EMA8
    assert feature_id_to_index(fs, "depth_imbalance_ema8_ppm") == _IDX_DEPTH_IMBALANCE_EMA8_PPM


# ── LobShapeStrategy construction ────────────────────────────────────────────

class TestLobShapeStrategy:
    def test_strategy_init_defaults(self) -> None:
        from hft_platform.strategies.alpha.lob_shape_strategy import (
            LobShapeStrategy,
            _LAMBDA_DEFAULT,
            _SIGNAL_THRESHOLD_DEFAULT,
            _MAX_POSITION_DEFAULT,
        )
        s = LobShapeStrategy("test_lob_shape")
        assert s.strategy_id == "test_lob_shape"
        assert s._lambda == _LAMBDA_DEFAULT
        assert s._signal_threshold == _SIGNAL_THRESHOLD_DEFAULT
        assert s._max_position == _MAX_POSITION_DEFAULT

    def test_strategy_init_custom_params(self) -> None:
        from hft_platform.strategies.alpha.lob_shape_strategy import LobShapeStrategy
        s = LobShapeStrategy(
            "lob_v1",
            subscribe_symbols=["2330", "2317"],
            lambda_=0.5,
            signal_threshold=0.10,
            max_position=50,
            qty=2,
        )
        assert "2330" in s.symbols
        assert "2317" in s.symbols
        assert s._lambda == 0.5
        assert s._qty == 2

    def test_on_features_no_ctx_is_noop(self) -> None:
        """Strategy with ctx=None must not raise on on_features."""
        from hft_platform.events import FeatureUpdateEvent
        from hft_platform.strategies.alpha.lob_shape_strategy import LobShapeStrategy

        s = LobShapeStrategy("test")
        s.ctx = None
        feat_values = tuple([1_000_000, 1_000_100] + [0] * 14)
        event = FeatureUpdateEvent(
            symbol="2330", ts=1000, local_ts=1001, seq=1,
            feature_set_id="lob_shared_v1", schema_version=1,
            changed_mask=0xFFFF, warmup_ready_mask=0xFFFF,
            quality_flags=0,
            feature_ids=tuple(f"f{i}" for i in range(16)),
            values=feat_values,
        )
        # Should not raise
        result = s.handle_event(None, event)
        assert result == []

    def test_on_features_disabled_flag_is_noop(self, monkeypatch) -> None:
        """When HFT_FEATURE_ENGINE_ENABLED=0, strategy produces no intents."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
        from hft_platform.strategies.alpha.lob_shape_strategy import LobShapeStrategy
        from hft_platform.events import FeatureUpdateEvent

        s = LobShapeStrategy("test")
        # enabled_flag reflects env at construction time
        assert not s._enabled_flag

    def test_on_features_warmup_guard(self, monkeypatch) -> None:
        """Strategy ignores events where rolling features haven't warmed up."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        from hft_platform.strategies.alpha.lob_shape_strategy import (
            LobShapeStrategy,
            _WARMUP_REQUIRED_MASK,
        )
        from hft_platform.events import FeatureUpdateEvent
        import unittest.mock as mock

        s = LobShapeStrategy("test")
        # _enabled_flag must be True from the env var set above — no manual override.
        assert s._enabled_flag, "monkeypatch HFT_FEATURE_ENGINE_ENABLED=1 not picked up"

        mock_ctx = mock.MagicMock()
        mock_ctx.get_feature_tuple.return_value = tuple([1_000_000, 1_000_100] + [100] * 14)
        mock_ctx.positions = {}
        s.ctx = mock_ctx

        # warmup_ready_mask intentionally missing the required bits
        incomplete_mask = 0  # no features warmed up
        feat_values = tuple([1_000_000, 1_000_100] + [100] * 14)
        event = FeatureUpdateEvent(
            symbol="2330", ts=1000, local_ts=1001, seq=1,
            feature_set_id="lob_shared_v1", schema_version=1,
            changed_mask=0xFFFF, warmup_ready_mask=incomplete_mask,
            quality_flags=0,
            feature_ids=tuple(f"f{i}" for i in range(16)),
            values=feat_values,
        )
        result = s.handle_event(mock_ctx, event)
        assert result == [], "Should produce no intents when rolling features not warmed up"

    def test_on_book_update_caches_lob(self, monkeypatch) -> None:
        """on_book_update() stores bids/asks in _lob_cache keyed by symbol."""
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        from hft_platform.strategies.alpha.lob_shape_strategy import LobShapeStrategy
        from hft_platform.events import BidAskEvent, MetaData

        s = LobShapeStrategy("test")
        assert s._enabled_flag

        bids = np.array([[1_000_000, 10], [999_900, 8], [999_800, 6]], dtype=np.int64)
        asks = np.array([[1_000_100, 5], [1_000_200, 4], [1_000_300, 3]], dtype=np.int64)

        meta = MetaData(seq=1, source_ts=1000, local_ts=1001)
        event = BidAskEvent(meta=meta, symbol="2330", bids=bids, asks=asks)
        s.on_book_update(event)

        assert "2330" in s._lob_cache, "_lob_cache must be populated after on_book_update"
        cached_bids, cached_asks = s._lob_cache["2330"]
        assert cached_bids.shape == (3, 2)
        assert cached_asks.shape == (3, 2)

    def test_on_features_full_slope_signal(self, monkeypatch) -> None:
        """Full formula signal = (slope_ask - slope_bid) + λ×sign_align fires a BUY intent.

        Asymmetric LOB: bid-side qty falls steeply (slope_bid very negative),
        ask-side qty falls gently (slope_ask less negative).
        raw_diff = slope_ask - slope_bid > 0.
        With ofi_l1_ema8 and depth_imbalance both positive, sign_align = 1.
        signal ≈ 0.57 >> threshold(0.05) → BUY expected.
        """
        monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "1")
        from hft_platform.strategies.alpha.lob_shape_strategy import LobShapeStrategy
        from hft_platform.events import BidAskEvent, FeatureUpdateEvent, MetaData
        from hft_platform.contracts.strategy import Side
        import unittest.mock as mock

        s = LobShapeStrategy("test")
        assert s._enabled_flag

        # Steep bid-side decline; gentle ask-side decline → raw_diff > 0 (bullish slope)
        bids = np.array([
            [1_000_000, 100], [999_900, 80], [999_800, 60], [999_700, 40], [999_600, 20],
        ], dtype=np.int64)
        asks = np.array([
            [1_000_100, 10], [1_000_200, 9], [1_000_300, 8], [1_000_400, 7], [1_000_500, 6],
        ], dtype=np.int64)

        meta = MetaData(seq=1, source_ts=1000, local_ts=1001)
        book_event = BidAskEvent(meta=meta, symbol="2330", bids=bids, asks=asks)
        s.on_book_update(book_event)

        # Feature tuple: indices 0=best_bid, 1=best_ask, 13=ofi_l1_ema8, 15=depth_imbalance
        feat_values: list[int] = [1_000_000, 1_000_100] + [0] * 11 + [100, 0, 100]
        assert len(feat_values) == 16

        mock_ctx = mock.MagicMock()
        mock_ctx.get_feature_tuple.return_value = tuple(feat_values)
        mock_ctx.positions = {}  # falsy → position() returns 0

        feat_event = FeatureUpdateEvent(
            symbol="2330", ts=1000, local_ts=1001, seq=2,
            feature_set_id="lob_shared_v1", schema_version=1,
            changed_mask=0xFFFF, warmup_ready_mask=0xFFFF,
            quality_flags=0,
            feature_ids=tuple(f"f{i}" for i in range(16)),
            values=tuple(feat_values),
        )

        intents = s.handle_event(mock_ctx, feat_event)

        assert len(intents) == 1, "Positive slope+sign_align signal must emit one BUY intent"
        call_kwargs = mock_ctx.place_order.call_args.kwargs
        assert call_kwargs["side"] == Side.BUY
        assert call_kwargs["symbol"] == "2330"
        assert call_kwargs["price"] == 1_000_000  # best_bid from feature tuple


# ── Research alpha args guard ─────────────────────────────────────────────────

def test_update_raises_on_insufficient_args() -> None:
    """update() with <4 positional args raises ValueError (no silent defaults)."""
    from research.alphas.lob_shape.impl import LobShapeAlpha

    alpha = LobShapeAlpha()
    bids = np.zeros((2, 2), dtype=np.int64)
    asks = np.zeros((2, 2), dtype=np.int64)

    with pytest.raises(ValueError, match="4 positional"):
        alpha.update(bids, asks)  # missing ofi_l1_ema8, depth_imbalance_ema8_ppm
