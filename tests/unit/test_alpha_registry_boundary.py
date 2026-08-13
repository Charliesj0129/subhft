"""Boundary and compatibility tests for canonical alpha discovery."""

from __future__ import annotations

import ast
import base64
import pickle
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.alpha.discovery import AlphaDiscoveryRegistry, _to_module_name
from hft_platform.contracts.alpha import AlphaManifest, AlphaProtocol, AlphaStatus, AlphaTier
from hft_platform.monitor._alpha_dispatcher import AlphaDispatcher
from research.registry import alpha_registry as legacy_registry_module
from research.registry.alpha_registry import AlphaRegistry
from research.registry.alpha_registry import _to_module_name as legacy_to_module_name

_LEGACY_EMPTY_REGISTRY_PICKLE = (
    "gASVVgAAAAAAAACMIHJlc2VhcmNoLnJlZ2lzdHJ5LmFscGhhX3JlZ2lzdHJ5"
    "lIwNQWxwaGFSZWdpc3RyeZSTlCmBlH2UKIwHX2FscGhhc5R9lIwHX2Vycm9y"
    "c5RdlHViLg=="
)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_import_probe(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        check=False,
        capture_output=True,
        cwd=_REPO_ROOT,
        text=True,
    )


class _Alpha:
    manifest = AlphaManifest(
        alpha_id="boundary_alpha",
        hypothesis="boundary",
        formula="1",
        paper_refs=(),
        data_fields=(),
        complexity="O(1)",
        status=AlphaStatus.DRAFT,
        tier=AlphaTier.TIER_1,
    )

    def update(self, **tick_data: object) -> float:
        return float(len(tick_data))

    def reset(self) -> None:
        return None

    def get_signal(self) -> float:
        return 0.0


def test_legacy_registry_inherits_canonical_discovery_without_overrides() -> None:
    assert issubclass(AlphaRegistry, AlphaDiscoveryRegistry)
    assert AlphaRegistry.__module__ == "research.registry.alpha_registry"
    assert AlphaRegistry.discover is AlphaDiscoveryRegistry.discover
    assert AlphaRegistry.register is AlphaDiscoveryRegistry.register
    assert legacy_to_module_name is _to_module_name
    assert legacy_registry_module.AlphaManifest is AlphaManifest
    assert legacy_registry_module.AlphaProtocol is AlphaProtocol
    assert legacy_registry_module.AlphaStatus is AlphaStatus
    assert legacy_registry_module.AlphaTier is AlphaTier


def test_research_package_import_does_not_eagerly_load_legacy_registry() -> None:
    result = _run_import_probe(
        "\n".join(
            [
                "import sys",
                "import research",
                "assert 'research.registry.alpha_registry' not in sys.modules",
                "assert 'research.registry.schemas' not in sys.modules",
            ]
        )
    )

    assert result.returncode == 0, result.stderr


def test_research_registry_package_import_does_not_eagerly_load_services() -> None:
    result = _run_import_probe(
        "\n".join(
            [
                "import sys",
                "import research.registry",
                "for name in (",
                "    'research.registry.alpha_registry',",
                "    'research.registry.correlation_tracker',",
                "    'research.registry.pool_optimizer',",
                "    'research.registry.scorecard',",
                "):",
                "    assert name not in sys.modules, name",
            ]
        )
    )

    assert result.returncode == 0, result.stderr


def test_legacy_package_exports_resolve_lazily_with_identity_preserved() -> None:
    result = _run_import_probe(
        "\n".join(
            [
                "import importlib",
                "import research",
                "import research.registry",
                "root_exports = {",
                "    'AlphaManifest': ('research.registry.schemas', 'AlphaManifest'),",
                "    'AlphaProtocol': ('research.registry.schemas', 'AlphaProtocol'),",
                "    'AlphaRegistry': ('research.registry.alpha_registry', 'AlphaRegistry'),",
                "    'AlphaStatus': ('research.registry.schemas', 'AlphaStatus'),",
                "    'AlphaTier': ('research.registry.schemas', 'AlphaTier'),",
                "    'Scorecard': ('research.registry.schemas', 'Scorecard'),",
                "}",
                "registry_exports = {",
                "    'AlphaManifest': ('research.registry.schemas', 'AlphaManifest'),",
                "    'AlphaPoolOptimizer': ('research.registry.pool_optimizer', 'AlphaPoolOptimizer'),",
                "    'AlphaProtocol': ('research.registry.schemas', 'AlphaProtocol'),",
                "    'AlphaRegistry': ('research.registry.alpha_registry', 'AlphaRegistry'),",
                "    'AlphaStatus': ('research.registry.schemas', 'AlphaStatus'),",
                "    'AlphaTier': ('research.registry.schemas', 'AlphaTier'),",
                "    'CorrelationTracker': ('research.registry.correlation_tracker', 'CorrelationTracker'),",
                "    'Scorecard': ('research.registry.schemas', 'Scorecard'),",
                "    'compute_scorecard': ('research.registry.scorecard', 'compute_scorecard'),",
                "    'load_scorecard': ('research.registry.scorecard', 'load_scorecard'),",
                "    'save_scorecard': ('research.registry.scorecard', 'save_scorecard'),",
                "}",
                "for name, (module_name, concrete_name) in root_exports.items():",
                "    assert getattr(research, name) is getattr(importlib.import_module(module_name), concrete_name)",
                "for name, (module_name, concrete_name) in registry_exports.items():",
                "    assert getattr(research.registry, name) is getattr(importlib.import_module(module_name), concrete_name)",
            ]
        )
    )

    assert result.returncode == 0, result.stderr


def test_unknown_attributes_and_normal_research_submodule_imports_remain_supported() -> None:
    result = _run_import_probe(
        "\n".join(
            [
                "import research",
                "try:",
                "    research.not_a_real_export",
                "except AttributeError:",
                "    pass",
                "else:",
                "    raise AssertionError('unknown export did not raise AttributeError')",
                "from research import factory",
                "assert factory.__name__ == 'research.factory'",
            ]
        )
    )

    assert result.returncode == 0, result.stderr


def test_real_alpha_discovery_does_not_transitively_load_legacy_registry() -> None:
    result = _run_import_probe(
        "\n".join(
            [
                "import sys",
                "from hft_platform.alpha.discovery import AlphaDiscoveryRegistry",
                "AlphaDiscoveryRegistry().discover('research/alphas')",
                "assert 'research.registry.alpha_registry' not in sys.modules",
            ]
        )
    )

    assert result.returncode == 0, result.stderr


def test_legacy_protocol_four_pickle_resolves_to_compatible_registry() -> None:
    registry = pickle.loads(base64.b64decode(_LEGACY_EMPTY_REGISTRY_PICKLE))

    assert type(registry) is AlphaRegistry
    assert registry.list_alpha_ids() == []
    assert registry.errors == ()


def test_register_queries_and_duplicate_guard_match_legacy_behavior() -> None:
    registry = AlphaDiscoveryRegistry()
    alpha = _Alpha()

    registry.register(alpha)

    assert registry.get("boundary_alpha") is alpha
    assert registry.list_alpha_ids() == ["boundary_alpha"]
    assert registry.list_by_status(AlphaStatus.DRAFT) == [alpha.manifest]
    assert registry.list_by_tier(AlphaTier.TIER_1) == [alpha.manifest]
    assert registry.manifests() == [alpha.manifest]
    with pytest.raises(ValueError, match="Duplicate alpha_id already registered"):
        registry.register(alpha)
    with pytest.raises(TypeError, match="Alpha does not conform to AlphaProtocol"):
        registry.register(cast(AlphaProtocol, object()))


def test_discover_isolates_import_errors_and_reports_empty_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alphas_dir = tmp_path / "research" / "alphas"
    for alpha_id in ("good", "empty", "broken", "_private"):
        alpha_dir = alphas_dir / alpha_id
        alpha_dir.mkdir(parents=True)
        (alpha_dir / "impl.py").write_text("# discovery fixture\n", encoding="utf-8")

    good_module = ModuleType("research.alphas.good.impl")
    setattr(good_module, "ALPHA_CLASS", _Alpha)
    empty_module = ModuleType("research.alphas.empty.impl")
    imported: list[str] = []

    def _import_module(name: str) -> ModuleType:
        imported.append(name)
        if name.endswith(".good.impl"):
            return good_module
        if name.endswith(".empty.impl"):
            return empty_module
        raise RuntimeError("isolated import failure")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hft_platform.alpha.discovery.importlib.import_module", _import_module)

    registry = AlphaDiscoveryRegistry()
    discovered = registry.discover("research/alphas")

    assert set(discovered) == {"boundary_alpha"}
    assert isinstance(discovered["boundary_alpha"], _Alpha)
    assert "research.alphas._private.impl" not in imported
    assert any("No AlphaProtocol implementation discovered" in error for error in registry.errors)
    assert any("isolated import failure" in error for error in registry.errors)


def test_discover_missing_directory_returns_registered_copy(tmp_path: Path) -> None:
    alpha = _Alpha()
    registry = AlphaDiscoveryRegistry()
    registry.register(alpha)

    discovered = registry.discover(tmp_path / "missing")
    discovered.clear()

    assert registry.get("boundary_alpha") is alpha


def test_implicit_discovery_skips_foreign_required_and_broken_classes() -> None:
    module = ModuleType("research.alphas.implicit.impl")
    valid_cls = type(
        "ImplicitAlpha",
        (),
        {
            "__module__": module.__name__,
            "manifest": _Alpha.manifest,
            "update": _Alpha.update,
            "reset": _Alpha.reset,
            "get_signal": _Alpha.get_signal,
        },
    )

    class RequiredArgument:
        def __init__(self, value: object) -> None:
            self.value = value

    class BrokenConstructor:
        def __init__(self) -> None:
            raise RuntimeError("constructor failure")

    RequiredArgument.__module__ = module.__name__
    BrokenConstructor.__module__ = module.__name__
    setattr(module, "Valid", valid_cls)
    setattr(module, "Required", RequiredArgument)
    setattr(module, "Broken", BrokenConstructor)
    setattr(module, "Foreign", _Alpha)

    registry = AlphaDiscoveryRegistry()

    assert registry._load_module_alphas(module) is True
    assert registry.list_alpha_ids() == ["boundary_alpha"]


def test_construct_and_safe_register_failures_are_isolated() -> None:
    class InvalidAlpha:
        pass

    registry = AlphaDiscoveryRegistry()
    with patch("hft_platform.alpha.discovery.inspect.signature", side_effect=ValueError("no signature")):
        assert registry._try_construct(InvalidAlpha) is None
    assert registry._try_construct(InvalidAlpha) is None

    registry._safe_register(_Alpha())
    registry._safe_register(_Alpha())
    assert any("Duplicate alpha_id already registered" in error for error in registry.errors)


def test_to_module_name_handles_cwd_and_absolute_research_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert _to_module_name(tmp_path / "research" / "alphas" / "a" / "impl.py") == "research.alphas.a.impl"
    assert _to_module_name(Path("/outside/worktree/research/alphas/b/impl.py")) == "research.alphas.b.impl"


def test_research_registry_retains_correlation_service() -> None:
    tracker = MagicMock()
    tracker.compute_matrix.return_value = {"alpha_ids": ["a"]}

    with patch("research.registry.alpha_registry.CorrelationTracker", return_value=tracker):
        result = AlphaRegistry().compute_correlation_matrix({"a": [1.0, 2.0]})

    assert result == {"alpha_ids": ["a"]}
    tracker.compute_matrix.assert_called_once_with({"a": [1.0, 2.0]})


def test_monitor_dispatcher_loads_through_canonical_registry(tmp_path: Path) -> None:
    with (
        patch.object(AlphaDiscoveryRegistry, "discover", return_value={"boundary_alpha": _Alpha()}),
        patch("hft_platform.monitor._alpha_dispatcher._load_promotion_weights", return_value={}),
    ):
        loaded = AlphaDispatcher().load_alphas(
            ("boundary_alpha", "missing"),
            alphas_dir=tmp_path,
            promotions_dir=tmp_path,
        )

    assert loaded == ["boundary_alpha"]


@pytest.mark.parametrize(
    "path",
    [
        Path("src/hft_platform/alpha/discovery.py"),
        Path("src/hft_platform/alpha/validation.py"),
        Path("src/hft_platform/monitor/_alpha_dispatcher.py"),
    ],
)
def test_platform_discovery_consumers_do_not_import_research(path: Path) -> None:
    source_path = _REPO_ROOT / path
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    research_imports = [
        node
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and (node.module == "research" or node.module.startswith("research."))
        )
        or (
            isinstance(node, ast.Import)
            and any(alias.name == "research" or alias.name.startswith("research.") for alias in node.names)
        )
    ]

    assert research_imports == []
