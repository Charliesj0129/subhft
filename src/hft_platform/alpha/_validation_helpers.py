from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np

from hft_platform.alpha._validation_types import ValidationConfig
from hft_platform.core import timebase

_ALPHA_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _validate_alpha_id(alpha_id: str) -> None:
    """Validate alpha_id against a strict pattern to prevent injection attacks.

    Raises ValueError if alpha_id does not match ``[a-z][a-z0-9_]{0,63}``.
    """
    if not isinstance(alpha_id, str) or not _ALPHA_ID_PATTERN.fullmatch(alpha_id):
        raise ValueError(f"Invalid alpha_id {alpha_id!r}: must match [a-z][a-z0-9_]{{0,63}}")


def _resolve_data_path(root: Path, path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    return str(p.resolve())


def _resolve_allowed_data_roots(root: Path | None, config: ValidationConfig | None) -> list[str]:
    if root is None or config is None:
        return []
    out: list[str] = []
    for rel in tuple(config.allowed_data_roots):
        text = str(rel).strip()
        if not text:
            continue
        p = Path(text)
        if not p.is_absolute():
            p = root / p
        out.append(str(p.resolve()))
    return out


def _path_under_any(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve()
    for base in roots:
        base_resolved = base.resolve()
        if resolved == base_resolved:
            return True
        if base_resolved in resolved.parents:
            return True
    return False


def _ensure_project_root_on_path(root: Path | None = None) -> None:
    candidates = [root, Path(__file__).resolve().parents[3], Path.cwd()]
    for candidate in candidates:
        if candidate is None:
            continue
        resolved = Path(candidate).resolve()
        if not (resolved / "research").exists():
            continue
        resolved_str = str(resolved)
        if resolved_str not in sys.path:
            sys.path.insert(0, resolved_str)


@contextmanager
def _pushd(path: Path):
    prev = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


def _make_validation_artifact_dir(experiments_base: Path, alpha_id: str) -> Path:
    stamp = _dt.datetime.fromtimestamp(timebase.now_s(), tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = experiments_base / "validations" / alpha_id / f"{stamp}_{uuid4().hex[:8]}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def _resolve_first_data_meta_path(data_paths: list[str]) -> str | None:
    for path_str in data_paths:
        data_path = Path(path_str).resolve()
        _payload, meta_path, _error = _load_dataset_metadata(data_path)
        if meta_path is not None and meta_path.exists():
            return str(meta_path)
    return None


def _dataset_metadata_candidates(data_path: Path) -> list[Path]:
    return [
        data_path.with_suffix(data_path.suffix + ".meta.json"),
        data_path.with_suffix(".meta.json"),
        data_path.with_suffix(data_path.suffix + ".metadata.json"),
        data_path.with_suffix(".metadata.json"),
    ]


def _missing_or_blank_metadata_keys(meta: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    missing: list[str] = []
    for key in required:
        if key not in meta:
            missing.append(key)
            continue
        value = meta.get(key)
        if value is None:
            missing.append(key)
            continue
        if isinstance(value, str) and not value.strip():
            missing.append(key)
            continue
        if isinstance(value, (list, tuple, set, dict)) and len(value) == 0:
            missing.append(key)
    return missing


def _dataset_row_count(path: Path) -> int | None:
    source = np.load(path, allow_pickle=False)
    try:
        if isinstance(source, np.lib.npyio.NpzFile):
            if "data" in source:
                arr = np.asarray(source["data"])
            elif source.files:
                arr = np.asarray(source[source.files[0]])
            else:
                return 0
        else:
            arr = np.asarray(source)
        if arr.ndim == 0:
            return int(arr.size)
        return int(arr.shape[0])
    except Exception:
        return None
    finally:
        if isinstance(source, np.lib.npyio.NpzFile):
            source.close()


def _has_hftbt_data(data_paths: list[str]) -> bool:
    """Return True if at least one data path has a sibling hftbt.npz file."""
    for path_str in data_paths:
        p = Path(path_str)
        if p.name == "hftbt.npz" and p.exists():
            return True
        if (p.parent / "hftbt.npz").exists():
            return True
    return False


def _load_dataset_metadata(data_path: Path) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    for meta_path in _dataset_metadata_candidates(data_path):
        if not meta_path.exists():
            continue
        try:
            payload = json.loads(meta_path.read_text())
        except (OSError, ValueError) as exc:
            return None, meta_path, f"invalid_json:{exc}"
        if not isinstance(payload, dict):
            return None, meta_path, "invalid_format"
        return payload, meta_path, None
    return None, None, "missing_meta_file"
