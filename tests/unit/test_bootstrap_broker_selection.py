"""Tests for HFT_BROKER env var based broker selection in SystemBootstrapper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.services.bootstrap import _VALID_BROKERS, SystemBootstrapper


class TestResolveBrokerId:
    """Unit tests for SystemBootstrapper._resolve_broker_id()."""

    def test_default_broker_is_shioaji(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When HFT_BROKER is unset, default to 'shioaji'."""
        monkeypatch.delenv("HFT_BROKER", raising=False)
        assert SystemBootstrapper._resolve_broker_id() == "shioaji"

    def test_explicit_shioaji(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_BROKER", "shioaji")
        assert SystemBootstrapper._resolve_broker_id() == "shioaji"

    def test_fubon_broker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_BROKER", "fubon")
        assert SystemBootstrapper._resolve_broker_id() == "fubon"

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_BROKER", "Fubon")
        assert SystemBootstrapper._resolve_broker_id() == "fubon"

    def test_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_BROKER", "  shioaji  ")
        assert SystemBootstrapper._resolve_broker_id() == "shioaji"

    def test_invalid_broker_raises_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_BROKER", "invalid_broker")
        with pytest.raises(ValueError, match="Unknown HFT_BROKER"):
            SystemBootstrapper._resolve_broker_id()

    def test_empty_string_raises_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_BROKER", "")
        with pytest.raises(ValueError, match="Unknown HFT_BROKER"):
            SystemBootstrapper._resolve_broker_id()


class TestBuildBrokerClientsSelection:
    """Tests for broker_id dispatch inside _build_broker_clients()."""

    @pytest.fixture()
    def bootstrapper(self) -> SystemBootstrapper:
        return SystemBootstrapper(settings={})

    def test_shioaji_path_uses_shioaji_facade(self, bootstrapper: SystemBootstrapper) -> None:
        """broker_id='shioaji' instantiates ShioajiClientFacade."""
        with patch("hft_platform.services.bootstrap.ShioajiClientFacade") as mock_facade:
            mock_facade.return_value = MagicMock()
            md, order = bootstrapper._build_broker_clients(
                role="engine",
                symbols_path="config/symbols.yaml",
                base_shioaji_cfg={},
                broker_id="shioaji",
            )
            assert mock_facade.call_count == 2

    def test_fubon_path_lazy_imports_fubon_facade(self, bootstrapper: SystemBootstrapper) -> None:
        """broker_id='fubon' lazy-imports and instantiates FubonClientFacade."""
        mock_fubon_facade_cls = MagicMock()
        mock_fubon_facade_cls.return_value = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "hft_platform.feed_adapter.fubon": MagicMock(),
                "hft_platform.feed_adapter.fubon.facade": MagicMock(FubonClientFacade=mock_fubon_facade_cls),
            },
        ):
            with patch("hft_platform.services.bootstrap.ShioajiClientFacade"):
                import hft_platform.services.bootstrap as bs_mod

                with patch.object(
                    bs_mod,
                    "__import__",
                    create=True,
                ):
                    fake_module = MagicMock()
                    fake_module.FubonClientFacade = mock_fubon_facade_cls
                    with patch.dict("sys.modules", {"hft_platform.feed_adapter.fubon.facade": fake_module}):
                        md, order = bootstrapper._build_broker_clients(
                            role="engine",
                            symbols_path="config/symbols.yaml",
                            base_shioaji_cfg={},
                            broker_id="fubon",
                        )
                        assert mock_fubon_facade_cls.call_count == 2

    def test_non_engine_role_returns_noop_clients(self, bootstrapper: SystemBootstrapper) -> None:
        """Non-engine roles get no-op clients regardless of broker_id."""
        md, order = bootstrapper._build_broker_clients(
            role="maintenance",
            symbols_path="config/symbols.yaml",
            base_shioaji_cfg={},
            broker_id="shioaji",
        )
        # _RoleGuardedNoopClient has runtime_role attribute
        assert hasattr(md, "runtime_role")
        assert md.runtime_role == "maintenance"


class TestValidBrokersConstant:
    """Validate the _VALID_BROKERS constant."""

    def test_contains_shioaji(self) -> None:
        assert "shioaji" in _VALID_BROKERS

    def test_contains_fubon(self) -> None:
        assert "fubon" in _VALID_BROKERS

    def test_is_frozenset(self) -> None:
        assert isinstance(_VALID_BROKERS, frozenset)
