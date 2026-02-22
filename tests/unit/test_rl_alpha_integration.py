from pathlib import Path

import numpy as np

from hft_platform.alpha.experiments import ExperimentTracker
from research.registry.alpha_registry import AlphaRegistry
from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier
from research.rl.alpha_adapter import RLAlphaAdapter, RLAlphaConfig
from research.rl.lifecycle import RLRunConfig, log_rl_run, promote_latest_rl_run, register_rl_alpha
from research.rl.registry_features import RegistryFeatureProvider


class _DummyAlpha:
    def __init__(self, alpha_id: str):
        self._alpha_id = alpha_id
        self._signal = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id=self._alpha_id,
            hypothesis="dummy",
            formula="dummy",
            paper_refs=(),
            data_fields=("current_mid",),
            complexity="O(1)",
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.ENSEMBLE,
            rust_module=None,
        )

    def update(self, **tick_data):
        self._signal = float(tick_data.get("current_mid", 0.0))
        return self._signal

    def reset(self):
        self._signal = 0.0

    def get_signal(self):
        return self._signal


def test_rl_alpha_adapter_predictor_and_register():
    cfg = RLAlphaConfig(
        alpha_id="rl_test",
        feature_fields=("f1", "f2"),
        model_path=None,
        paper_refs=("rl",),
    )
    alpha = RLAlphaAdapter(config=cfg, predictor=lambda x: float(np.sum(x)))
    signal = alpha.update(f1=0.5, f2=-0.2)
    assert -1.0 <= signal <= 1.0
    assert alpha.manifest.tier == AlphaTier.RL

    registry = AlphaRegistry()
    registered = register_rl_alpha(registry=registry, config=cfg, predictor=lambda x: float(np.sum(x)))
    assert registered.manifest.alpha_id in registry.list_alpha_ids()


def test_rl_lifecycle_log_run(tmp_path: Path):
    run_cfg = RLRunConfig(
        alpha_id="rl_test",
        model_path="research/rl/model.npz",
        feature_fields=("f1", "f2"),
        params={"lr": 1e-3},
        data_paths=("d.npy",),
        owner="charlie",
    )
    meta_path = log_rl_run(
        run_config=run_cfg,
        rewards=np.array([0.1, -0.05, 0.2, 0.15], dtype=np.float64),
        signals=np.array([0.2, -0.1, 0.4, 0.3], dtype=np.float64),
        base_dir=str(tmp_path / "experiments"),
    )
    assert Path(meta_path).exists()

    tracker = ExperimentTracker(base_dir=tmp_path / "experiments")
    rows = tracker.list_runs(alpha_id="rl_test")
    assert len(rows) == 1
    assert rows[0].run_id


def test_registry_feature_provider():
    provider = RegistryFeatureProvider(
        alpha_ids=("a", "b"),
        _alphas={"a": _DummyAlpha("a"), "b": _DummyAlpha("b")},
    )
    provider.reset()
    feats = provider.update(current_mid=12.5)
    assert feats.shape == (2,)
    assert np.allclose(feats, np.array([12.5, 12.5], dtype=np.float32))


def test_promote_latest_rl_run(tmp_path: Path):
    base_dir = tmp_path / "experiments"
    run_cfg = RLRunConfig(
        alpha_id="rl_promote",
        model_path="research/rl/model.npz",
        feature_fields=("f1", "f2"),
        params={"lr": 5e-4},
        data_paths=("d.npy",),
        owner="charlie",
    )
    log_rl_run(
        run_config=run_cfg,
        rewards=np.array([0.2, 0.15, 0.25, 0.18], dtype=np.float64),
        signals=np.array([0.1, 0.2, 0.15, 0.25], dtype=np.float64),
        base_dir=str(base_dir),
    )

    result = promote_latest_rl_run(
        alpha_id="rl_promote",
        owner="charlie",
        base_dir=str(base_dir),
        project_root=str(tmp_path),
        shadow_sessions=2,
        min_shadow_sessions=0,
        drift_alerts=0,
        execution_reject_rate=0.0,
    )
    assert result.alpha_id == "rl_promote"
    assert result.approved
    assert result.promotion_config_path is not None
