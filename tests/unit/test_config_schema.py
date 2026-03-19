import pytest

from hft_platform.config import loader
from hft_platform.config.schema import (
    ConfigValidationError,
    HftConfig,
    validate_config,
    validate_config_or_exit,
)

# ---------------------------------------------------------------------------
# Minimal valid config fixture
# ---------------------------------------------------------------------------


def _minimal_config() -> dict:
    return {
        "mode": "sim",
        "symbols": ["2330"],
        "broker": "shioaji",
        "strategy": {
            "id": "demo",
            "module": "hft_platform.strategies.demo",
            "class": "Demo",
        },
        "paths": {
            "symbols": "config/base/symbols.yaml",
            "strategy_limits": "config/base/strategy_limits.yaml",
            "order_adapter": "config/base/order_adapter.yaml",
        },
        "prometheus_port": 9090,
        "replay": {"start_date": None, "end_date": None},
    }


# ===================================================================
# Structural validation
# ===================================================================


class TestConfigSchemaStructural:
    """Structural (msgspec.convert) validation tests."""

    def test_valid_full_config(self):
        cfg = validate_config(_minimal_config())
        assert isinstance(cfg, HftConfig)
        assert cfg.mode == "sim"
        assert cfg.symbols == ["2330"]
        assert cfg.broker == "shioaji"
        assert cfg.strategy is not None
        assert cfg.strategy.id == "demo"
        assert cfg.prometheus_port == 9090

    def test_valid_minimal_config(self):
        """Only mode + symbols required to be meaningful."""
        cfg = validate_config({"mode": "sim", "symbols": ["2330"]})
        assert cfg.mode == "sim"
        assert cfg.strategy is None

    def test_defaults_applied(self):
        cfg = validate_config({})
        assert cfg.mode == "sim"
        assert cfg.symbols == ["2330"]
        assert cfg.broker == "shioaji"
        assert cfg.prometheus_port == 9090

    def test_invalid_symbols_type(self):
        with pytest.raises(ConfigValidationError, match="structure invalid"):
            validate_config({"symbols": "not_a_list"})

    def test_invalid_prometheus_port_type(self):
        with pytest.raises(ConfigValidationError, match="structure invalid"):
            validate_config({"prometheus_port": "not_an_int"})

    def test_invalid_strategy_missing_id(self):
        with pytest.raises(ConfigValidationError, match="structure invalid"):
            validate_config({"strategy": {"module": 123}})

    def test_extra_keys_ignored(self):
        """Unknown top-level keys should not cause failure."""
        d = _minimal_config()
        d["some_unknown_section"] = {"foo": "bar"}
        cfg = validate_config(d)
        assert cfg.mode == "sim"

    def test_nested_extra_strategy_params(self):
        d = _minimal_config()
        d["strategy"]["params"] = {"custom_key": 42}
        cfg = validate_config(d)
        assert cfg.strategy is not None
        assert cfg.strategy.params == {"custom_key": 42}


# ===================================================================
# Semantic validation
# ===================================================================


class TestConfigSchemaSemantic:
    """Semantic (business rule) validation tests."""

    def test_invalid_mode(self):
        with pytest.raises(ConfigValidationError, match="mode must be one of"):
            validate_config({"mode": "invalid_mode"})

    def test_invalid_broker(self):
        with pytest.raises(ConfigValidationError, match="broker must be one of"):
            validate_config({"broker": "unknown_broker"})

    def test_empty_symbols(self):
        with pytest.raises(ConfigValidationError, match="symbols list must not be empty"):
            validate_config({"symbols": []})

    def test_symbol_not_string(self):
        with pytest.raises(ConfigValidationError, match="non-empty string"):
            validate_config({"symbols": [""]})

    def test_prometheus_port_out_of_range_low(self):
        with pytest.raises(ConfigValidationError, match="prometheus_port"):
            validate_config({"prometheus_port": 0})

    def test_prometheus_port_out_of_range_high(self):
        with pytest.raises(ConfigValidationError, match="prometheus_port"):
            validate_config({"prometheus_port": 70000})

    def test_strategy_empty_id(self):
        with pytest.raises(ConfigValidationError, match="strategy.id"):
            validate_config(
                {
                    "strategy": {"id": "", "module": "m", "class": "C"},
                }
            )

    def test_strategy_empty_module(self):
        with pytest.raises(ConfigValidationError, match="strategy.module"):
            validate_config(
                {
                    "strategy": {"id": "x", "module": "", "class": "C"},
                }
            )

    def test_valid_modes(self):
        for mode in ("sim", "live", "replay"):
            cfg = validate_config({"mode": mode})
            assert cfg.mode == mode

    def test_valid_brokers(self):
        for broker in ("shioaji", "fubon"):
            cfg = validate_config({"broker": broker})
            assert cfg.broker == broker


# ===================================================================
# validate_config_or_exit
# ===================================================================


class TestValidateConfigOrExit:
    """Tests for the exit-on-failure wrapper."""

    def test_exits_on_invalid_config(self):
        with pytest.raises(SystemExit) as exc_info:
            validate_config_or_exit({"mode": "bad"})
        assert exc_info.value.code == 1

    def test_skip_via_env(self, monkeypatch):
        monkeypatch.setenv("HFT_SKIP_CONFIG_VALIDATION", "1")
        result = validate_config_or_exit({"mode": "bad"})
        assert result is None

    def test_returns_config_on_success(self, monkeypatch):
        monkeypatch.delenv("HFT_SKIP_CONFIG_VALIDATION", raising=False)
        cfg = validate_config_or_exit(_minimal_config())
        assert isinstance(cfg, HftConfig)


# ===================================================================
# Integration with loader.load_settings
# ===================================================================


class TestLoaderIntegration:
    """Ensure loader.load_settings calls validation."""

    def test_load_settings_validates(self, tmp_path, monkeypatch):
        """Valid config passes without error."""
        base = tmp_path / "config" / "base" / "main.yaml"
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(
            textwrap.dedent("""\
            mode: sim
            symbols: ["2330"]
            broker: shioaji
            strategy:
              id: demo
              module: m
              class: C
            prometheus_port: 9090
        """)
        )

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(loader, "DEFAULT_YAML_PATH", "config/base/main.yaml")
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.delenv("HFT_SKIP_CONFIG_VALIDATION", raising=False)

        settings, defaults = loader.load_settings()
        assert settings["mode"] == "sim"

    def test_load_settings_exits_on_bad_config(self, tmp_path, monkeypatch):
        """Invalid config causes SystemExit(1)."""
        base = tmp_path / "config" / "base" / "main.yaml"
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(
            textwrap.dedent("""\
            mode: bad_mode
            symbols: ["2330"]
        """)
        )

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(loader, "DEFAULT_YAML_PATH", "config/base/main.yaml")
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.delenv("HFT_SKIP_CONFIG_VALIDATION", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            loader.load_settings()
        assert exc_info.value.code == 1

    def test_load_settings_skip_via_env(self, tmp_path, monkeypatch):
        """HFT_SKIP_CONFIG_VALIDATION=1 bypasses validation."""
        base = tmp_path / "config" / "base" / "main.yaml"
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(
            textwrap.dedent("""\
            mode: bad_mode
            symbols: ["2330"]
        """)
        )

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(loader, "DEFAULT_YAML_PATH", "config/base/main.yaml")
        monkeypatch.setenv("HFT_SKIP_CONFIG_VALIDATION", "1")
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)

        # Should not raise despite bad mode
        settings, _ = loader.load_settings()
        assert settings["mode"] == "bad_mode"

    def test_load_settings_skip_via_cli_override(self, tmp_path, monkeypatch):
        """skip_config_validation CLI override bypasses validation."""
        base = tmp_path / "config" / "base" / "main.yaml"
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(
            textwrap.dedent("""\
            mode: bad_mode
            symbols: ["2330"]
        """)
        )

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(loader, "DEFAULT_YAML_PATH", "config/base/main.yaml")
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.delenv("HFT_SKIP_CONFIG_VALIDATION", raising=False)

        settings, _ = loader.load_settings(cli_overrides={"skip_config_validation": True})
        assert settings["mode"] == "bad_mode"
