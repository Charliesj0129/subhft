from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from zoneinfo import ZoneInfo

import structlog

from hft_platform.core import timebase
from hft_platform.ops.runtime_state_store import locked_state

logger = structlog.get_logger(__name__)

_TZ_TAIPEI = ZoneInfo("Asia/Taipei")

DEFAULT_AUTONOMY_EVIDENCE_DIR = Path("outputs/production_rollout/autonomy")

_shared_writer: "AutonomyEvidenceWriter | None" = None
_shared_writer_lock = Lock()


class AutonomyEvidenceWriter:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else DEFAULT_AUTONOMY_EVIDENCE_DIR
        self._trading_date: date | None = None
        self._on_transition_callbacks: list[Callable[[dict[str, Any]], None]] = []
        # Guards the multi-file write in record_transition. Since
        # ``quarantine_async`` moved the durable write off the event loop this
        # is load-bearing rather than insurance: the shared singleton writer is
        # now reached from the loop, from executor threads, and from the CLI.
        self._transition_lock = Lock()

    def set_trading_date(self, d: date) -> None:
        """Override the trading date used for session directory naming."""
        self._trading_date = d

    def on_transition(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback invoked after each ``record_transition``."""
        self._on_transition_callbacks.append(callback)

    @property
    def session_dir(self) -> Path:
        if self._trading_date is not None:
            return self.base_dir / self._trading_date.strftime("%Y%m%d")
        return self.base_dir / datetime.now(tz=_TZ_TAIPEI).strftime("%Y%m%d")

    def record_transition(
        self,
        *,
        scope: str,
        mode: str,
        reason: str,
        manual_rearm_required: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._transition_lock:
            record = {
                "ts_ns": timebase.now_ns(),
                "scope": str(scope),
                "mode": str(mode),
                "reason": str(reason),
                "manual_rearm_required": bool(manual_rearm_required),
                "metadata": dict(metadata or {}),
            }
            self._append_jsonl("state_timeline.jsonl", record)
            self._append_markdown(
                "alert_digest.md",
                f"- `{record['scope']}` -> `{record['mode']}` reason=`{record['reason']}`",
            )
            self._update_scope_summary(record)
            self._update_summary(record)
            if manual_rearm_required:
                self.record_manual_rearm_requirement(
                    scope=scope,
                    reason=reason,
                    metadata=metadata,
                )
            elif self._is_strategy_rearm_ack(record):
                # A recovery transition must reach runtime_state.json too -- it used
                # not to, which is why nothing ever wrote the flag back to false and
                # a stale false was indistinguishable from a fresh authorization.
                #
                # But ONLY an explicit, correlated strategy re-arm may do so. Most
                # false transitions are not recoveries at all: HFTSystem.run()
                # records `system_start` with manual_rearm_required=False on every
                # boot, and projecting that would clear a genuine platform latch
                # that no operator had re-armed -- turning a restart into an
                # unauthorised HALT release. Fail-closed means a transition that
                # cannot prove it is a recovery must not clear anything.
                self._update_runtime_state(record)
            for cb in self._on_transition_callbacks:
                try:
                    cb(record)
                except Exception:
                    pass
            return record

    def record_manual_rearm_requirement(
        self,
        *,
        scope: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "ts_ns": timebase.now_ns(),
            "scope": str(scope),
            "reason": str(reason),
            "metadata": dict(metadata or {}),
        }
        self._append_markdown(
            "manual_rearm_requirements.md",
            f"- `{record['scope']}` reason=`{record['reason']}` "
            f"metadata={json.dumps(record['metadata'], ensure_ascii=False)}",
        )
        self._update_runtime_state(record)

    @staticmethod
    def _is_strategy_rearm_ack(record: dict[str, Any]) -> bool:
        """True only for a strategy recovery that names the request it consumed."""
        if record.get("scope") != "strategy":
            return False
        if record.get("reason") != "manual_rearm":
            return False
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            return False
        request_id = metadata.get("request_id")
        strategy_id = metadata.get("strategy_id")
        return bool(isinstance(request_id, str) and request_id and isinstance(strategy_id, str) and strategy_id)

    def _update_scope_summary(self, record: dict[str, Any]) -> None:
        filename = "platform_degrade.json" if record["scope"] == "platform" else "strategy_quarantine.json"
        path = self._ensure_session_dir() / filename
        payload: dict[str, Any]
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
        else:
            payload = {}

        events = payload.get("events")
        if not isinstance(events, list):
            events = []
        events.append(record)
        payload["events"] = events
        payload["latest"] = record
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _update_summary(self, record: dict[str, Any]) -> None:
        path = self._ensure_session_dir() / "summary.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
        else:
            payload = {}

        transition_count = int(payload.get("transition_count", 0) or 0) + 1
        payload["transition_count"] = transition_count
        payload["last_transition"] = record
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def _append_jsonl(self, filename: str, record: dict[str, Any]) -> None:
        path = self._ensure_session_dir() / filename
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _update_runtime_state(self, record: dict[str, Any]) -> None:
        """Project one transition onto the operator-visible runtime state.

        Three properties this file depends on:

        * **The record's own flag decides.** This used to hardcode ``True``,
          which is why a recovery could never clear anything.
        * **The whole read-modify-write is serialized.** The operator CLI
          mutates the same document from another process; without a shared lock
          its write-back silently erased a platform latch this method had just
          persisted. See ``runtime_state_store``.
        * **The write is atomic and writer-unique.**
        """
        required = bool(record.get("manual_rearm_required", True))
        metadata = record.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        scope = record["scope"]

        with locked_state(self.base_dir / "runtime_state.json") as state:
            if scope == "platform":
                # Only reachable with required=True; see _is_strategy_rearm_ack.
                platform = state["platform"]
                platform["manual_rearm_required"] = required
                platform["reason"] = record["reason"] if required else None
            elif scope == "strategy":
                strategy_id = str(metadata.get("strategy_id") or "").strip()
                if strategy_id:
                    strategies = state["strategies"]
                    if required:
                        entry: dict[str, Any] = {
                            "manual_rearm_required": True,
                            "reason": record["reason"],
                        }
                        token = metadata.get("quarantine_token")
                        if isinstance(token, str) and token:
                            # The operator's re-arm must name this exact token,
                            # so a request aimed at an earlier quarantine cannot
                            # clear it.
                            entry["quarantine_token"] = token
                        strategies[strategy_id] = entry
                    else:
                        # Recovery: drop the flag AND the consumed request, so
                        # the same request can never be replayed against a later
                        # quarantine of the same strategy.
                        #
                        # Clear only the latch this acknowledgement names. The
                        # governor's token check guards one process's in-memory
                        # state, and session ownership is advisory, so two
                        # engines can overlap: an acknowledgement minted for
                        # token T1 must not erase a T2 quarantine another
                        # process persisted in between -- the next restart would
                        # then hydrate nothing and resume a strategy no operator
                        # authorized. The ``required`` branch above records the
                        # token precisely so this branch can compare it, under
                        # the same lock that is about to write.
                        acked = metadata.get("quarantine_token")
                        acked = acked if isinstance(acked, str) and acked else None
                        persisted_entry = strategies.get(strategy_id)
                        persisted = (
                            persisted_entry.get("quarantine_token") if isinstance(persisted_entry, dict) else None
                        )
                        persisted = persisted if isinstance(persisted, str) and persisted else None
                        # A latch with no persisted token predates tokens; refusing
                        # there would make it permanently unclearable.
                        if persisted is not None and acked != persisted:
                            logger.warning(
                                "stale_strategy_rearm_ack_ignored",
                                strategy_id=strategy_id,
                                acknowledged_token=acked,
                                persisted_token=persisted,
                            )
                        else:
                            strategies[strategy_id] = {
                                "manual_rearm_required": False,
                                "reason": None,
                            }

    def _append_markdown(self, filename: str, line: str) -> None:
        path = self._ensure_session_dir() / filename
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip() + "\n")

    def write_daily_summary(self, summary: dict[str, Any]) -> Path:
        """Write a daily summary JSON file to the session directory.

        Args:
            summary: Arbitrary summary payload (e.g. PnL, fill counts).

        Returns:
            Path to the written file.
        """
        path = self._ensure_session_dir() / "daily_summary.json"
        path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        return path

    def _ensure_session_dir(self) -> Path:
        path = self.session_dir
        path.mkdir(parents=True, exist_ok=True)
        return path


def get_shared_autonomy_evidence_writer(*, base_dir: str | Path | None = None) -> AutonomyEvidenceWriter:
    global _shared_writer
    with _shared_writer_lock:
        if _shared_writer is None:
            _shared_writer = AutonomyEvidenceWriter(base_dir=base_dir)
        elif base_dir is not None:
            _shared_writer.base_dir = Path(base_dir)
        return _shared_writer


def reset_shared_autonomy_evidence_writer() -> None:
    global _shared_writer
    with _shared_writer_lock:
        _shared_writer = None
