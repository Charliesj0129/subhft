"""Tests for startup config snapshot with secret redaction."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from hft_platform.ops.config_snapshot import (
    REDACT_KEYWORDS,
    build_snapshot,
    collect_allowed_env_vars,
    is_secret_var,
)


class TestSecretRedaction:
    def test_password_vars_are_detected(self) -> None:
        assert is_secret_var("HFT_REDIS_PASSWORD")
        assert is_secret_var("HFT_CLICKHOUSE_PASSWORD")
        assert is_secret_var("HFT_FUBON_PASSWORD")

    def test_token_vars_are_detected(self) -> None:
        assert is_secret_var("HFT_TELEGRAM_BOT_TOKEN")

    def test_key_vars_are_detected(self) -> None:
        assert is_secret_var("SHIOAJI_API_KEY")
        assert is_secret_var("SHIOAJI_SECRET_KEY")

    def test_cert_vars_are_detected(self) -> None:
        assert is_secret_var("HFT_FUBON_CERT_PATH")

    def test_safe_vars_are_not_detected(self) -> None:
        assert not is_secret_var("HFT_MODE")
        assert not is_secret_var("HFT_SYMBOLS")
        assert not is_secret_var("HFT_BROKER")
        assert not is_secret_var("HFT_FEATURE_ENGINE_ENABLED")

    def test_collect_excludes_secrets(self) -> None:
        env = {
            "HFT_MODE": "sim",
            "HFT_BROKER": "shioaji",
            "HFT_REDIS_PASSWORD": "super_secret",
            "HFT_TELEGRAM_BOT_TOKEN": "bot12345",
            "SHIOAJI_API_KEY": "abc123",
            "PATH": "/usr/bin",
        }
        with patch.dict(os.environ, env, clear=True):
            result = collect_allowed_env_vars()
        assert result["HFT_MODE"] == "sim"
        assert result["HFT_BROKER"] == "shioaji"
        assert "HFT_REDIS_PASSWORD" not in result
        assert "HFT_TELEGRAM_BOT_TOKEN" not in result
        assert "SHIOAJI_API_KEY" not in result
        assert "PATH" not in result


class TestConfigSnapshot:
    def test_build_snapshot_has_required_fields(self) -> None:
        env = {"HFT_MODE": "sim", "HFT_BROKER": "shioaji"}
        with patch.dict(os.environ, env, clear=True):
            snap = build_snapshot(yaml_paths=[], git_sha="abc123")
        assert snap["git_sha"] == "abc123"
        assert "config_hash" in snap
        assert "env_json" in snap
        assert "boot_ts" in snap
        assert "PASSWORD" not in snap["env_json"]
        assert "TOKEN" not in snap["env_json"]

    def test_build_snapshot_env_json_contains_safe_vars(self) -> None:
        env = {"HFT_MODE": "live", "HFT_BROKER": "fubon", "HFT_FUBON_PASSWORD": "secret"}
        with patch.dict(os.environ, env, clear=True):
            snap = build_snapshot(yaml_paths=[], git_sha="def456")
        assert "HFT_MODE" in snap["env_json"]
        assert "HFT_BROKER" in snap["env_json"]
        assert "HFT_FUBON_PASSWORD" not in snap["env_json"]

    def test_build_snapshot_yaml_json_reflects_paths(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            snap = build_snapshot(yaml_paths=["config/base/main.yaml"], git_sha="xyz")
        assert "config/base/main.yaml" in snap["yaml_json"]

    def test_build_snapshot_uses_provided_git_sha(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            snap = build_snapshot(git_sha="deadbeef")
        assert snap["git_sha"] == "deadbeef"

    def test_build_snapshot_boot_ts_is_positive_int(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            snap = build_snapshot()
        assert isinstance(snap["boot_ts"], int)
        assert snap["boot_ts"] > 0

    def test_redact_keywords_frozenset_contains_expected(self) -> None:
        assert "PASSWORD" in REDACT_KEYWORDS
        assert "SECRET" in REDACT_KEYWORDS
        assert "TOKEN" in REDACT_KEYWORDS
        assert "KEY" in REDACT_KEYWORDS
        assert "CERT" in REDACT_KEYWORDS


class TestWriteSnapshotToClickhouse:
    def test_returns_true_on_success(self) -> None:
        import asyncio

        from hft_platform.ops.config_snapshot import write_snapshot_to_clickhouse

        ch_client = MagicMock()
        snapshot = {
            "boot_ts": 1700000000000,
            "config_hash": "abcdef123456",
            "git_sha": "abc1234",
            "env_json": '{"HFT_MODE": "sim"}',
            "yaml_json": '["config/base/main.yaml"]',
        }
        result = asyncio.run(write_snapshot_to_clickhouse(ch_client, snapshot))
        assert result is True
        ch_client.execute.assert_called_once()

    def test_returns_false_on_ch_error(self) -> None:
        import asyncio

        from hft_platform.ops.config_snapshot import write_snapshot_to_clickhouse

        ch_client = MagicMock()
        ch_client.execute.side_effect = RuntimeError("ClickHouse down")
        snapshot = {
            "boot_ts": 1700000000000,
            "config_hash": "abcdef123456",
            "git_sha": "abc1234",
            "env_json": '{"HFT_MODE": "sim"}',
            "yaml_json": "[]",
        }
        result = asyncio.run(write_snapshot_to_clickhouse(ch_client, snapshot))
        assert result is False
