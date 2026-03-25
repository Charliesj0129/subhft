from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_RUNTIME_STATE_PATH = Path("outputs/production_rollout/autonomy/runtime_state.json")


class ManualRearmGate:
    def __init__(self, *, state_path: str | Path | None = None) -> None:
        self.state_path = Path(state_path) if state_path is not None else DEFAULT_RUNTIME_STATE_PATH

    def rearm_strategy(self, strategy_id: str) -> None:
        state = self._load_state()
        strategies = self._strategies_section(state)
        strategy_state = strategies.get(strategy_id)
        if not isinstance(strategy_state, dict) or not bool(strategy_state.get("manual_rearm_required")):
            raise ValueError(f"strategy {strategy_id!r} does not require manual re-arm")

        strategy_state["manual_rearm_required"] = False
        strategy_state["reason"] = None
        self._write_state(state)

    def rearm_platform(self) -> None:
        state = self._load_state()
        platform_state = self._platform_section(state)
        platform_state["manual_rearm_required"] = False
        platform_state["reason"] = None
        self._write_state(state)

    def requires_manual_rearm(self, scope: str, *, strategy_id: str | None = None) -> bool:
        state = self._load_state()
        normalized_scope = scope.strip().lower()
        if normalized_scope == "platform":
            return bool(self._platform_section(state).get("manual_rearm_required"))
        if normalized_scope == "strategy":
            strategies = self._strategies_section(state)
            if strategy_id is not None:
                strategy_state = strategies.get(strategy_id)
                return bool(isinstance(strategy_state, dict) and strategy_state.get("manual_rearm_required"))
            return any(
                bool(isinstance(strategy_state, dict) and strategy_state.get("manual_rearm_required"))
                for strategy_state in strategies.values()
            )
        raise ValueError(f"unsupported scope: {scope}")

    def snapshot(self) -> dict[str, Any]:
        return self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._default_state()

        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return self._default_state()

        state = dict(raw)
        self._platform_section(state)
        self._strategies_section(state)
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        tmp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.state_path)

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {},
        }

    @staticmethod
    def _platform_section(state: dict[str, Any]) -> dict[str, Any]:
        platform_state = state.get("platform")
        if not isinstance(platform_state, dict):
            platform_state = {"manual_rearm_required": False, "reason": None}
            state["platform"] = platform_state
        platform_state.setdefault("manual_rearm_required", False)
        platform_state.setdefault("reason", None)
        return platform_state

    @staticmethod
    def _strategies_section(state: dict[str, Any]) -> dict[str, Any]:
        strategies = state.get("strategies")
        if not isinstance(strategies, dict):
            strategies = {}
            state["strategies"] = strategies
        return strategies
