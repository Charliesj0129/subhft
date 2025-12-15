import importlib.util
import json
import os
from copy import deepcopy
from typing import Any, Dict, Tuple

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
        "strategy_limits": "config/strategy_limits.yaml",
        "order_adapter": "config/order_adapter.yaml",
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


def _load_settings_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r") as f:
            return json.load(f) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Failed to load settings.json", path=path, error=str(exc))
        return {}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow merge; nested dicts merged recursively."""
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _merge(deepcopy(base.get(k, {})), v)
        else:
            base[k] = v
    return base


def _env_overrides() -> Dict[str, Any]:
    env = {}
    if os.getenv("HFT_MODE"):
        env["mode"] = os.getenv("HFT_MODE")
    if os.getenv("HFT_SYMBOLS"):
        env["symbols"] = os.getenv("HFT_SYMBOLS").split(",")
    if os.getenv("HFT_PROM_PORT"):
        try:
            env["prometheus_port"] = int(os.getenv("HFT_PROM_PORT"))
        except ValueError:
            pass
    return env


def detect_live_credentials() -> bool:
    return bool(os.getenv("SHIOAJI_PERSON_ID") and os.getenv("SHIOAJI_PASSWORD"))


def load_settings(cli_overrides: Dict[str, Any] | None = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (settings, applied_defaults) after applying priority chain."""
    cli_overrides = cli_overrides or {}
    applied_defaults: Dict[str, Any] = {}

    settings = deepcopy(DEFAULT_SETTINGS)
    applied_defaults = deepcopy(DEFAULT_SETTINGS)

    settings_json = _load_settings_json("config/settings.json")
    settings = _merge(settings, settings_json)

    settings_py = _load_settings_py("config/settings.py")
    settings = _merge(settings, settings_py)

    env = _env_overrides()
    settings = _merge(settings, env)

    settings = _merge(settings, cli_overrides)

    return settings, applied_defaults


def summarize_settings(settings: Dict[str, Any], downgraded_mode: str | None = None) -> str:
    lines = []
    lines.append(f"mode={settings.get('mode')}{' (downgraded to sim)' if downgraded_mode else ''}")
    lines.append(f"symbols={','.join(settings.get('symbols', []))}")
    strat = settings.get("strategy", {})
    lines.append(f"strategy={strat.get('id')} ({strat.get('module')}.{strat.get('class')})")
    paths = settings.get("paths", {})
    lines.append(f"paths[symbols]={paths.get('symbols')} paths[strategy_limits]={paths.get('strategy_limits')}")
    lines.append(f"prometheus_port={settings.get('prometheus_port')}")
    return " | ".join(lines)
