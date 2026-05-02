"""Coverage tests for strategy/compat.py — uncovered branches."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from hft_platform.strategy.compat import (
    StrategyFeatureCompatibilityIssue,
    _safe_get_feature_ids,
    _safe_get_feature_set_id,
    _safe_get_profile_id,
    _safe_get_schema_version,
    check_strategies_feature_compat,
    check_strategy_feature_compat,
)

# ---------------------------------------------------------------------------
# _safe_get_* helpers
# ---------------------------------------------------------------------------


class TestSafeGetFeatureSetId:
    def test_returns_none_for_none_engine(self):
        assert _safe_get_feature_set_id(None) is None

    def test_returns_none_when_not_callable(self):
        engine = SimpleNamespace(feature_set_id="not_callable")
        assert _safe_get_feature_set_id(engine) is None

    def test_returns_value_when_callable(self):
        engine = MagicMock()
        engine.feature_set_id.return_value = "lob_shared_v3"
        assert _safe_get_feature_set_id(engine) == "lob_shared_v3"

    def test_returns_none_on_exception(self):
        engine = MagicMock()
        engine.feature_set_id.side_effect = RuntimeError("boom")
        assert _safe_get_feature_set_id(engine) is None


class TestSafeGetFeatureIds:
    def test_returns_empty_for_none_engine(self):
        assert _safe_get_feature_ids(None) == set()

    def test_returns_empty_when_not_callable(self):
        engine = SimpleNamespace(feature_ids="string")
        assert _safe_get_feature_ids(engine) == set()

    def test_returns_set_when_callable(self):
        engine = MagicMock()
        engine.feature_ids.return_value = ["ofi", "spread", "imbalance"]
        result = _safe_get_feature_ids(engine)
        assert result == {"ofi", "spread", "imbalance"}

    def test_returns_empty_on_exception(self):
        engine = MagicMock()
        engine.feature_ids.side_effect = RuntimeError("boom")
        assert _safe_get_feature_ids(engine) == set()

    def test_returns_empty_when_none_returned(self):
        engine = MagicMock()
        engine.feature_ids.return_value = None
        assert _safe_get_feature_ids(engine) == set()


class TestSafeGetSchemaVersion:
    def test_returns_none_for_none_engine(self):
        assert _safe_get_schema_version(None) is None

    def test_returns_none_when_not_callable(self):
        engine = SimpleNamespace(schema_version=3)
        assert _safe_get_schema_version(engine) is None

    def test_returns_int_when_callable(self):
        engine = MagicMock()
        engine.schema_version.return_value = 3
        assert _safe_get_schema_version(engine) == 3

    def test_returns_none_on_exception(self):
        engine = MagicMock()
        engine.schema_version.side_effect = TypeError("bad")
        assert _safe_get_schema_version(engine) is None


class TestSafeGetProfileId:
    def test_returns_none_for_none_engine(self):
        assert _safe_get_profile_id(None) is None

    def test_returns_profile_from_active_profile_id(self):
        engine = MagicMock()
        engine.active_profile_id.return_value = "default"
        assert _safe_get_profile_id(engine) == "default"

    def test_returns_none_when_empty_string(self):
        engine = MagicMock()
        engine.active_profile_id.return_value = ""
        engine.profile_id.return_value = ""
        assert _safe_get_profile_id(engine) is None

    def test_falls_back_to_profile_id(self):
        engine = MagicMock(spec=[])
        engine.profile_id = MagicMock(return_value="fallback")
        assert _safe_get_profile_id(engine) == "fallback"

    def test_returns_none_on_exception(self):
        engine = MagicMock()
        engine.active_profile_id.side_effect = RuntimeError("boom")
        engine.profile_id.side_effect = RuntimeError("boom")
        assert _safe_get_profile_id(engine) is None


# ---------------------------------------------------------------------------
# check_strategy_feature_compat
# ---------------------------------------------------------------------------


class TestCheckStrategyFeatureCompat:
    def _make_strategy(self, **kwargs):
        defaults = {
            "strategy_id": "test_strat",
            "required_feature_set_id": None,
            "required_feature_schema_version": None,
            "required_feature_ids": None,
            "optional_feature_ids": None,
            "required_feature_profile_id": None,
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_no_requirements_returns_empty(self):
        strategy = self._make_strategy()
        issues = check_strategy_feature_compat(strategy, None)
        assert issues == []

    def test_feature_engine_missing_with_requirements(self):
        strategy = self._make_strategy(required_feature_set_id="lob_shared_v3")
        issues = check_strategy_feature_compat(strategy, None)
        assert len(issues) == 1
        assert issues[0].code == "feature_engine_missing"

    def test_feature_set_mismatch(self):
        strategy = self._make_strategy(required_feature_set_id="lob_shared_v3")
        engine = MagicMock()
        engine.feature_set_id.return_value = "lob_shared_v2"
        engine.schema_version.return_value = 3
        engine.feature_ids.return_value = []
        engine.active_profile_id.return_value = None
        issues = check_strategy_feature_compat(strategy, engine)
        codes = [i.code for i in issues]
        assert "feature_set_mismatch" in codes

    def test_feature_set_unavailable(self):
        strategy = self._make_strategy(required_feature_set_id="lob_shared_v3")
        engine = MagicMock()
        engine.feature_set_id.side_effect = RuntimeError("unavailable")
        engine.schema_version.return_value = 3
        engine.feature_ids.return_value = []
        engine.active_profile_id.return_value = None
        issues = check_strategy_feature_compat(strategy, engine)
        codes = [i.code for i in issues]
        assert "feature_set_unavailable" in codes

    def test_schema_too_old(self):
        strategy = self._make_strategy(required_feature_schema_version=3)
        engine = MagicMock()
        engine.feature_set_id.return_value = None
        engine.schema_version.return_value = 2
        engine.feature_ids.return_value = []
        engine.active_profile_id.return_value = None
        issues = check_strategy_feature_compat(strategy, engine)
        codes = [i.code for i in issues]
        assert "feature_schema_too_old" in codes

    def test_profile_mismatch(self):
        strategy = self._make_strategy(required_feature_profile_id="custom")
        engine = MagicMock()
        engine.feature_set_id.return_value = None
        engine.schema_version.return_value = None
        engine.feature_ids.return_value = []
        engine.active_profile_id.return_value = "default"
        issues = check_strategy_feature_compat(strategy, engine)
        codes = [i.code for i in issues]
        assert "feature_profile_mismatch" in codes

    def test_profile_unavailable(self):
        strategy = self._make_strategy(required_feature_profile_id="custom")
        engine = MagicMock()
        engine.feature_set_id.return_value = None
        engine.schema_version.return_value = None
        engine.feature_ids.return_value = []
        engine.active_profile_id.return_value = None
        engine.profile_id.return_value = None
        issues = check_strategy_feature_compat(strategy, engine)
        codes = [i.code for i in issues]
        assert "feature_profile_unavailable" in codes

    def test_required_feature_missing(self):
        strategy = self._make_strategy(required_feature_ids=["ofi", "missing_feature"])
        engine = MagicMock()
        engine.feature_set_id.return_value = None
        engine.schema_version.return_value = None
        engine.feature_ids.return_value = ["ofi", "spread"]
        engine.active_profile_id.return_value = None
        issues = check_strategy_feature_compat(strategy, engine)
        codes = [i.code for i in issues]
        assert "required_feature_missing" in codes

    def test_optional_feature_missing(self):
        strategy = self._make_strategy(optional_feature_ids=["optional_feat"])
        engine = MagicMock()
        engine.feature_set_id.return_value = None
        engine.schema_version.return_value = None
        engine.feature_ids.return_value = ["ofi"]
        engine.active_profile_id.return_value = None
        issues = check_strategy_feature_compat(strategy, engine)
        codes = [i.code for i in issues]
        assert "optional_feature_missing" in codes
        assert issues[0].level == "warning"


# ---------------------------------------------------------------------------
# check_strategies_feature_compat (batch)
# ---------------------------------------------------------------------------


class TestCheckStrategiesBatch:
    def test_aggregates_issues_from_multiple_strategies(self):
        s1 = SimpleNamespace(
            strategy_id="s1",
            required_feature_set_id="v3",
            required_feature_schema_version=None,
            required_feature_ids=None,
            optional_feature_ids=None,
            required_feature_profile_id=None,
        )
        s2 = SimpleNamespace(
            strategy_id="s2",
            required_feature_set_id=None,
            required_feature_schema_version=None,
            required_feature_ids=["missing"],
            optional_feature_ids=None,
            required_feature_profile_id=None,
        )
        engine = MagicMock()
        engine.feature_set_id.return_value = "v3"
        engine.schema_version.return_value = 3
        engine.feature_ids.return_value = ["ofi"]
        engine.active_profile_id.return_value = None

        issues = check_strategies_feature_compat([s1, s2], engine)
        strategy_ids = {i.strategy_id for i in issues}
        assert "s2" in strategy_ids


# ---------------------------------------------------------------------------
# StrategyFeatureCompatibilityIssue.to_dict
# ---------------------------------------------------------------------------


class TestIssueToDict:
    def test_to_dict_contains_all_fields(self):
        issue = StrategyFeatureCompatibilityIssue(
            strategy_id="test",
            level="error",
            code="test_code",
            message="test message",
        )
        d = issue.to_dict()
        assert d["strategy_id"] == "test"
        assert d["level"] == "error"
        assert d["code"] == "test_code"
        assert d["message"] == "test message"
