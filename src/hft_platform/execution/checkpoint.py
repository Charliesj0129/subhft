"""Position checkpoint writer — periodic async serialization of PositionStore.

Atomic write (temp file + os.rename) with SHA-256 integrity hash.
Env vars:
    HFT_POSITION_CHECKPOINT_PATH  — default: .state/position_checkpoint.json
    HFT_CHECKPOINT_INTERVAL_S     — default: 60
"""

from __future__ import annotations

import asyncio
import glob as glob_mod
import hashlib
import os
import tempfile
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.core.timebase import now_ns

if TYPE_CHECKING:
    from hft_platform.execution.positions import PositionStore

try:
    import orjson

    def _dumps(obj: Any) -> bytes:
        return orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)

    def _loads(data: bytes) -> Any:
        return orjson.loads(data)

except ImportError:
    import json

    def _dumps(obj: Any) -> bytes:
        return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def _loads(data: bytes) -> Any:
        return json.loads(data)


logger = get_logger("execution.checkpoint")

_TZ_TPE = ZoneInfo("Asia/Taipei")
DEFAULT_POSITION_CHECKPOINT_PATH = ".state/position_checkpoint.json"


def _taifex_trading_date() -> str:
    """Return TAIFEX trading date (YYYYMMDD).

    Futures night session (15:00-05:00) belongs to the PREVIOUS calendar date.
    If current Taipei time is between 00:00 and 05:00, use D-1.
    """
    now = datetime.fromtimestamp(timebase.now_s(), tz=_TZ_TPE)
    if now.hour < 5:
        now = now - timedelta(days=1)
    return now.strftime("%Y%m%d")


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
            DEFAULT_POSITION_CHECKPOINT_PATH,
        )
        self._interval_s = float(interval_s if interval_s is not None else os.getenv("HFT_CHECKPOINT_INTERVAL_S", "60"))  # type: ignore[arg-type]
        self._trading_date_provider: Callable[[], str] = trading_date_provider or _taifex_trading_date
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
        loop = asyncio.get_running_loop()
        try:
            while self.running:
                await asyncio.sleep(self._interval_s)
                if not self.running:
                    break
                try:
                    await loop.run_in_executor(None, self.write_checkpoint)
                except Exception:
                    logger.exception("checkpoint_write_failed")
        finally:
            self.running = False

    def write_checkpoint(self) -> str:
        """Serialize current positions to disk atomically.

        Returns the path written.

        M3: Acquires _fill_lock via snapshot_positions() to ensure a consistent
        read of position state during serialization (no concurrent fill mutations).
        """
        # M3: Use snapshot_positions() which holds _fill_lock for the copy,
        # preventing concurrent fills from producing partial/torn position state.
        snapshot = self._store.snapshot_positions()

        positions_payload: Dict[str, Any] = {}
        for key, pos in snapshot.items():
            entry: Dict[str, Any] = {
                "symbol": pos.symbol,
                "net_qty": pos.net_qty,
                "avg_price_scaled": pos.avg_price_scaled,
                "realized_pnl_scaled": pos.realized_pnl_scaled,
                "fees_scaled": pos.fees_scaled,  # M1: include accumulated fees
            }
            if pos.avg_price_scaled < 0:
                entry["unknown_basis"] = True
            positions_payload[key] = entry

        # M4: Also persist pending recovery positions that haven't been merged
        # into live positions yet (no fills received since restart). Without this,
        # a restart-without-fills followed by checkpoint would erase recovery data.
        recovery = getattr(self._store, "_recovery_positions", {})
        for rkey, rdata in recovery.items():
            if rkey not in positions_payload:
                avg_price = rdata.get("avg_price_scaled", 0)
                positions_payload[rkey] = {
                    "symbol": rdata.get("symbol", rkey.split(":")[-1]),
                    "net_qty": rdata["net_qty"],
                    "avg_price_scaled": avg_price,
                    "realized_pnl_scaled": rdata.get("realized_pnl_scaled", 0),
                    "fees_scaled": rdata.get("fees_scaled", 0),
                    "unknown_basis": avg_price < 0,  # flag sentinel for downstream awareness
                }

        body_obj = {
            "trading_date": self._trading_date_provider(),
            "timestamp_ns": now_ns(),
            # M2: persist portfolio aggregates so StormGuard drawdown survives crash recovery
            "peak_equity_scaled": self._store._peak_equity_scaled,
            "total_realized_pnl_scaled": self._store._total_realized_pnl_scaled,
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

        # Atomic write: write to temp then rename.
        #
        # Bug e1967c0c (2026-04-25) regressed this path by introducing a
        # raw ``os.close(fd)`` followed by a ``finally``-block ``_is_closed``
        # probe + second ``os.close(fd)``. Between the two ``close`` calls
        # the kernel reused the freed fd integer for the WAL batch-timer
        # daemon thread's brand-new ``tempfile.mkstemp`` fd, and the
        # second ``os.close`` ripped that fd out from under the WAL
        # writer — surfacing as ``[Errno 9] Bad file descriptor`` on its
        # next ``write/flush/fsync``. Switching to ``with os.fdopen(fd, "wb")``
        # mirrors the 7 sibling sites (``fill_dlq``, ``gateway/dedup``,
        # ``router``, ``canary``, ``recorder/_loader_*``) and guarantees fd
        # ownership transfers cleanly to the file object: it is closed
        # exactly once, on both the success and exception paths.
        fd, tmp_path = tempfile.mkstemp(
            dir=parent or ".",
            prefix=".ckpt_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(final_bytes)
                f.flush()
                # Durability: fsync MUST happen before close+rename so the
                # data is on stable storage prior to the atomic rename.
                os.fsync(f.fileno())
            os.rename(tmp_path, self._path)  # type: ignore[arg-type]
        except BaseException:
            # ``with`` already closed the fd. Only the temp path may still
            # be on disk if rename failed or the write/fsync raised.
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
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

        Also cleans up stale ``.ckpt_*.tmp`` files left by hard crashes
        (SIGKILL / power loss between fsync and rename).

        Returns the parsed dict if valid, or ``None`` on missing / corrupt file.
        """
        # Clean stale tmp files from previous crashed writes
        parent = os.path.dirname(path) or "."
        for tmp in glob_mod.glob(os.path.join(parent, ".ckpt_*.tmp")):
            try:
                os.unlink(tmp)
                logger.info("checkpoint_stale_tmp_cleaned", path=tmp)
            except OSError:
                pass

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

    @staticmethod
    def clear_checkpoint(path: str | None = None) -> bool:
        """Remove checkpoint file for graceful position reset.

        Returns True if file was deleted, False if it didn't exist.
        Logs the operation for audit trail.
        """
        resolved_path = (
            path
            if path
            else os.getenv(
                "HFT_POSITION_CHECKPOINT_PATH",
                DEFAULT_POSITION_CHECKPOINT_PATH,
            )
        )
        assert resolved_path is not None  # always has a default
        if os.path.exists(resolved_path):
            os.unlink(resolved_path)
            logger.warning(
                "checkpoint_cleared",
                path=resolved_path,
                msg="Position checkpoint deleted via programmatic reset",
            )
            return True
        logger.info("checkpoint_clear_no_file", path=resolved_path)
        return False
