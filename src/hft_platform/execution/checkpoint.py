"""Position checkpoint writer — periodic async serialization of PositionStore.

Atomic write (temp file + os.rename) with SHA-256 integrity hash.
Env vars:
    HFT_POSITION_CHECKPOINT_PATH  — default: .runtime/position_checkpoint.json
    HFT_CHECKPOINT_INTERVAL_S     — default: 60
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from structlog import get_logger

from hft_platform.core.timebase import now_ns

if TYPE_CHECKING:
    from hft_platform.execution.positions import PositionStore

try:
    import orjson

    def _dumps(obj: Any) -> bytes:
        return orjson.dumps(obj)

    def _loads(data: bytes) -> Any:
        return orjson.loads(data)

except ImportError:
    import json

    def _dumps(obj: Any) -> bytes:
        return json.dumps(obj, separators=(",", ":")).encode("utf-8")

    def _loads(data: bytes) -> Any:
        return json.loads(data)


logger = get_logger("execution.checkpoint")


class PositionCheckpointWriter:
    """Periodically serialize PositionStore to JSON with atomic write + SHA-256."""

    __slots__ = (
        "_store",
        "_path",
        "_interval_s",
        "_trading_date_provider",
        "running",
    )

    def __init__(
        self,
        store: PositionStore,
        path: Optional[str] = None,
        interval_s: Optional[float] = None,
        trading_date_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self._store = store
        self._path = path or os.getenv(
            "HFT_POSITION_CHECKPOINT_PATH",
            ".runtime/position_checkpoint.json",
        )
        self._interval_s = float(interval_s if interval_s is not None else os.getenv("HFT_CHECKPOINT_INTERVAL_S", "60"))  # type: ignore[arg-type]
        self._trading_date_provider: Callable[[], str] = trading_date_provider or (
            lambda: datetime.now(tz=ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")
        )
        self.running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Run the periodic checkpoint loop until ``self.running`` is cleared."""
        self.running = True
        logger.info(
            "checkpoint_writer_started",
            path=self._path,
            interval_s=self._interval_s,
        )
        try:
            while self.running:
                await asyncio.sleep(self._interval_s)
                if not self.running:
                    break
                try:
                    self.write_checkpoint()
                except Exception:
                    logger.exception("checkpoint_write_failed")
        finally:
            self.running = False

    def write_checkpoint(self) -> str:
        """Serialize current positions to disk atomically.

        Returns the path written.
        """
        positions_payload: Dict[str, Any] = {}
        for key, pos in self._store.positions.items():
            positions_payload[key] = {
                "symbol": pos.symbol,
                "net_qty": pos.net_qty,
                "avg_price_scaled": pos.avg_price_scaled,
                "realized_pnl_scaled": pos.realized_pnl_scaled,
            }

        body_obj = {
            "trading_date": self._trading_date_provider(),
            "timestamp_ns": now_ns(),
            "positions": positions_payload,
        }
        body_bytes = _dumps(body_obj)
        sha = hashlib.sha256(body_bytes).hexdigest()

        # Rebuild with hash included
        body_obj["sha256"] = sha
        final_bytes = _dumps(body_obj)

        # Ensure parent directory exists
        parent = os.path.dirname(self._path)  # type: ignore[type-var]
        if parent:
            os.makedirs(parent, exist_ok=True)

        # Atomic write: write to temp then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=parent or ".",
            prefix=".ckpt_",
            suffix=".tmp",
        )
        try:
            os.write(fd, final_bytes)
            os.fsync(fd)
            os.close(fd)
            os.rename(tmp_path, self._path)  # type: ignore[arg-type]
        except BaseException:
            os.close(fd) if not _is_closed(fd) else None
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

        logger.info(
            "checkpoint_written",
            path=self._path,
            positions=len(positions_payload),
            sha256=sha,
        )
        return self._path  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Static loader
    # ------------------------------------------------------------------

    @staticmethod
    def load_checkpoint(path: str) -> Optional[Dict[str, Any]]:
        """Load and verify a checkpoint file.

        Returns the parsed dict if valid, or ``None`` on missing / corrupt file.
        """
        if not os.path.exists(path):
            return None

        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            data = _loads(raw)
        except Exception:
            logger.warning("checkpoint_load_failed", path=path)
            return None

        stored_sha = data.pop("sha256", None)
        if stored_sha is None:
            logger.warning("checkpoint_missing_sha256", path=path)
            return None

        verify_bytes = _dumps(data)
        computed_sha = hashlib.sha256(verify_bytes).hexdigest()
        if computed_sha != stored_sha:
            logger.warning(
                "checkpoint_sha256_mismatch",
                path=path,
                expected=stored_sha,
                computed=computed_sha,
            )
            return None

        data["sha256"] = stored_sha
        return data


def _is_closed(fd: int) -> bool:
    """Check if a file descriptor is already closed."""
    try:
        os.fstat(fd)
        return False
    except OSError:
        return True
