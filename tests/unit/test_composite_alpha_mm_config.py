from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


class TestCompositeAlphaMMConfig:
    """Validate the COMPOSITE_ALPHA_MM_V1 strategy config entry."""

    def test_strategy_yaml_parseable(self) -> None:
        """strategies.yaml must be valid YAML."""
        config_path = Path("config/base/strategies.yaml")
        if not config_path.exists():
            pytest.skip("strategies.yaml not found")
        content = config_path.read_text()
        # Should not raise — result may be None if all entries are commented out
        result = yaml.safe_load(content)
        assert result is None or isinstance(result, dict)

    def test_composite_mm_entry_present(self) -> None:
        """COMPOSITE_ALPHA_MM_V1 must be present in strategies.yaml (even if commented)."""
        config_path = Path("config/base/strategies.yaml")
        if not config_path.exists():
            pytest.skip("strategies.yaml not found")
        content = config_path.read_text()
        assert "COMPOSITE_ALPHA_MM_V1" in content

    def test_composite_mm_module_importable(self) -> None:
        """The strategy module must be importable."""
        with patch.dict("os.environ", {"HFT_FEATURE_ENGINE_ENABLED": "0"}):
            from hft_platform.strategies.composite_alpha_mm import CompositeAlphaMM
        assert CompositeAlphaMM is not None

    def test_composite_mm_required_features(self) -> None:
        """Config references features that exist in the strategy's index constants."""
        from hft_platform.strategies.composite_alpha_mm import (
            _IDX_BEST_ASK,
            _IDX_BEST_BID,
            _IDX_DEPTH_IMBALANCE_EMA8_PPM,
            _IDX_OFI_L1_EMA8,
        )

        # Verify indices are valid non-negative integers
        assert _IDX_BEST_BID >= 0
        assert _IDX_BEST_ASK >= 0
        assert _IDX_OFI_L1_EMA8 >= 0
        assert _IDX_DEPTH_IMBALANCE_EMA8_PPM >= 0

    def test_composite_mm_params_defaults(self) -> None:
        """Strategy should construct with no params (all have defaults)."""
        with patch.dict("os.environ", {"HFT_FEATURE_ENGINE_ENABLED": "0"}):
            from hft_platform.strategies.composite_alpha_mm import CompositeAlphaMM

            strat = CompositeAlphaMM("test_config_mm")
        assert strat._max_position == 50
        assert strat._qty == 1
        assert strat._base_half_spread_ticks == 2

    def test_composite_mm_custom_params(self) -> None:
        """Strategy should accept custom params matching YAML config keys."""
        with patch.dict("os.environ", {"HFT_FEATURE_ENGINE_ENABLED": "1"}):
            from hft_platform.strategies.composite_alpha_mm import CompositeAlphaMM

            strat = CompositeAlphaMM(
                "test_config_mm",
                w_ofi=0.5,
                w_depth=0.3,
                w_slope=0.2,
                base_half_spread_ticks=3,
                inv_skew_per_lot=3000,
                signal_threshold=0.05,
                max_position=100,
                qty=2,
                tick_size_scaled=10000,
                ema_alpha=0.02,
            )
        assert strat._w_ofi == 0.5
        assert strat._max_position == 100
        assert strat._qty == 2
