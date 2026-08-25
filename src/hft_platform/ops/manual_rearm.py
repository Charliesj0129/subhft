from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.ops.platform_degrade_registry import try_force_clear_shared_controller
from hft_platform.ops.runtime_state_store import locked_state, read_state

logger = get_logger("manual_rearm")

DEFAULT_RUNTIME_STATE_PATH = Path("outputs/production_rollout/autonomy/runtime_state.json")


class ManualRearmGate:
    def __init__(self, *, state_path: str | Path | None = None) -> None:
        self.state_path = Path(state_path) if state_path is not None else DEFAULT_RUNTIME_STATE_PATH

    def rearm_strategy(self, strategy_id: str) -> str:
        """Record an operator request to clear one strategy's quarantine.

        This writes a *request*, not the outcome. It used to clear the
        ``manual_rearm_required`` boolean directly, which made the file
        level-triggered: any strategy whose flag read false was treated by the
        engine as freshly authorized. A stale false -- left by an earlier
        re-arm, or by a failed write during a later quarantine -- then
        re-enabled a broken strategy on the next supervisor tick, with no
        operator involved.

        The request is edge-triggered instead, and bound to the exact quarantine
        it intends to clear via ``quarantine_token``. The engine consumes it,
        clears the flag itself, and drops the request, so it can never apply
        twice or apply to a later quarantine.

        The read, the validation and the write are one locked transaction. The
        engine mutates this same document from another process; without the lock
        a write-back here erased a platform latch the engine had just persisted.

        Returns the request id, for correlating with the engine's
        ``strategy_rearm_applied_from_operator_request`` log line.
        """
        request_id = uuid.uuid4().hex
        with locked_state(self.state_path) as state:
            strategies = self._strategies_section(state)
            strategy_state = strategies.get(strategy_id)
            if not isinstance(strategy_state, dict) or not bool(strategy_state.get("manual_rearm_required")):
                raise ValueError(f"strategy {strategy_id!r} does not require manual re-arm")

            token = strategy_state.get("quarantine_token")
            if not isinstance(token, str) or not token:
                # Fail closed. An entry without a token predates this protocol,
                # so the engine cannot verify which quarantine it targets.
                raise ValueError(
                    f"strategy {strategy_id!r} has no quarantine_token; the running engine "
                    "predates the request/ack re-arm protocol. Restart the engine to clear "
                    "the quarantine, or deploy the current build first."
                )

            strategy_state["rearm_request"] = {
                "request_id": request_id,
                "quarantine_token": token,
                "requested_at_ns": timebase.now_ns(),
            }

        logger.warning(
            "strategy_rearm_requested",
            strategy_id=strategy_id,
            request_id=request_id,
            quarantine_token=token,
        )
        return request_id

    def rearm_platform(self) -> None:
        """Persist the manual-rearm flag AND clear the live controller.

        Prior to this fix the rearm wrote a JSON flag that the
        :class:`~hft_platform.ops.platform_degrade.PlatformDegradeController`
        never consulted.  Operators reported reduce_only staying latched
        for hours after they had confirmed conditions were safe.

        We now also call ``force_clear`` on the shared controller (if
        one exists in this process) so the live state mirrors the
        persisted flag.  The persistence step is unconditional so a
        cold-start process picks up the rearmed state.
        """
        self.clear_platform_flag()

        # Best-effort: bridge the live controller through its process-local
        # registry without importing the concrete controller module.
        try:
            if not try_force_clear_shared_controller(reason="manual_rearm_gate"):
                # Different process from the live engine (typical Docker
                # path: `docker compose exec` runs a fresh interpreter).
                # The persisted flag will be honoured on the next engine
                # restart via PlatformDegradeController state restore.
                logger.warning(
                    "manual_rearm_ipc_unreachable",
                    state_path=str(self.state_path),
                    note=(
                        "Persisted to runtime_state.json but live controller "
                        "not in this process. Restart hft-engine to apply."
                    ),
                )
        except Exception as exc:
            # Persistence above is the source of truth; swallow any
            # runtime-coupling error to keep the operator path robust.
            logger.warning("manual_rearm_ipc_error", error=str(exc))

    def clear_platform_flag(self) -> None:
        """Clear the persisted platform manual-rearm flag, lock-free.

        Unlike :meth:`rearm_platform` this does NOT bridge the live controller,
        so it is safe to call from within controller bootstrap — which already
        holds the registry lock — to discard a stale auto-recoverable flag
        without deadlocking.
        """
        with locked_state(self.state_path) as state:
            platform_state = self._platform_section(state)
            platform_state["manual_rearm_required"] = False
            platform_state["reason"] = None
            platform_state["rearm_requested_at"] = timebase.now_s()

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
        return read_state(self.state_path)

    def _write_state(self, state: dict[str, Any]) -> None:
        """Replace the whole document under the shared lock.

        Only for callers that already hold the complete intended state. Prefer
        ``locked_state`` for read-modify-write: taking the lock only around the
        write does not stop a lost update, it only stops a torn file.
        """
        with locked_state(self.state_path) as current:
            current.clear()
            current.update(state)

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
