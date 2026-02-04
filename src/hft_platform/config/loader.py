import importlib.util
import os
from copy import deepcopy
from typing import Any, Dict, Tuple

import yaml
from structlog import get_logger

logger = get_logger("config.loader")

DEFAULT_SETTINGS: Dict[str, Any] = {
    "mode": "sim",  # sim | live | replay
    "symbols": ["2330"],
    "strategy": {
        "id": "simple_mm_demo",
        "module": "hft_platform.strategies.simple_mm",
        "class": "SimpleMarketMaker",
        "params": {"subscribe_symbols": ["2330"]},
    },
    "paths": {
        "symbols": "config/symbols.yaml",
        "strategy_limits": "config/base/strategy_limits.yaml",
        "order_adapter": "config/base/order_adapter.yaml",
    },
    "prometheus_port": 9090,
    "replay": {
        "start_date": None,
        "end_date": None,
    },
}


def _load_settings_py(path: str) -> Dict[str, Any]:
    spec = importlib.util.spec_from_file_location("hft_settings", path)
    if not spec or not spec.loader:
        return {}
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Failed to load settings.py", path=path, error=str(exc))
        return {}
    if hasattr(module, "get_settings"):
        try:
            return module.get_settings() or {}
        except Exception as exc:
            logger.error("settings.py#get_settings failed", path=path, error=str(exc))
            return {}
    return {k: getattr(module, k) for k in dir(module) if k.isupper()}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow merge; nested dicts merged recursively."""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _merge(deepcopy(base.get(k, {})), v)
        else:
            base[k] = v
    return base


def _env_overrides() -> Dict[str, Any]:
    env: Dict[str, Any] = {}
    mode = os.getenv("HFT_MODE")
    if mode:
        env["mode"] = mode
    env_name = os.getenv("HFT_ENV")
    if env_name:
        env["env"] = env_name
    symbols = os.getenv("HFT_SYMBOLS")
    if symbols:
        env["symbols"] = symbols.split(",")
    prom_port = os.getenv("HFT_PROM_PORT")
    if prom_port:
        try:
            env["prometheus_port"] = int(prom_port)
        except ValueError:
            pass
    return env


def detect_live_credentials() -> bool:
    return bool(os.getenv("SHIOAJI_API_KEY") and os.getenv("SHIOAJI_SECRET_KEY"))


DEFAULT_YAML_PATH = "config/base/main.yaml"


def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.error("Failed to load yaml", path=path, error=str(exc))
        return {}


def load_settings(cli_overrides: Dict[str, Any] | None = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (settings, applied_defaults) after applying priority chain:
    Base YAML -> Env YAML -> Settings.py -> Env Vars -> CLI
    """
    cli_overrides = cli_overrides or {}

    # 1. Base Settings
    settings = _load_yaml(DEFAULT_YAML_PATH)
    if not settings:
        # Fallback if file missing (e.g. initial setup)
        settings = deepcopy(DEFAULT_SETTINGS)

    # applied_defaults represents the 'base' state before runtime overrides
    applied_defaults = deepcopy(settings)

    # 2. Env Specific Settings (sim/live)
    # Determine mode from Base -> Env Var
    mode = os.getenv("HFT_MODE", settings.get("mode", "sim"))

    env_yaml_path = f"config/env/{mode}/main.yaml"
    if os.path.exists(env_yaml_path):
        env_settings = _load_yaml(env_yaml_path)
        settings = _merge(settings, env_settings)

    # Optional env overlay (dev/staging/prod) without changing mode
    env_name = os.getenv("HFT_ENV")
    if env_name:
        env_overlay_path = f"config/env/{env_name}/main.yaml"
        if os.path.exists(env_overlay_path):
            env_settings = _load_yaml(env_overlay_path)
            settings = _merge(settings, env_settings)
        settings["env"] = env_name

    # 3. Local Developer Overrides (settings.py - Python power)
    settings_py = _load_settings_py("config/settings.py")
    settings = _merge(settings, settings_py)

    # 4. Environment Variables
    env_vars = _env_overrides()
    settings = _merge(settings, env_vars)

    # 5. CLI Overrides
    settings = _merge(settings, cli_overrides)

    # Ensure mode is synced if overridden
    if "mode" in settings:
        settings["mode"] = mode if os.getenv("HFT_MODE") else settings["mode"]

    return settings, applied_defaults


def summarize_settings(settings: Dict[str, Any], downgraded_mode: str | None = None) -> str:
    lines = []
    lines.append(f"mode={settings.get('mode')}{' (downgraded to sim)' if downgraded_mode else ''}")
    if settings.get("env"):
        lines.append(f"env={settings.get('env')}")
    lines.append(f"symbols={','.join(settings.get('symbols', []))}")
    strat = settings.get("strategy", {})
    lines.append(f"strategy={strat.get('id')} ({strat.get('module')}.{strat.get('class')})")
    paths = settings.get("paths", {})
    lines.append(f"paths[symbols]={paths.get('symbols')} paths[strategy_limits]={paths.get('strategy_limits')}")
    lines.append(f"prometheus_port={settings.get('prometheus_port')}")
    return " | ".join(lines)
