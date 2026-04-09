"""Tests for hot-reload domain validation (C-04)."""

from __future__ import annotations

from typing import Any, Dict

import yaml


def _write_yaml(path: Any, data: Dict[str, Any]) -> None:
    with open(str(path), "w") as f:
        yaml.safe_dump(data, f)


def test_hot_reload_rejects_invalid_domain_values(tmp_path: Any) -> None:
    """A structurally valid YAML with bad domain values should be rejected."""
    from hft_platform.config.hot_reload import ConfigWatcher

    config_file = tmp_path / "limits.yaml"
    # Write valid initial config
    _write_yaml(
        config_file,
        {
            "mode": "sim",
            "symbols": ["2330"],
        },
    )

    watcher = ConfigWatcher(str(config_file))
    # Initial load
    watcher._load_and_store()
    initial_config = watcher.current_config
    assert initial_config is not None
    assert initial_config["symbols"] == ["2330"]

    # Write invalid config: empty symbols list fails _semantic_checks
    _write_yaml(
        config_file,
        {
            "mode": "sim",
            "symbols": [],
        },
    )

    # Reload should reject and keep previous config
    watcher._load_and_notify(watcher._last_mtime + 10.0)
    assert watcher.current_config == initial_config, "Config should not have changed after domain-invalid reload"


def test_hot_reload_rejects_invalid_mode(tmp_path: Any) -> None:
    """A config with an unrecognised mode should be rejected."""
    from hft_platform.config.hot_reload import ConfigWatcher

    config_file = tmp_path / "limits.yaml"
    _write_yaml(
        config_file,
        {
            "mode": "sim",
            "symbols": ["2330"],
        },
    )

    watcher = ConfigWatcher(str(config_file))
    watcher._load_and_store()
    initial_config = watcher.current_config

    # Write config with invalid mode
    _write_yaml(
        config_file,
        {
            "mode": "bogus",
            "symbols": ["2330"],
        },
    )

    watcher._load_and_notify(watcher._last_mtime + 10.0)
    assert watcher.current_config == initial_config, "Config should not have changed after invalid mode reload"


def test_hot_reload_accepts_valid_domain_change(tmp_path: Any) -> None:
    """A valid domain change should be accepted and callbacks fired."""
    from hft_platform.config.hot_reload import ConfigWatcher

    config_file = tmp_path / "limits.yaml"
    _write_yaml(
        config_file,
        {
            "mode": "sim",
            "symbols": ["2330"],
        },
    )

    watcher = ConfigWatcher(str(config_file))
    watcher._load_and_store()

    received = []
    watcher.register(lambda c: received.append(c))

    # Write valid updated config
    _write_yaml(
        config_file,
        {
            "mode": "sim",
            "symbols": ["2330", "2317"],
        },
    )

    watcher._load_and_notify(watcher._last_mtime + 10.0)
    assert watcher.current_config["symbols"] == ["2330", "2317"]
    assert len(received) == 1
    assert received[0]["symbols"] == ["2330", "2317"]


def test_hot_reload_validation_does_not_block_callbacks_on_valid_reload(tmp_path: Any) -> None:
    """Callbacks must still be fired when validation passes."""
    from hft_platform.config.hot_reload import ConfigWatcher

    config_file = tmp_path / "limits.yaml"
    _write_yaml(config_file, {"mode": "sim", "symbols": ["2330"]})

    watcher = ConfigWatcher(str(config_file))
    watcher._load_and_store()

    fired = []
    watcher.register(lambda c: fired.append(True))

    _write_yaml(config_file, {"mode": "live", "symbols": ["2330"]})
    watcher._load_and_notify(watcher._last_mtime + 10.0)

    assert len(fired) == 1, "Callback must fire for valid domain reload"
