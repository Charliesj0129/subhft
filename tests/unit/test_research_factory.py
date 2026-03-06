from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import research.factory as factory
import research.tools.data_governance as data_governance


def _bootstrap_research_root(root: Path) -> None:
    (root / "alphas").mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "data" / "raw").mkdir(parents=True, exist_ok=True)
    (root / "data" / "interim").mkdir(parents=True, exist_ok=True)
    (root / "data" / "processed").mkdir(parents=True, exist_ok=True)


def test_audit_scoped_data_paths_ignore_unrelated_datasets(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)

    unrelated = root / "data" / "raw" / "unrelated.npy"
    np.save(unrelated, np.zeros(8, dtype=np.float64))

    scoped = root / "data" / "interim" / "scoped.npy"
    arr = np.zeros(8, dtype=[("price", "f8"), ("qty", "f8")])
    np.save(scoped, arr)
    rc = data_governance.cmd_stamp_data_meta(
        argparse.Namespace(
            data=str(scoped),
            dataset_id="scoped_v1",
            source_type="synthetic",
            source="unit_test",
            owner="tests",
            schema_version=1,
            symbols="TXF",
            split="full",
            out=None,
        )
    )
    assert rc == 0

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit.json"
    rc = factory.cmd_audit(
        argparse.Namespace(
            out=str(out),
            fail_on_warning=False,
            data=[str(scoped)],
        )
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    gov = payload["details"]["data_governance"]
    assert gov["scope"] == "scoped_data_paths"
    assert gov["missing_metadata_sidecars"] == []
    assert gov["invalid_metadata_sidecars"] == {}
    assert gov["scanned_datasets"] == ["data/interim/scoped.npy"]


def test_audit_scoped_data_paths_reject_invalid_metadata(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)

    scoped = root / "data" / "interim" / "scoped_bad.npy"
    np.save(scoped, np.zeros((6, 2), dtype=np.float64))
    meta = scoped.with_suffix(scoped.suffix + ".meta.json")
    meta.write_text(
        json.dumps(
            {
                "dataset_id": "scoped_bad",
                "source_type": "real",
                "owner": "tests",
                "schema_version": 1,
                "rows": 6,
                "fields": [],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_bad.json"
    rc = factory.cmd_audit(
        argparse.Namespace(
            out=str(out),
            fail_on_warning=False,
            data=[str(scoped)],
        )
    )
    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    gov = payload["details"]["data_governance"]
    assert "data/interim/scoped_bad.npy" in gov["invalid_metadata_sidecars"]
    assert "fields_must_be_nonempty_list" in gov["invalid_metadata_sidecars"]["data/interim/scoped_bad.npy"]
    assert any("metadata sidecar invalid" in err for err in payload["errors"])


# ---------------------------------------------------------------------------
# P0: AlphaManifest skills/roles fields + factory audit warning
# ---------------------------------------------------------------------------


def test_alpha_manifest_roles_skills_default_empty() -> None:
    """AlphaManifest defaults roles_used and skills_used to empty tuple."""
    from research.registry.schemas import AlphaManifest

    m = AlphaManifest(
        alpha_id="test_alpha",
        hypothesis="h",
        formula="f",
        paper_refs=("122",),
        data_fields=("ofi_l1",),
        complexity="O(1)",
    )
    assert m.roles_used == ()
    assert m.skills_used == ()


def test_alpha_manifest_roles_skills_roundtrip() -> None:
    """AlphaManifest roles_used/skills_used survive to_dict/from_dict round-trip."""
    from research.registry.schemas import AlphaManifest

    m = AlphaManifest(
        alpha_id="test_alpha",
        hypothesis="h",
        formula="f",
        paper_refs=(),
        data_fields=(),
        complexity="O(1)",
        roles_used=("planner", "code-reviewer"),
        skills_used=("iterative-retrieval", "hft-backtester"),
    )
    data = m.to_dict()
    assert list(data["roles_used"]) == ["planner", "code-reviewer"]
    assert list(data["skills_used"]) == ["iterative-retrieval", "hft-backtester"]

    m2 = AlphaManifest.from_dict(data)
    assert m2.roles_used == ("planner", "code-reviewer")
    assert m2.skills_used == ("iterative-retrieval", "hft-backtester")


def test_factory_audit_warns_when_alpha_has_no_skills(monkeypatch, tmp_path: Path) -> None:
    """Factory audit warns when a governed alpha's manifest has empty skills_used."""
    import research.factory as fct
    from research.registry.schemas import AlphaManifest

    root = tmp_path / "research"
    _bootstrap_research_root(root)

    # Build a minimal governed alpha structure (file layout)
    alpha_dir = root / "alphas" / "dummy_alpha"
    tests_dir = alpha_dir / "tests"
    tests_dir.mkdir(parents=True)
    (alpha_dir / "__init__.py").write_text("")
    (alpha_dir / "README.md").write_text("# dummy\n")
    (alpha_dir / "impl.py").write_text("")
    (tests_dir / "test_dummy.py").write_text("def test_placeholder(): pass\n")

    # Stub AlphaRegistry.discover to return a controlled alpha with empty skills_used
    class _DummyAlpha:
        @property
        def manifest(self):
            return AlphaManifest(
                alpha_id="dummy_alpha",
                hypothesis="h",
                formula="f",
                paper_refs=(),
                data_fields=(),
                complexity="O(1)",
                # skills_used defaults to () — triggers factory warning
            )

        def update(self, *a, **k):
            return 0.0

        def reset(self):
            pass

        def get_signal(self):
            return 0.0

    from research.registry import alpha_registry as _ar_mod

    class _StubRegistry:
        errors = ()

        def discover(self, _path):
            return {"dummy_alpha": _DummyAlpha()}

    monkeypatch.setattr(_ar_mod, "AlphaRegistry", _StubRegistry)
    monkeypatch.setattr(fct, "ROOT", root)
    out = tmp_path / "audit_skills.json"
    rc = fct.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))
    payload = json.loads(out.read_text(encoding="utf-8"))
    contract = payload["details"]["alpha_contract"]
    assert "dummy_alpha" in contract["alphas_missing_skills"]
    # rc=0 because fail_on_warning is False
    assert rc == 0
    # confirm warning message present
    assert any("skills_used" in w for w in payload["warnings"])


# ---------------------------------------------------------------------------
# P4: feature_set_version in AlphaManifest + from_dict round-trip
# ---------------------------------------------------------------------------


def test_alpha_manifest_feature_set_version_default_none() -> None:
    """AlphaManifest.feature_set_version defaults to None."""
    from research.registry.schemas import AlphaManifest

    m = AlphaManifest(
        alpha_id="test",
        hypothesis="h",
        formula="f",
        paper_refs=(),
        data_fields=(),
        complexity="O(1)",
    )
    assert m.feature_set_version is None


def test_alpha_manifest_feature_set_version_roundtrip() -> None:
    """feature_set_version survives to_dict/from_dict round-trip."""
    from research.registry.schemas import AlphaManifest

    m = AlphaManifest(
        alpha_id="test",
        hypothesis="h",
        formula="f",
        paper_refs=(),
        data_fields=(),
        complexity="O(1)",
        feature_set_version="lob_shared_v1",
    )
    data = m.to_dict()
    assert data["feature_set_version"] == "lob_shared_v1"
    m2 = AlphaManifest.from_dict(data)
    assert m2.feature_set_version == "lob_shared_v1"


def test_feature_set_version_constant_matches_default_set() -> None:
    """FEATURE_SET_VERSION constant equals the default FeatureSet id."""
    from hft_platform.feature.registry import FEATURE_SET_VERSION, build_default_lob_feature_set_v1

    fs = build_default_lob_feature_set_v1()
    assert fs.feature_set_id == FEATURE_SET_VERSION
