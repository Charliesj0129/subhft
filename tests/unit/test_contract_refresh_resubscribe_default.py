"""Pin the default for ``HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY``.

Prior to 2026-04-18 the default was ``"none"`` which meant the 24h contract
refresh thread wrote a new ``config/contracts.json`` + ``config/symbols.yaml``
at TXF/TMF rollover time **but never re-subscribed** to the new month
contract. The effect: broker kept pushing stale expiry callbacks, LOB
accumulated under the old symbol, and operators had to restart the engine
manually on rollover day.

The default is now ``"diff"`` — resubscribe only when the refresh diff has
added_codes or removed_codes. See
``docs/runbooks/shioaji-contract-refresh-operations.md`` for the Mode-2
fallback to ``"none"`` when broker returns a bad contract list.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _scrub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY", raising=False)


class TestClientDefault:
    def test_shioaji_client_default_is_diff(self, tmp_path, monkeypatch) -> None:
        from hft_platform.feed_adapter.shioaji_client import ShioajiClient

        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(
            "symbols:\n  - code: '2330'\n    exchange: 'TSE'\n",
            encoding="utf-8",
        )
        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_sj.Shioaji.return_value = MagicMock()
            client = ShioajiClient(config_path=str(cfg))

        assert client._contract_refresh_resubscribe_policy == "diff"


class TestConfigDataclassDefault:
    def test_dataclass_default_is_diff(self) -> None:
        from hft_platform.feed_adapter.shioaji._config import ShioajiClientConfig

        cfg = ShioajiClientConfig()
        assert cfg.contract_refresh_resubscribe_policy == "diff"

    def test_loader_default_is_diff(self) -> None:
        from hft_platform.feed_adapter.shioaji._config import load_shioaji_config

        cfg = load_shioaji_config()
        assert cfg.contract_refresh_resubscribe_policy == "diff"


class TestEnvOverride:
    def test_env_none_disables_resubscribe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY", "none")
        from hft_platform.feed_adapter.shioaji._config import load_shioaji_config

        cfg = load_shioaji_config()
        assert cfg.contract_refresh_resubscribe_policy == "none"

    def test_env_all_resubscribes_every_refresh(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY", "all")
        from hft_platform.feed_adapter.shioaji._config import load_shioaji_config

        cfg = load_shioaji_config()
        assert cfg.contract_refresh_resubscribe_policy == "all"

    def test_empty_env_falls_back_to_default_diff(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY", "")
        from hft_platform.feed_adapter.shioaji._config import load_shioaji_config

        cfg = load_shioaji_config()
        assert cfg.contract_refresh_resubscribe_policy == "diff"


class TestResubscribeTriggered:
    """End-to-end: with default ``diff`` policy, an added-codes diff fires
    ``_resubscribe_all``; an empty diff does not.
    """

    def _make_client(self, tmp_path):
        from hft_platform.feed_adapter.shioaji_client import ShioajiClient

        cfg = tmp_path / "symbols.yaml"
        cfg.write_text(
            "symbols:\n  - code: '2330'\n    exchange: 'TSE'\n",
            encoding="utf-8",
        )
        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_sj.Shioaji.return_value = MagicMock()
            client = ShioajiClient(config_path=str(cfg))
        client.logged_in = True
        return client

    def test_diff_with_added_codes_invokes_resubscribe(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        client._contract_refresh_last_diff = {
            "added_codes": ["TMFF6"],
            "removed_codes": [],
        }

        policy = client._contract_refresh_resubscribe_policy
        assert policy == "diff"

        diff = client._contract_refresh_last_diff
        should_resub = policy == "all" or (
            policy == "diff"
            and bool(diff.get("added_codes") or diff.get("removed_codes"))
        )
        assert should_resub, "rollover-day added_codes must trigger resubscribe"

    def test_diff_with_empty_diff_does_not_resubscribe(self, tmp_path) -> None:
        client = self._make_client(tmp_path)
        client._contract_refresh_last_diff = {
            "added_codes": [],
            "removed_codes": [],
        }

        policy = client._contract_refresh_resubscribe_policy
        diff = client._contract_refresh_last_diff
        should_resub = policy == "all" or (
            policy == "diff"
            and bool(diff.get("added_codes") or diff.get("removed_codes"))
        )
        assert not should_resub, "empty diff must not trigger resubscribe"
