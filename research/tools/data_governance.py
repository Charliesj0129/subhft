from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np


def _meta_path_for_dataset(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".meta.json")


def _load_array(path: Path) -> np.ndarray:
    source = np.load(path, allow_pickle=False)
    try:
        if isinstance(source, np.lib.npyio.NpzFile):
            if "data" in source:
                return np.asarray(source["data"])
            if source.files:
                return np.asarray(source[source.files[0]])
            return np.asarray([], dtype=np.float64)
        return np.asarray(source)
    finally:
        if isinstance(source, np.lib.npyio.NpzFile):
            source.close()


def _infer_fields(arr: np.ndarray) -> list[str]:
    if arr.dtype.names:
        return [str(name) for name in arr.dtype.names]
    if arr.ndim <= 1:
        return ["value"]
    width = int(arr.shape[1]) if arr.shape[1:] else 1
    width = max(1, min(width, 64))
    return [f"col_{i}" for i in range(width)]


def _rows(arr: np.ndarray) -> int:
    if arr.ndim == 0:
        return int(arr.size)
    return int(arr.shape[0])


def validate_metadata_payload(meta: Any, arr: np.ndarray) -> list[str]:
    required = ("dataset_id", "source_type", "owner", "schema_version", "rows", "fields")
    problems: list[str] = []
    if not isinstance(meta, dict):
        return ["meta_not_object"]

    for key in required:
        if key not in meta:
            problems.append(f"missing:{key}")

    source_type = str(meta.get("source_type", "")).lower()
    if source_type not in {"synthetic", "real"}:
        problems.append("source_type_must_be_synthetic_or_real")

    try:
        if int(meta.get("schema_version", 0)) < 1:
            problems.append("schema_version_must_be>=1")
    except (TypeError, ValueError):
        problems.append("schema_version_not_int")

    try:
        rows_meta = int(meta.get("rows", -1))
        rows_actual = _rows(arr)
        if rows_meta != rows_actual:
            problems.append(f"rows_mismatch(meta={rows_meta},actual={rows_actual})")
    except (TypeError, ValueError):
        problems.append("rows_not_int")

    fields = meta.get("fields")
    if not isinstance(fields, list) or not fields:
        problems.append("fields_must_be_nonempty_list")
    return problems


def cmd_stamp_data_meta(args: argparse.Namespace) -> int:
    data_path = Path(args.data).resolve()
    if not data_path.exists():
        print(f"[data_governance] data not found: {data_path}")
        return 2
    arr = _load_array(data_path)
    payload: dict[str, Any] = {
        "dataset_id": str(args.dataset_id or data_path.stem),
        "source_type": str(args.source_type),
        "source": str(args.source),
        "generator": str(getattr(args, "generator", "unknown")),
        "seed": (
            int(getattr(args, "seed"))
            if getattr(args, "seed", None) is not None
            else None
        ),
        "owner": str(args.owner),
        "schema_version": int(args.schema_version),
        "rows": _rows(arr),
        "fields": _infer_fields(arr),
        "symbols": [s.strip() for s in str(args.symbols).split(",") if s.strip()] if args.symbols else [],
        "split": str(args.split),
        "created_at": datetime.now(UTC).isoformat(),
        "data_file": str(data_path),
    }
    out_path = Path(args.out).resolve() if args.out else _meta_path_for_dataset(data_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(f"[data_governance] metadata written: {out_path}")
    return 0


def cmd_validate_data_meta(args: argparse.Namespace) -> int:
    data_path = Path(args.data).resolve()
    meta_path = Path(args.meta).resolve() if args.meta else _meta_path_for_dataset(data_path)
    if not data_path.exists():
        print(f"[data_governance] data not found: {data_path}")
        return 2
    if not meta_path.exists():
        print(f"[data_governance] metadata not found: {meta_path}")
        return 2
    arr = _load_array(data_path)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[data_governance] metadata invalid json: {exc}")
        return 2
    problems = validate_metadata_payload(meta, arr)
    if problems:
        print(json.dumps({"ok": False, "problems": problems, "meta": str(meta_path)}, indent=2))
        return 2
    print(json.dumps({"ok": True, "meta": str(meta_path), "rows": _rows(arr)}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dataset governance helpers.")
    sub = parser.add_subparsers(dest="command", required=True)

    stamp = sub.add_parser("stamp-data-meta", help="Create metadata sidecar for dataset")
    stamp.add_argument("data", help="Dataset path (.npy/.npz)")
    stamp.add_argument("--dataset-id", default=None)
    stamp.add_argument("--source-type", default="real", choices=["real", "synthetic"])
    stamp.add_argument("--source", default="unknown")
    stamp.add_argument("--generator", default="unknown")
    stamp.add_argument("--seed", type=int, default=None)
    stamp.add_argument("--owner", default="research")
    stamp.add_argument("--schema-version", type=int, default=1)
    stamp.add_argument("--symbols", default="")
    stamp.add_argument("--split", default="full")
    stamp.add_argument("--out", default=None)
    stamp.set_defaults(func=cmd_stamp_data_meta)

    validate = sub.add_parser("validate-data-meta", help="Validate metadata sidecar against dataset")
    validate.add_argument("data")
    validate.add_argument("--meta", default=None)
    validate.set_defaults(func=cmd_validate_data_meta)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
