import importlib.util
import os
from copy import deepcopy
from typing import Any, Dict, Tuple

import yaml
from structlog import get_logger

logger = get_logger("config.loader")

LOOPS_DIR = "config/loops"
STRATEGIES_YAML_PATH = "config/live/strategies.yaml"


class LoopBindingError(RuntimeError):
    """Raised when loop_id resolution or strategy-enabled assertion fails."""


def resolve_active_strategy(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Return the canonical strategy block respecting loop_id binding.

    Loop_v1 callers MUST go through this helper instead of reading
    ``settings["strategy"]`` directly. After loader binds a loop_id, both
    paths are equivalent — but the helper keeps the contract explicit.
    """
    return dict(settings.get("strategy") or {})


def _assert_strategy_enabled(strategy_id: str, strategies_yaml: str = STRATEGIES_YAML_PATH) -> None:
    """Refuse to start if the loop's strategy_id is not `enabled: true`.

    A loop binding that points to a disabled strategy guarantees a silent
    failure at instantiate time — fail fast at config load instead.
    """
    try:
        with open(strategies_yaml, "r") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError as exc:
        raise LoopBindingError(
            f"strategies registry not found at {strategies_yaml!r}; cannot verify "
            f"strategy_id={strategy_id!r}"
        ) from exc

    for entry in data.get("strategies", []) or []:
        if entry.get("id") == strategy_id:
            if not entry.get("enabled", False):
                raise LoopBindingError(
                    f"strategy_id={strategy_id!r} bound by loop, but disabled in "
                    f"{strategies_yaml}"
                )
            return
    raise LoopBindingError(
        f"strategy_id={strategy_id!r} bound by loop, but not present in {strategies_yaml}"
    )


def _bind_loop(settings: Dict[str, Any]) -> Dict[str, Any]:
    """If settings.loop_id is set, merge config/loops/<loop_id>.yaml on top.

    The loop YAML overrides ``strategy`` and ``broker`` (single source of
    truth). Other loop-only fields (symbol_family, risk_profile,
    recorder_mode, strict_equity, trace_policy, intent_recorder_required)
    are not part of HftConfig yet; downstream steps (L4-L11) read them
    directly from the loop YAML on disk.
    """
    loop_id = settings.get("loop_id")
    if not loop_id:
        return settings

    loop_path = os.path.join(LOOPS_DIR, f"{loop_id}.yaml")
    if not os.path.exists(loop_path):
        raise LoopBindingError(f"loop_id={loop_id!r} but loop file not found: {loop_path}")

    loop_cfg = _load_yaml(loop_path)
    if not loop_cfg:
        raise LoopBindingError(f"loop file is empty or unreadable: {loop_path}")

    if loop_cfg.get("loop_id") != loop_id:
        raise LoopBindingError(
            f"loop file {loop_path} has loop_id={loop_cfg.get('loop_id')!r}, "
            f"settings expected {loop_id!r}"
        )

    if "strategy" in loop_cfg:
        settings["strategy"] = deepcopy(loop_cfg["strategy"])
    if "broker" in loop_cfg:
        settings["broker"] = loop_cfg["broker"]

    strategy_id = (settings.get("strategy") or {}).get("id")
    if not strategy_id:
        raise LoopBindingError(f"loop {loop_id!r} did not provide strategy.id")
    _assert_strategy_enabled(strategy_id)

    return settings

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
        else:
            # Bug #33 root cause: silently skipping a missing overlay meant
            # typos (e.g. HFT_ENV=prrod) fell back to base without warning,
            # so operators never discovered their prod risk limits weren't loaded.
            logger.warning(
                "env_overlay_not_found",
                env=env_name,
                expected_path=env_overlay_path,
            )
        settings["env"] = env_name

    # 3. Local Developer Overrides (settings.py - Python power)
    settings_py = _load_settings_py("config/settings.py")
    settings = _merge(settings, settings_py)

    # 4. Environment Variables
    env_vars = _env_overrides()
    settings = _merge(settings, env_vars)

    # 5. CLI Overrides
    settings = _merge(settings, cli_overrides)

    # Ensure mode reflects CLI override (highest priority) or env var
    if "mode" in cli_overrides:
        settings["mode"] = cli_overrides["mode"]
    elif os.getenv("HFT_MODE"):
        settings["mode"] = mode

    # 5b. Loop binding (loop_v1). HFT_LOOP env var also routes here so Docker
    # entrypoints can opt in without YAML edits. Must run BEFORE validation
    # so strict mode sees the resolved strategy block.
    env_loop = os.getenv("HFT_LOOP")
    if env_loop and not settings.get("loop_id"):
        settings["loop_id"] = env_loop
    settings = _bind_loop(settings)

    # 6. Validate merged config (fail-fast unless bypassed). Strict mode is
    # forced whenever a loop_id is bound or HFT_CONFIG_STRICT=1.
    skip_validation = (
        cli_overrides.get("skip_config_validation", False) or os.getenv("HFT_SKIP_CONFIG_VALIDATION", "0") == "1"
    )
    strict_mode = bool(settings.get("loop_id")) or os.getenv("HFT_CONFIG_STRICT", "0") == "1"
    if not skip_validation:
        from hft_platform.config.schema import validate_config_or_exit

        validate_config_or_exit(settings, strict=strict_mode)
    else:
        logger.warning("config_validation_skipped")

    return settings, applied_defaults


def summarize_settings(settings: Dict[str, Any], downgraded_mode: str | None = None) -> str:
    lines = []
    lines.append(f"mode={settings.get('mode')}{' (downgraded to sim)' if downgraded_mode else ''}")
    if settings.get("loop_id"):
        lines.append(f"loop={settings.get('loop_id')}")
    if settings.get("env"):
        lines.append(f"env={settings.get('env')}")
    lines.append(f"symbols={','.join(settings.get('symbols', []))}")
    strat = resolve_active_strategy(settings)
    lines.append(f"strategy={strat.get('id')} ({strat.get('module')}.{strat.get('class')})")
    paths = settings.get("paths", {})
    lines.append(f"paths[symbols]={paths.get('symbols')} paths[strategy_limits]={paths.get('strategy_limits')}")
    lines.append(f"prometheus_port={settings.get('prometheus_port')}")
    return " | ".join(lines)
