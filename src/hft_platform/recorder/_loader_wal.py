"""WAL file discovery, manifest tracking, and single-file processing.

Functions operate on a ``WALLoaderService`` instance passed as the first
argument so that the public API stays in ``loader.py``.
"""

from __future__ import annotations

import fcntl
import glob
import os
import shutil
import tempfile
from typing import Any

from hft_platform.recorder._loader_common import (
    _loads,
    logger,
    timebase,
)

# ---------------------------------------------------------------------------
# Manifest helpers (P1-1)
# ---------------------------------------------------------------------------


def load_manifest(svc: Any) -> None:
    """Load processed file manifest from disk.

    EC-5: Validates manifest against actual WAL directory to detect
    stuck files (in manifest but still pending in WAL dir).
    """
    if not os.path.exists(svc._manifest_path):
        svc._manifest = set()
        return
    try:
        with open(svc._manifest_path, "r") as f:
            svc._manifest = {line.strip() for line in f if line.strip()}
        logger.info("Loaded WAL manifest", count=len(svc._manifest))
    except Exception as e:
        logger.warning("Failed to load manifest, starting fresh", error=str(e))
        svc._manifest = set()
        return

    # EC-5: Detect stuck files still in WAL dir but marked as processed
    try:
        pending = {f for f in os.listdir(svc.wal_dir) if f.endswith(".jsonl")}
        stuck = svc._manifest & pending
        if stuck:
            logger.warning(
                "Manifest has entries still pending in WAL dir, allowing re-process",
                count=len(stuck),
            )
            svc._manifest -= stuck
    except OSError:
        pass


def save_manifest(svc: Any) -> None:
    """Save processed file manifest to disk atomically (P1-1, EC-5).

    Uses temp file + fsync + rename to prevent corruption on crash.
    """
    manifest_dir = os.path.dirname(svc._manifest_path) or "."
    try:
        os.makedirs(manifest_dir, exist_ok=True)
        if os.path.exists(svc._manifest_path):
            bak_path = svc._manifest_path + ".bak"
            try:
                shutil.copy2(svc._manifest_path, bak_path)
            except OSError:
                pass
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=manifest_dir)
        try:
            with os.fdopen(fd, "w") as f:
                for fname in sorted(svc._manifest):
                    f.write(fname + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, svc._manifest_path)
            # Directory fsync to ensure rename is durable on crash
            try:
                dir_fd = os.open(manifest_dir, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass  # Best-effort; rename itself is atomic
        except Exception as _exc:  # noqa: BLE001
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    except Exception as e:
        logger.warning("Failed to save manifest", error=str(e))


# ---------------------------------------------------------------------------
# File discovery helpers
# ---------------------------------------------------------------------------


def extract_file_ts(fname: str) -> int:
    """Extract nanosecond timestamp from WAL filename (EC-2).

    Filename format: ``{table}_{nanosecond_ts}.jsonl``
    Returns 0 if parsing fails.
    """
    try:
        base = fname.rsplit(".", 1)[0]
        ts_str = base.rsplit("_", 1)[-1]
        return int(ts_str)
    except (ValueError, IndexError):
        return 0


def get_new_files(svc: Any) -> list[str]:
    """Get list of new WAL files not in manifest (P1-1, EC-2).

    Returns full file paths sorted by embedded nanosecond timestamp.
    """
    if not svc._manifest_enabled:
        files = glob.glob(os.path.join(svc.wal_dir, "*.jsonl"))
        files.sort(key=lambda p: extract_file_ts(os.path.basename(p)))
        return files

    try:
        current = {f for f in os.listdir(svc.wal_dir) if f.endswith(".jsonl")}
    except OSError:
        return []

    new_files = sorted(current - svc._manifest, key=extract_file_ts)
    return [os.path.join(svc.wal_dir, f) for f in new_files]


def mark_processed(svc: Any, filename: str) -> None:
    """Mark file as processed in manifest (P1-1, CC-3 thread-safe)."""
    if not svc._manifest_enabled:
        return
    fname = os.path.basename(filename)
    with svc._manifest_lock:
        svc._manifest.add(fname)


# ---------------------------------------------------------------------------
# Table name parsing
# ---------------------------------------------------------------------------


def parse_table_from_filename(fname: str) -> str:
    """Extract target table name from WAL filename."""
    base = fname
    # Emergency dump files use batch format with __wal_table__ header;
    # filename-based parsing is not needed and would produce wrong results.
    if base.startswith("emergency"):
        return "unknown"
    if "_" in fname:
        base = "_".join(fname.split("_")[:-1])
    if base.startswith("hft."):
        base = base.split(".", 1)[1]
    if base.startswith("market_data"):
        return "market_data"
    if base.startswith("orders"):
        return "orders"
    if base.startswith("fills"):
        return "fills"
    if base.startswith("risk_log"):
        return "risk_log"
    if base.startswith("backtest_runs"):
        return "backtest_runs"
    if base.startswith("latency_spans"):
        return "latency_spans"
    if base.startswith("pnl_snapshots"):
        return "pnl_snapshots"
    return base or "unknown"


def parse_batch_table_name(table_name: str) -> str:
    """Map batch writer table names to loader table names."""
    if table_name.startswith("hft."):
        table_name = table_name[4:]
    mapping = {
        "market_data": "market_data",
        "orders": "orders",
        "trades": "trades",
        "fills": "fills",
        "risk_log": "risk_log",
        "logs": "risk_log",
        "backtest_runs": "backtest_runs",
        "latency_spans": "latency_spans",
        "pnl_snapshots": "pnl_snapshots",
    }
    result = mapping.get(table_name)
    if result is None:
        raise ValueError(f"Unknown table name in WAL batch: {table_name!r}")
    return result


# ---------------------------------------------------------------------------
# Single-file processing
# ---------------------------------------------------------------------------


def process_single_file(svc: Any, fpath: str, force: bool = False) -> bool:
    """Process a single WAL file (CC-3: extracted for parallel use).

    Returns True if the file was successfully processed and archived.
    """
    fname = os.path.basename(fpath)

    # CE3-03: Shard claim
    if not svc._claim_registry.try_claim(fname):
        logger.debug("WAL file already claimed, skipping", file=fname)
        return False

    try:
        return _process_single_file_inner(svc, fpath, fname, force)
    except ConnectionError:
        logger.error("WAL file processing failed due to ClickHouse connection error", file=fname)
        return False
    finally:
        svc._claim_registry.release_claim(fname)


def _process_single_file_inner(svc: Any, fpath: str, fname: str, force: bool) -> bool:
    """Inner processing logic (called after claim acquired)."""
    # Check modification time to ensure writer is done
    if not force:
        try:
            mtime = os.path.getmtime(fpath)
            if timebase.now_s() - mtime < 2.0:
                return False
        except OSError:
            return False

    # EC-2: Strict ordering check
    if svc._strict_order:
        file_ts = extract_file_ts(fname)
        with svc._loader_stats_lock:
            last_ts_snapshot = svc._last_processed_ts
        if file_ts and file_ts < last_ts_snapshot:
            logger.warning(
                "WAL file timestamp out of order, quarantining (strict mode)",
                file=fname,
                file_ts=file_ts,
                last_ts=last_ts_snapshot,
            )
            svc._quarantine_corrupt_file(
                fpath,
                fname,
                f"out_of_order: file_ts={file_ts} < last_processed_ts={last_ts_snapshot}",
            )
            return False

    logger.info("Loading file", file=fname)

    # Read all lines from file
    all_lines: list = []
    corrupt_lines = 0
    try:
        with open(fpath, "r") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.debug("File locked by writer, skipping", file=fname)
                return False
            try:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        all_lines.append(_loads(line))
                    except Exception as _exc:  # noqa: BLE001
                        corrupt_lines += 1
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        if corrupt_lines > 0 and not all_lines:
            svc._quarantine_corrupt_file(fpath, fname, f"All {corrupt_lines} lines corrupt")
            return False
        elif corrupt_lines > 0:
            logger.warning(
                "Partial corruption in WAL file",
                file=fname,
                corrupt_lines=corrupt_lines,
                valid_rows=len(all_lines),
            )

    except FileNotFoundError:
        return False

    if not all_lines:
        try:
            shutil.move(fpath, os.path.join(svc.archive_dir, fname))
        except FileNotFoundError:
            pass
        return True

    # CC-4: Detect multi-table batch format
    is_batch = isinstance(all_lines[0], dict) and "__wal_table__" in all_lines[0]

    if is_batch:
        table_batches: list[tuple[str, list]] = []
        current_table = None
        current_rows: list = []

        for obj in all_lines:
            if isinstance(obj, dict) and "__wal_table__" in obj:
                if current_table and current_rows:
                    table_batches.append((current_table, current_rows))
                current_table = obj["__wal_table__"]
                current_rows = []
            else:
                current_rows.append(obj)

        if current_table and current_rows:
            table_batches.append((current_table, current_rows))

        for target_table, rows in table_batches:
            try:
                parsed_table = parse_batch_table_name(target_table)
            except ValueError:
                logger.warning("Unknown batch table name, skipping batch entry", table=target_table, file=fname)
                return False
            success = svc._insert_with_dedup(parsed_table, rows, fname)
            if not success:
                svc._write_to_dlq(parsed_table, rows, "insert_failed_after_retries")
                return False
    else:
        target_table = parse_table_from_filename(fname)
        if target_table == "unknown":
            logger.warning("Unknown table for file", file=fname)
            return False

        success = svc._insert_with_dedup(target_table, all_lines, fname)
        if not success:
            svc._write_to_dlq(target_table, all_lines, "insert_failed_after_retries")
            return False

    # Move to archive
    try:
        shutil.move(fpath, os.path.join(svc.archive_dir, fname))
        logger.info("Archived file", file=fname)
        mark_processed(svc, fpath)

        file_ts = extract_file_ts(fname)
        with svc._loader_stats_lock:
            if file_ts > svc._last_processed_ts:
                svc._last_processed_ts = file_ts
            svc._processed_files_total += 1
        return True
    except FileNotFoundError:
        return False


def process_files(svc: Any, force: bool = False) -> None:
    """Process pending WAL files and load to ClickHouse.

    CC-3: Supports parallel file processing via ThreadPoolExecutor.
    """
    if not svc.ch_client:
        logger.debug("ClickHouse client not ready, deferring WAL processing")
        return

    files = get_new_files(svc)
    if not files:
        return

    processed = 0

    if svc._loader_concurrency > 1 and len(files) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=svc._loader_concurrency) as pool:
            futures = {pool.submit(process_single_file, svc, f, force): f for f in files}
            for future in as_completed(futures):
                fpath = futures[future]
                try:
                    if future.result():
                        processed += 1
                except Exception as e:
                    logger.error(
                        "File processing failed",
                        file=fpath,
                        error=str(e),
                    )
    else:
        for fpath in files:
            try:
                if process_single_file(svc, fpath, force):
                    processed += 1
            except Exception as e:
                logger.error("File processing failed", file=fpath, error=str(e))

    if processed and svc._manifest_enabled:
        save_manifest(svc)
