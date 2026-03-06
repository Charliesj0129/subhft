from __future__ import annotations

from hft_platform.feature.engine import FeatureEngine
from hft_platform.strategy.compat import check_strategy_feature_compat


class DummyStrategy:
    strategy_id = "s1"
    required_feature_set_id = "lob_shared_v1"
    required_feature_schema_version = 1
    required_feature_profile_id = None
    required_feature_ids = ["spread_scaled", "microprice_x2"]
    optional_feature_ids = ["nonexistent_optional"]


def test_strategy_feature_compat_reports_optional_only():
    issues = check_strategy_feature_compat(DummyStrategy(), FeatureEngine())
    assert not [i for i in issues if i.level == "error"]
    assert any(i.code == "optional_feature_missing" for i in issues)
