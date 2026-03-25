from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from hft_platform.core import timebase

DEFAULT_AUTONOMY_EVIDENCE_DIR = Path("outputs/production_rollout/autonomy")

_shared_writer: "AutonomyEvidenceWriter | None" = None
_shared_writer_lock = Lock()


class AutonomyEvidenceWriter:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else DEFAULT_AUTONOMY_EVIDENCE_DIR
        self._trading_date: date | None = None
        self.on_transition: list = []

    def set_trading_date(self, trading_date: date) -> None:
        self._trading_date = trading_date

    @property
    def session_dir(self) -> Path:
        if self._trading_date is not None:
            return self.base_dir / self._trading_date.strftime("%Y%m%d")
        return self.base_dir / datetime.now().strftime("%Y%m%d")

    def record_transition(
        self,
        *,
        scope: str,
        mode: str,
        reason: str,
        manual_rearm_required: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
        for cb in self.on_transition:
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
            f"- `{record['scope']}` reason=`{record['reason']}` metadata={json.dumps(record['metadata'], ensure_ascii=False)}",
        )
        self._update_runtime_state(record)

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
        path = self.base_dir / "runtime_state.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {}
        else:
            payload = {}

        platform = payload.get("platform")
        if not isinstance(platform, dict):
            platform = {"manual_rearm_required": False, "reason": None}
            payload["platform"] = platform
        strategies = payload.get("strategies")
        if not isinstance(strategies, dict):
            strategies = {}
            payload["strategies"] = strategies

        if record["scope"] == "platform":
            platform["manual_rearm_required"] = True
            platform["reason"] = record["reason"]
        elif record["scope"] == "strategy":
            strategy_id = str(record["metadata"].get("strategy_id") or "").strip()
            if strategy_id:
                strategies[strategy_id] = {
                    "manual_rearm_required": True,
                    "reason": record["reason"],
                }

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def write_daily_summary(self, summary: dict[str, Any]) -> None:
        path = self._ensure_session_dir() / "daily_summary.json"
        path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    def _append_markdown(self, filename: str, line: str) -> None:
        path = self._ensure_session_dir() / filename
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line.rstrip() + "\n")

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
