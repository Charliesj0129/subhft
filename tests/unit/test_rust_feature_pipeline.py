"""Parity tests: RustFeaturePipelineV1 vs Python FeatureEngine compute path.

Validates that the fused Rust pipeline produces identical feature values,
changed masks, and warmup masks as the Python implementation.
"""
import pytest

try:
    try:
        from hft_platform import rust_core

        RustFeaturePipelineV1 = rust_core.RustFeaturePipelineV1
        LobFeatureKernelV1 = rust_core.LobFeatureKernelV1
    except Exception:
        import rust_core

        RustFeaturePipelineV1 = rust_core.RustFeaturePipelineV1
        LobFeatureKernelV1 = rust_core.LobFeatureKernelV1
except Exception:
    RustFeaturePipelineV1 = None
    LobFeatureKernelV1 = None


# 16 features in lob_shared_v1; warmup thresholds (typical)
WARMUP_THRESHOLDS = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2]


@pytest.fixture
def pipeline():
    if RustFeaturePipelineV1 is None:
        pytest.skip("Rust extension not available")
    return RustFeaturePipelineV1(WARMUP_THRESHOLDS)


@pytest.fixture
def kernel():
    if LobFeatureKernelV1 is None:
        pytest.skip("Rust extension not available")
    return LobFeatureKernelV1()


@pytest.mark.skipif(RustFeaturePipelineV1 is None, reason="Rust extension not available")
class TestRustFeaturePipeline:
    def test_first_update_all_changed(self, pipeline):
        values, changed_mask, warmup_mask = pipeline.process(
            best_bid=100_0000, best_ask=101_0000,
            mid_price_x2=201_0000, spread_scaled=1_0000,
            bid_depth=50, ask_depth=40,
            l1_bid_qty=50, l1_ask_qty=40,
            warm_count=1,
        )
        assert len(values) == 16
        # First update: all bits set in changed_mask
        assert changed_mask == (1 << 16) - 1
        # warmup_mask: thresholds [1,1,...,2,2,...] at warm_count=1
        # Features 0-10 have threshold 1, should be ready
        for i in range(11):
            assert warmup_mask & (1 << i), f"Feature {i} should be warm at count=1"

    def test_unchanged_values_zero_mask(self, pipeline):
        args = dict(
            best_bid=100_0000, best_ask=101_0000,
            mid_price_x2=201_0000, spread_scaled=1_0000,
            bid_depth=50, ask_depth=40,
            l1_bid_qty=50, l1_ask_qty=40,
        )
        pipeline.process(**args, warm_count=1)
        # Same input again — stateless features unchanged, EMA features converge
        _values, changed_mask, _warmup_mask = pipeline.process(**args, warm_count=2)
        # OFI-related features (11-15) should change because EMA state updates
        # But stateless features (0-10) should be unchanged
        for i in range(11):
            assert not (changed_mask & (1 << i)), f"Stateless feature {i} should be unchanged"

    def test_warmup_mask_progression(self, pipeline):
        args = dict(
            best_bid=100_0000, best_ask=101_0000,
            mid_price_x2=201_0000, spread_scaled=1_0000,
            bid_depth=50, ask_depth=40,
            l1_bid_qty=50, l1_ask_qty=40,
        )
        _v, _c, warmup1 = pipeline.process(**args, warm_count=1)
        _v, _c, warmup2 = pipeline.process(**args, warm_count=2)
        # At warm_count=2, all features (threshold 1 and 2) should be ready
        assert warmup2 == (1 << 16) - 1

    def test_reset_clears_state(self, pipeline):
        pipeline.process(
            best_bid=100_0000, best_ask=101_0000,
            mid_price_x2=201_0000, spread_scaled=1_0000,
            bid_depth=50, ask_depth=40,
            l1_bid_qty=50, l1_ask_qty=40,
            warm_count=1,
        )
        pipeline.reset()
        # After reset, next call should produce all-changed mask again
        _values, changed_mask, _warmup = pipeline.process(
            best_bid=100_0000, best_ask=101_0000,
            mid_price_x2=201_0000, spread_scaled=1_0000,
            bid_depth=50, ask_depth=40,
            l1_bid_qty=50, l1_ask_qty=40,
            warm_count=1,
        )
        assert changed_mask == (1 << 16) - 1

    def test_parity_with_kernel_v1(self, pipeline, kernel):
        """Pipeline values should match LobFeatureKernelV1.update() exactly."""
        args_list = [
            (100_0000, 101_0000, 201_0000, 1_0000, 50, 40, 50, 40),
            (100_5000, 101_5000, 202_0000, 1_0000, 55, 45, 55, 45),
            (99_0000, 100_0000, 199_0000, 1_0000, 30, 60, 30, 60),
        ]
        for i, args in enumerate(args_list):
            kernel_out = kernel.update(*args)
            pipeline_out, _cm, _wm = pipeline.process(*args, warm_count=i + 1)
            assert len(kernel_out) == len(pipeline_out) == 16
            for j in range(16):
                assert kernel_out[j] == pipeline_out[j], (
                    f"Mismatch at update {i}, feature {j}: "
                    f"kernel={kernel_out[j]} vs pipeline={pipeline_out[j]}"
                )


@pytest.mark.skipif(RustFeaturePipelineV1 is None, reason="Rust extension not available")
class TestRustFeaturePipelineEdgeCases:
    def test_zero_depth(self, pipeline):
        values, _cm, _wm = pipeline.process(
            best_bid=0, best_ask=0,
            mid_price_x2=0, spread_scaled=0,
            bid_depth=0, ask_depth=0,
            l1_bid_qty=0, l1_ask_qty=0,
            warm_count=1,
        )
        # imbalance_ppm should be 0 when depth_total is 0
        assert values[6] == 0  # imbalance_ppm

    def test_negative_depth_clamped(self, pipeline):
        values, _cm, _wm = pipeline.process(
            best_bid=100_0000, best_ask=101_0000,
            mid_price_x2=201_0000, spread_scaled=1_0000,
            bid_depth=-10, ask_depth=-5,
            l1_bid_qty=-3, l1_ask_qty=-2,
            warm_count=1,
        )
        # Negative depths clamped to 0
        assert values[4] == 0  # bid_depth
        assert values[5] == 0  # ask_depth
