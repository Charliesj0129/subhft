from __future__ import annotations

from pathlib import Path

from hft_platform.feature.engine import FeatureEngine
from hft_platform.feature.profile import FeatureProfileRegistry
from hft_platform.feature.rollout import FeatureRolloutController


def test_feature_profile_registry_load_and_apply(tmp_path: Path):
    path = tmp_path / 'feature_profiles.yaml'
    path.write_text(
        'default_profile_id: p1\nprofiles:\n  - profile_id: p1\n    feature_set_id: lob_shared_v1\n    schema_version: 1\n    enabled: true\n    state: active\n    params:\n      ema_window: 5\n',
        encoding='utf-8',
    )
    reg = FeatureProfileRegistry.from_file(path)
    assert reg.validate() == []
    fe = FeatureEngine()
    prof = reg.get_active_for_set(fe.feature_set_id())
    assert prof is not None
    fe.apply_profile(prof)
    assert fe.active_profile_id() == 'p1'
    assert fe.profile_params().get('ema_window') == 5


def test_feature_rollout_controller_roundtrip_and_rollback(tmp_path: Path):
    path = tmp_path / "feature_rollout_state.json"
    ctrl = FeatureRolloutController(path)
    a1 = ctrl.set_assignment(feature_set_id="lob_shared_v1", state="active", profile_id="p1", actor="test")
    assert a1.active_profile_id == "p1"
    a2 = ctrl.set_assignment(feature_set_id="lob_shared_v1", state="active", profile_id="p2", actor="test")
    assert a2.prev_active_profile_id == "p1"
    ctrl2 = FeatureRolloutController.from_file(path)
    assert ctrl2.resolve_profile_id("lob_shared_v1") == "p2"
    rb = ctrl2.rollback(feature_set_id="lob_shared_v1", actor="test")
    assert rb.active_profile_id == "p1"
