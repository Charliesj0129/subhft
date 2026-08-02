"""Resumable, fail-closed 72-hour SMMA-family mining runner."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence, TypeAlias

import numpy as np

from hft_platform.core import timebase
from research.backtest.cost_models import load_cost_profile
from research.combinatorial.bidask import BIDASK_FEATURE_HISTORY_BARS, build_bidask_family_features
from research.combinatorial.canonical_ast import canonical_hash
from research.combinatorial.expression_eval import evaluate_family_expression
from research.combinatorial.expression_lang import compile_expression
from research.combinatorial.gp_alpha_adapter import required_history_by_variable
from research.combinatorial.kbar import KBAR_FEATURE_HISTORY_BARS, build_kbar_family_features
from research.combinatorial.smma import (
    FEEDBACK_GENERATOR_VERSION,
    SMMA_LENGTHS,
    build_smma_family_features,
    evaluate_smma_expression,
    generated_feedback_proposals,
    generated_gp_expressions,
    normalize_smma_lengths,
    smma_lengths_from_expression,
    validate_stationary_signal,
)
from research.combinatorial.smma_dataset import (
    DATASET_SCHEMA,
    DATE_FROM,
    DATE_TO,
    ROOTS,
    BarDataset,
    export_clickhouse_dataset,
    load_governed_dataset,
)
from research.combinatorial.smma_validation import (
    ENTRY_RULE_VERSION,
    MIN_DAYS_FOR_PROMOTION,
    MINIMUM_EDGE_POINTS,
    STRICT_EDGE_TARGET_POINTS,
    THRESHOLD_QUANTILES,
    ExecutionResult,
    KillMetrics,
    LockedMetrics,
    ThresholdResolution,
    activation_mask,
    benjamini_hochberg,
    build_split_plan,
    candidate_rank_key,
    effective_test_count,
    evaluate_recent_kill_criteria,
    forward_returns,
    forward_target_indices,
    locked_validation,
    metrics_to_dict,
    monotonic_horizon_pollution,
    resolve_quantile_threshold,
    simulate_next_bar_execution,
)
from research.combinatorial.taifex_trading_dates import build_trading_date_window
from research.combinatorial.tick import TICK_FEATURE_HISTORY_BARS, build_tick_family_features
from research.combinatorial.tick_dataset import (
    TICK_DATASET_SCHEMA,
    TICK_ROOTS,
    TickBarDataset,
    export_clickhouse_tick_dataset,
    load_governed_tick_dataset,
)

# A mining run's active family determines the concrete dataset type: "smma",
# "bidask" and "kbar" all read the SMMA-governed BarDataset (bidask reuses its
# existing bid/ask columns, kbar its OHLCV columns), while "tick" reads the
# independent TickBarDataset. Every
# generic MiningRun code path (split planning, robustness slicing, fill
# simulation) only ever touches fields both dataclasses share.
GovernedBars: TypeAlias = BarDataset | TickBarDataset

RUN_SCHEMA = "alpha_mining_run.v3"
CHECKPOINT_SCHEMA = "alpha_mining_checkpoint.v3"
PRIMARY_TIMEFRAMES_MINUTES: tuple[int, ...] = (60, 120, 240)
SUPPORTED_PRIMARY_TIMEFRAMES_MINUTES: tuple[int, ...] = (2, *PRIMARY_TIMEFRAMES_MINUTES)
DEFAULT_SEEDS: tuple[int, ...] = (20260726, 20260727, 20260728)
MAX_WALL_TIME_HOURS = 72.0
MAX_CANDIDATES = 20_000
MAX_WORKERS = 12
DISCOVERY_PROCESS_START_METHOD = "forkserver"
DISCOVERY_NUMERIC_THREAD_ENV = (
    "OPENBLAS_NUM_THREADS",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)
CHECKPOINT_TRIALS = 250
CHECKPOINT_SECONDS = 300.0
HEARTBEAT_SECONDS = 60.0
RSS_PAUSE_BYTES = 18 * 1024**3
RSS_STOP_BYTES = 20 * 1024**3
OUTPUT_STOP_BYTES = 100 * 1024**3
DAY_NS = 24 * 60 * 60 * 1_000_000_000
CANDIDATES_PER_EXPRESSION = len(("1h", "4h", "session")) * len((1, -1)) * len(THRESHOLD_QUANTILES)
FEEDBACK_ATTEMPTS_PER_EXPRESSION = 50
MIN_FEEDBACK_ATTEMPTS = 100

_CODE_FILES: tuple[str, ...] = (
    "research/combinatorial/smma.py",
    "research/combinatorial/smma_alpha_adapter.py",
    "research/combinatorial/smma_dataset.py",
    "research/combinatorial/smma_runner.py",
    "research/combinatorial/smma_screen.py",
    "research/combinatorial/smma_validation.py",
    "research/combinatorial/bidask.py",
    "research/combinatorial/kbar.py",
    "research/combinatorial/tick.py",
    "research/combinatorial/tick_dataset.py",
    "research/combinatorial/taifex_trading_dates.py",
    "research/combinatorial/expression_eval.py",
    "research/combinatorial/canonical_ast.py",
    "research/combinatorial/expression_lang.py",
    "research/combinatorial/gp_alpha_adapter.py",
    "research/combinatorial/operator_library.py",
    "research/combinatorial/ledger.py",
    "research/combinatorial/partitioning.py",
    "research/combinatorial/promote.py",
    "research/backtest/cost_models.py",
    "config/research/cost_profiles.yaml",
    "src/hft_platform/cli/_alpha.py",
    "src/hft_platform/cli/_parser.py",
)


class RunIntegrityError(RuntimeError):
    """A resume fingerprint or append-only artifact did not match."""


def _smma_build_features(bars: BarDataset, config: "RunConfig") -> dict[str, np.ndarray]:
    return build_smma_family_features(
        open_=bars.open,
        high=bars.high,
        low=bars.low,
        close=bars.close,
        reset_mask=bars.reset,
        lengths=config.smma_lengths,
    )


def _smma_build_features_for_expression(bars: BarDataset, expression: str) -> dict[str, np.ndarray]:
    return build_smma_family_features(
        open_=bars.open,
        high=bars.high,
        low=bars.low,
        close=bars.close,
        reset_mask=bars.reset,
        lengths=smma_lengths_from_expression(expression),
    )


def _bidask_features(bars: BarDataset) -> dict[str, np.ndarray]:
    return build_bidask_family_features(
        bid_open=bars.bid_open,
        ask_open=bars.ask_open,
        bid_qty_open=bars.bid_qty_open,
        ask_qty_open=bars.ask_qty_open,
        bid_close=bars.bid_close,
        ask_close=bars.ask_close,
        bid_qty_close=bars.bid_qty_close,
        ask_qty_close=bars.ask_qty_close,
        reset_mask=bars.reset,
    )


def _bidask_build_features(bars: BarDataset, config: "RunConfig") -> dict[str, np.ndarray]:
    del config
    return _bidask_features(bars)


def _bidask_build_features_for_expression(bars: BarDataset, expression: str) -> dict[str, np.ndarray]:
    del expression
    return _bidask_features(bars)


def _kbar_features(bars: BarDataset) -> dict[str, np.ndarray]:
    return build_kbar_family_features(
        open_=bars.open,
        high=bars.high,
        low=bars.low,
        close=bars.close,
        volume=bars.volume,
        reset_mask=bars.reset,
    )


def _kbar_build_features(bars: BarDataset, config: "RunConfig") -> dict[str, np.ndarray]:
    del config
    return _kbar_features(bars)


def _kbar_build_features_for_expression(bars: BarDataset, expression: str) -> dict[str, np.ndarray]:
    del expression
    return _kbar_features(bars)


def _tick_features(bars: TickBarDataset) -> dict[str, np.ndarray]:
    return build_tick_family_features(
        trade_tick_count=bars.trade_tick_count,
        quote_update_count=bars.quote_update_count,
        buy_tick_count=bars.buy_tick_count,
        sell_tick_count=bars.sell_tick_count,
        buy_tick_volume=bars.buy_tick_volume,
        sell_tick_volume=bars.sell_tick_volume,
        reset_mask=bars.reset,
    )


def _tick_build_features(bars: TickBarDataset, config: "RunConfig") -> dict[str, np.ndarray]:
    del config
    return _tick_features(bars)


def _tick_build_features_for_expression(bars: TickBarDataset, expression: str) -> dict[str, np.ndarray]:
    del expression
    return _tick_features(bars)


@dataclass(frozen=True, slots=True)
class FamilyDatasetConfig:
    """Per-family governed dataset scope + the export/load callables for it."""

    date_from: str
    date_to: str
    roots: tuple[str, ...]
    export: Any
    load: Any


@dataclass(frozen=True, slots=True)
class FamilyAdapter:
    """One mining family's feature builder, expression evaluator, and dataset scope."""

    build_features: Any
    build_features_for_expression: Any
    evaluate_expression: Any
    dataset: FamilyDatasetConfig


@dataclass(slots=True)
class _SelectionGroupContext:
    """Selection-stage data cached only for one root/timeframe group."""

    bars: GovernedBars
    split_labels: np.ndarray
    selection_mask: np.ndarray
    selection_ts: np.ndarray
    features: Mapping[str, np.ndarray]
    signals: dict[str, tuple[np.ndarray, np.ndarray]]


FAMILY_REGISTRY: dict[str, FamilyAdapter] = {
    "smma": FamilyAdapter(
        build_features=_smma_build_features,
        build_features_for_expression=_smma_build_features_for_expression,
        evaluate_expression=evaluate_smma_expression,
        dataset=FamilyDatasetConfig(
            date_from=DATE_FROM,
            date_to=DATE_TO,
            roots=ROOTS,
            export=export_clickhouse_dataset,
            load=load_governed_dataset,
        ),
    ),
    "bidask": FamilyAdapter(
        build_features=_bidask_build_features,
        build_features_for_expression=_bidask_build_features_for_expression,
        evaluate_expression=evaluate_family_expression,
        dataset=FamilyDatasetConfig(
            date_from="2026-01-27",
            date_to="2026-07-29",
            roots=ROOTS,
            export=export_clickhouse_dataset,
            load=load_governed_dataset,
        ),
    ),
    "kbar": FamilyAdapter(
        build_features=_kbar_build_features,
        build_features_for_expression=_kbar_build_features_for_expression,
        evaluate_expression=evaluate_family_expression,
        dataset=FamilyDatasetConfig(
            date_from="2026-01-27",
            date_to="2026-07-29",
            roots=ROOTS,
            export=export_clickhouse_dataset,
            load=load_governed_dataset,
        ),
    ),
    "tick": FamilyAdapter(
        build_features=_tick_build_features,
        build_features_for_expression=_tick_build_features_for_expression,
        evaluate_expression=evaluate_family_expression,
        dataset=FamilyDatasetConfig(
            date_from="2026-04-07",
            date_to="2026-07-29",
            roots=TICK_ROOTS,
            export=export_clickhouse_tick_dataset,
            load=load_governed_tick_dataset,
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class RunConfig:
    run_dir: Path
    family: str = "smma"
    wall_time_hours: float = MAX_WALL_TIME_HOURS
    max_candidates: int = MAX_CANDIDATES
    workers: int = MAX_WORKERS
    seeds: tuple[int, ...] = DEFAULT_SEEDS
    timeframes_minutes: tuple[int, ...] = PRIMARY_TIMEFRAMES_MINUTES
    smma_lengths: tuple[int, ...] = SMMA_LENGTHS
    posthoc_diagnostic: bool = False
    resume: bool = False
    unlock_final_holdout: bool = False
    dataset_cache_dir: Path | None = None
    cost_mode: str = "per_contract"
    feedback_expressions_per_group: int = 0

    def validate(self) -> None:
        if self.family not in FAMILY_REGISTRY:
            raise ValueError(f"unsupported family: {self.family!r}; choose one of {sorted(FAMILY_REGISTRY)}")
        if not (0.0 < float(self.wall_time_hours) <= MAX_WALL_TIME_HOURS):
            raise ValueError("wall_time_hours must be in (0, 72]")
        if not (1 <= int(self.max_candidates) <= MAX_CANDIDATES):
            raise ValueError("max_candidates must be in [1, 20000]")
        if not (1 <= int(self.workers) <= MAX_WORKERS):
            raise ValueError("workers must be in [1, 12]")
        if len(self.seeds) != 3 or len(set(int(seed) for seed in self.seeds)) != 3:
            raise ValueError("exactly three distinct seeds are required")
        if (
            not self.timeframes_minutes
            or len(set(int(value) for value in self.timeframes_minutes)) != len(self.timeframes_minutes)
            or not set(int(value) for value in self.timeframes_minutes).issubset(
                set(SUPPORTED_PRIMARY_TIMEFRAMES_MINUTES)
            )
        ):
            raise ValueError("timeframes_minutes must be distinct supported primary values")
        if self.family == "smma":
            normalize_smma_lengths(self.smma_lengths)
        if self.unlock_final_holdout and self.family != "smma":
            raise ValueError("final holdout cannot be unlocked for a family without a truthful screen adapter")
        if self.cost_mode not in {"per_contract", "root_proxy"}:
            raise ValueError("cost_mode must be 'per_contract' or 'root_proxy'")
        if self.cost_mode == "root_proxy" and not self.posthoc_diagnostic:
            raise ValueError("root_proxy cost mode is allowed only for an explicit posthoc diagnostic")
        if int(self.feedback_expressions_per_group) < 0:
            raise ValueError("feedback_expressions_per_group must be non-negative")
        if self.feedback_expressions_per_group and self.unlock_final_holdout:
            raise ValueError("adaptive-search pilots cannot unlock the final holdout")
        if self.feedback_expressions_per_group:
            group_count = len(FAMILY_REGISTRY[self.family].dataset.roots) * len(self.timeframes_minutes)
            expression_slots = int(self.max_candidates) // (group_count * CANDIDATES_PER_EXPRESSION)
            if expression_slots <= int(self.feedback_expressions_per_group):
                raise ValueError("adaptive-search budget must leave at least one generation-0 expression per group")


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    root: str
    timeframe_min: int
    expression: str
    horizon: str
    direction: int
    threshold: float
    seed: int
    complexity: int
    family: str = "smma"
    threshold_quantile: float | None = None
    entry_rule_version: str = ENTRY_RULE_VERSION
    threshold_resolution: ThresholdResolution | None = None
    duplicate_of: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateResult:
    candidate: Candidate
    kill: KillMetrics
    stage: str
    status: str
    failure_reason: str = ""
    locked: LockedMetrics | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": asdict(self.candidate),
            "kill": metrics_to_dict(self.kill),
            "locked": metrics_to_dict(self.locked) if self.locked is not None else None,
            "stage": self.stage,
            "status": self.status,
            "failure_reason": self.failure_reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidateResult":
        locked_raw = payload.get("locked")
        kill_payload = dict(payload["kill"])
        kill_payload["reasons"] = tuple(kill_payload.get("reasons", ()))
        locked_payload = dict(locked_raw) if isinstance(locked_raw, Mapping) else None
        if locked_payload is not None:
            locked_payload["walk_forward_sharpes"] = tuple(locked_payload.get("walk_forward_sharpes", ()))
            locked_payload["failure_reasons"] = tuple(locked_payload.get("failure_reasons", ()))
            locked_payload["walk_forward_fold_trade_counts"] = tuple(
                locked_payload.get("walk_forward_fold_trade_counts", ())
            )
            locked_payload["walk_forward_fold_purged_counts"] = tuple(
                locked_payload.get("walk_forward_fold_purged_counts", ())
            )
        candidate_payload = dict(payload["candidate"])
        resolution_payload = candidate_payload.get("threshold_resolution")
        if isinstance(resolution_payload, Mapping):
            candidate_payload["threshold_resolution"] = ThresholdResolution(**dict(resolution_payload))
        return cls(
            candidate=Candidate(**candidate_payload),
            kill=KillMetrics(**kill_payload),
            locked=LockedMetrics(**locked_payload) if locked_payload is not None else None,
            stage=str(payload["stage"]),
            status=str(payload["status"]),
            failure_reason=str(payload.get("failure_reason", "")),
        )


def _result_from_kill_ledger_row(
    row: Mapping[str, Any],
    *,
    locked: LockedMetrics | None = None,
) -> CandidateResult:
    metrics = dict(row["metrics"])
    metrics["reasons"] = tuple(metrics.get("reasons", ()))
    candidate_payload = dict(row["candidate"])
    resolution_payload = candidate_payload.get("threshold_resolution")
    if isinstance(resolution_payload, Mapping):
        candidate_payload["threshold_resolution"] = ThresholdResolution(**dict(resolution_payload))
    return CandidateResult(
        candidate=Candidate(**candidate_payload),
        kill=KillMetrics(**metrics),
        locked=locked,
        stage=str(row["stage"]),
        status=str(row["status"]),
        failure_reason=str(row.get("failure_reason", "")),
    )


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def code_fingerprint(project_root: Path = Path(".")) -> str:
    digest = hashlib.sha256()
    for relative in _CODE_FILES:
        path = project_root / relative
        if not path.exists():
            raise RunIntegrityError(f"code fingerprint input is missing: {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        suffix=".json",
        delete=False,
        encoding="utf-8",
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    temporary.replace(path)


def _with_integrity_hash(payload: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(payload)
    result[field] = _canonical_hash(result)
    return result


def _verify_integrity_hash(
    payload: Mapping[str, Any],
    field: str,
    *,
    artifact: str,
) -> dict[str, Any]:
    verified = dict(payload)
    expected = str(verified.pop(field, ""))
    if not expected or _canonical_hash(verified) != expected:
        raise RunIntegrityError(f"{artifact} hash mismatch")
    return dict(payload)


class HashChainLedger:
    """Append-only, hash-chained JSONL ledger with resume validation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._last_hash = ""
        self._candidate_stages: set[tuple[str, str]] = set()
        self._discovery_candidate_ids: set[str] = set()
        self._rows: list[dict[str, Any]] = []
        self._warm()

    def _warm(self) -> None:
        if not self.path.exists():
            return
        previous = ""
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RunIntegrityError(f"trial ledger line {line_number} is invalid JSON") from exc
                row_hash = str(row.pop("row_hash", ""))
                if row.get("previous_hash", "") != previous or _canonical_hash(row) != row_hash:
                    raise RunIntegrityError(f"trial ledger hash chain mismatch at line {line_number}")
                previous = row_hash
                candidate_id = str(row.get("candidate_id", ""))
                stage = str(row.get("stage", ""))
                self._candidate_stages.add((candidate_id, stage))
                if stage == "discovery":
                    self._discovery_candidate_ids.add(candidate_id)
                self._rows.append(dict(row))
        self._last_hash = previous

    def has(self, candidate_id: str, stage: str) -> bool:
        return (candidate_id, stage) in self._candidate_stages

    def append(self, payload: Mapping[str, Any]) -> bool:
        candidate_id, stage = str(payload["candidate_id"]), str(payload["stage"])
        with self._lock:
            if (candidate_id, stage) in self._candidate_stages:
                return False
            row = {**dict(payload), "previous_hash": self._last_hash}
            row_hash = _canonical_hash(row)
            row["row_hash"] = row_hash
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._last_hash = row_hash
            self._candidate_stages.add((candidate_id, stage))
            if stage == "discovery":
                self._discovery_candidate_ids.add(candidate_id)
            stored = dict(row)
            stored.pop("row_hash")
            self._rows.append(stored)
            return True

    def rows(self, *, stage: str | None = None) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(dict(row) for row in self._rows if stage is None or str(row.get("stage", "")) == stage)

    @property
    def unique_candidates(self) -> int:
        with self._lock:
            return len(self._discovery_candidate_ids)

    @property
    def row_count(self) -> int:
        with self._lock:
            return len(self._rows)


class SplitUnlockGuard:
    """Two distinct freezes: selection→locked, then locked→final."""

    def __init__(self, path: Path) -> None:
        self._ledger = HashChainLedger(path)

    def freeze_locked(self, candidate_id: str) -> None:
        self._ledger.append(
            {
                "candidate_id": candidate_id,
                "stage": "freeze_locked",
                "recorded_at_ns": timebase.now_ns(),
            }
        )

    def freeze_final(self, candidate_id: str) -> None:
        if not self._ledger.has(candidate_id, "freeze_locked"):
            raise RunIntegrityError("candidate must be locked-frozen before final freeze")
        self._ledger.append(
            {
                "candidate_id": candidate_id,
                "stage": "freeze_final",
                "recorded_at_ns": timebase.now_ns(),
            }
        )

    def require(self, candidate_id: str, split: str) -> None:
        required = "freeze_locked" if split == "locked_validation" else "freeze_final"
        granted = self._ledger.has(candidate_id, required)
        self._ledger.append(
            {
                "candidate_id": f"access:{candidate_id}:{split}:{timebase.now_ns()}",
                "subject_candidate_id": candidate_id,
                "stage": "access",
                "split": split,
                "granted": granted,
                "recorded_at_ns": timebase.now_ns(),
            }
        )
        if not granted:
            raise RunIntegrityError(f"{split} is locked for candidate {candidate_id}")

    def has_granted_access(self, candidate_id: str, split: str) -> bool:
        return any(
            row.get("subject_candidate_id") == candidate_id and row.get("split") == split and row.get("granted") is True
            for row in self._ledger.rows(stage="access")
        )


def _candidate_id(payload: Mapping[str, Any]) -> str:
    return _canonical_hash(payload)


def _feedback_seed(*, family: str, root: str, timeframe_min: int, base_seed: int) -> int:
    material = f"{FEEDBACK_GENERATOR_VERSION}:{family}:{root}:{int(timeframe_min)}:{int(base_seed)}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def enumerate_candidates(
    *,
    family: str,
    root: str,
    timeframe_min: int,
    expressions: Sequence[str],
    signals: Mapping[str, np.ndarray],
    discovery_mask: np.ndarray,
    seed: int,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for expression in expressions:
        complexity = compile_expression(expression, max_depth=3).max_depth
        discovery_signal = np.asarray(signals[expression])[discovery_mask]
        resolutions = {
            (direction, threshold_quantile): resolve_quantile_threshold(
                discovery_signal,
                direction=direction,
                quantile=threshold_quantile,
            )
            for direction in (1, -1)
            for threshold_quantile in THRESHOLD_QUANTILES
        }
        for horizon in ("1h", "4h", "session"):
            for direction in (1, -1):
                canonical_by_cut: dict[str, str] = {}
                for threshold_quantile in THRESHOLD_QUANTILES:
                    resolution = resolutions[(direction, threshold_quantile)]
                    identity = {
                        "family": family,
                        "root": root,
                        "timeframe_min": timeframe_min,
                        "expression": expression,
                        "horizon": horizon,
                        "direction": direction,
                        "threshold_quantile": threshold_quantile,
                        "entry_rule_version": ENTRY_RULE_VERSION,
                    }
                    candidate_id = _candidate_id(identity)
                    cut_key = float(resolution.cut).hex()
                    duplicate_of = canonical_by_cut.setdefault(cut_key, candidate_id)
                    candidates.append(
                        Candidate(
                            candidate_id=candidate_id,
                            root=root,
                            timeframe_min=timeframe_min,
                            expression=expression,
                            horizon=horizon,
                            direction=direction,
                            threshold=resolution.cut,
                            seed=int(seed),
                            complexity=complexity,
                            family=family,
                            threshold_quantile=threshold_quantile,
                            entry_rule_version=ENTRY_RULE_VERSION,
                            threshold_resolution=resolution,
                            duplicate_of=duplicate_of if duplicate_of != candidate_id else None,
                        )
                    )
    return candidates


def _effective_trigger_test_count(
    *,
    candidates: Sequence[Candidate],
    signals: Mapping[str, np.ndarray],
    discovery_mask: np.ndarray,
    trading_days: np.ndarray,
) -> int:
    """Estimate independent tested trigger rules from discovery-day activation profiles."""
    discovery_days = np.asarray(trading_days)[discovery_mask]
    ordered_days = tuple(dict.fromkeys(str(value) for value in discovery_days))
    profiles: list[np.ndarray] = []
    for candidate in candidates:
        if candidate.horizon != "1h" or candidate.duplicate_of is not None:
            continue
        active = activation_mask(
            np.asarray(signals[candidate.expression])[discovery_mask],
            direction=candidate.direction,
            threshold=candidate.threshold,
        )
        profiles.append(
            np.asarray(
                [np.mean(active[discovery_days == day]) for day in ordered_days],
                dtype=np.float64,
            )
        )
    if not profiles:
        return 1
    activation_effective = effective_test_count(np.vstack(profiles))
    horizon_count = len({candidate.horizon for candidate in candidates})
    return max(1, min(len(candidates), activation_effective * max(1, horizon_count)))


def _discovery_trial_sharpe_std(
    rows: Sequence[Mapping[str, Any]],
    *,
    evaluated_only: bool = True,
) -> float:
    """Return discovery Sharpe dispersion with an explicit compatibility mode."""
    sharpes = np.asarray(
        [
            float(row.get("metrics", {}).get("clustered_sharpe", 0.0))
            for row in rows
            if (not evaluated_only or row.get("status") in {"passed", "killed"})
            and np.isfinite(float(row.get("metrics", {}).get("clustered_sharpe", 0.0)))
        ],
        dtype=np.float64,
    )
    return float(np.std(sharpes, ddof=1)) if sharpes.size > 1 else 0.0


def _profile_for_root(root: str) -> str:
    return "TXFD6" if root == "TXF" else "TMFD6"


_FINITE_FEATURE_HISTORY_BY_FAMILY: Mapping[str, Mapping[str, int]] = {
    "bidask": BIDASK_FEATURE_HISTORY_BARS,
    "kbar": KBAR_FEATURE_HISTORY_BARS,
    "tick": TICK_FEATURE_HISTORY_BARS,
}


def feature_history_bars_for_expression(family: str, expression: str) -> int | None:
    """Exact raw feature-bar history, or ``None`` for recursive SMMA state."""
    expression_history = required_history_by_variable(expression)
    if not expression_history:
        raise ValueError("candidate expression has no feature variables")
    if family == "smma":
        return None
    try:
        base_history = _FINITE_FEATURE_HISTORY_BY_FAMILY[family]
    except KeyError as exc:
        raise RunIntegrityError(f"feature-history metadata is unavailable for family {family!r}") from exc
    missing = sorted(set(expression_history) - set(base_history))
    if missing:
        raise RunIntegrityError(f"feature-history metadata is missing variables: {missing}")
    return max(int(required) + int(base_history[variable]) - 1 for variable, required in expression_history.items())


def _feature_grid_history_starts(
    reset_mask: Sequence[bool] | np.ndarray,
    *,
    feature_count: int,
    feature_history_bars: int | None,
) -> np.ndarray:
    starts: np.ndarray = np.full(feature_count, -1, dtype=np.int64)
    if feature_history_bars is None:
        return starts
    if int(feature_history_bars) < 1:
        raise ValueError("feature_history_bars must be positive when history is finite")
    resets = np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    if resets.size != feature_count:
        raise ValueError("feature reset mask must align with the feature grid")
    segment_start = 0
    for feature_index in range(feature_count):
        if bool(resets[feature_index]):
            segment_start = feature_index
        starts[feature_index] = max(segment_start, feature_index - int(feature_history_bars) + 1)
    return starts


def _exact_horizon_inputs(
    *,
    dataset: GovernedBars,
    candidate: Candidate,
    feature_bars: GovernedBars,
    feature_signal: np.ndarray,
    split_labels: np.ndarray,
    feature_history_bars: int | None = 1,
) -> tuple[GovernedBars, np.ndarray, np.ndarray, int, np.ndarray]:
    """Project coarse signals onto 60-minute bars when a true 1h horizon requires it."""
    signal = np.asarray(feature_signal, dtype=np.float64).reshape(-1)
    labels = np.asarray(split_labels).astype("<U18").reshape(-1)
    if len({len(feature_bars), signal.size, labels.size}) != 1:
        raise ValueError("feature bars, signal, and split labels must have identical lengths")
    history_starts = _feature_grid_history_starts(
        feature_bars.reset,
        feature_count=signal.size,
        feature_history_bars=feature_history_bars,
    )
    if candidate.horizon != "1h" or candidate.timeframe_min <= 60:
        return feature_bars, signal, labels, candidate.timeframe_min, history_starts

    if candidate.timeframe_min not in (120, 240):
        raise RunIntegrityError(f"cannot represent an exact 1h horizon for {candidate.timeframe_min}m features")
    execution_bars = dataset.group(candidate.root, 60)
    source_days = np.unique(feature_bars.trading_day)
    source_sessions = np.unique(feature_bars.session)
    mask = np.isin(execution_bars.trading_day, source_days)
    if not set(str(value) for value in source_sessions).issuperset({"day", "night"}):
        mask &= np.isin(execution_bars.session, source_sessions)
    execution_bars = _subset_bars(execution_bars, mask)

    labels_by_day: dict[str, str] = {}
    for day, label in zip(feature_bars.trading_day, labels, strict=True):
        day_text = str(day)
        label_text = str(label)
        prior = labels_by_day.setdefault(day_text, label_text)
        if prior != label_text:
            raise RunIntegrityError(f"split label changes within trading day {day_text}")
    execution_labels = np.asarray(
        [labels_by_day[str(day)] for day in execution_bars.trading_day],
        dtype="<U18",
    )

    minute_ns = 60 * 1_000_000_000
    aligned_signal: np.ndarray = np.full(len(execution_bars), np.nan, dtype=np.float64)
    aligned_history_starts: np.ndarray = np.full(len(execution_bars), -1, dtype=np.int64)
    feature_bucket_starts: np.ndarray = np.full(len(feature_bars), -1, dtype=np.int64)
    missing_buckets: list[int] = []
    for feature_index, value in enumerate(signal):
        bucket_start = int(feature_bars.ts_ns[feature_index])
        bucket_end = bucket_start + candidate.timeframe_min * minute_ns
        matching = np.flatnonzero(
            (execution_bars.trading_day == feature_bars.trading_day[feature_index])
            & (execution_bars.session == feature_bars.session[feature_index])
            & (execution_bars.contract == feature_bars.contract[feature_index])
            & (execution_bars.ts_ns >= bucket_start)
            & (execution_bars.ts_ns < bucket_end)
        )
        if matching.size == 0:
            missing_buckets.append(bucket_start)
            continue
        index = int(matching[-1])
        if np.isfinite(aligned_signal[index]):
            raise RunIntegrityError(f"multiple coarse bars map to 60m execution index {index}")
        aligned_signal[index] = value
        feature_bucket_starts[feature_index] = int(matching[0])
        source_feature_index = int(history_starts[feature_index])
        if source_feature_index >= 0:
            source_start = int(feature_bucket_starts[source_feature_index])
            if source_start < 0:
                raise RunIntegrityError("feature-history source bucket was not mapped to the execution grid")
            aligned_history_starts[index] = source_start
    if missing_buckets:
        raise RunIntegrityError(f"60m execution grid is missing coarse-bar bucket(s): {missing_buckets[:3]}")
    return execution_bars, aligned_signal, execution_labels, 60, aligned_history_starts


def _evaluate_candidate(
    candidate: Candidate,
    *,
    dataset: GovernedBars,
    bars: GovernedBars,
    signal: np.ndarray,
    split_name: str,
    split_labels: np.ndarray,
    evaluation_fraction: float = 1.0,
    cost_mode: str = "root_proxy",
) -> CandidateResult:
    bars, signal, split_labels, target_timeframe_min, _history_starts = _exact_horizon_inputs(
        dataset=dataset,
        candidate=candidate,
        feature_bars=bars,
        feature_signal=signal,
        split_labels=split_labels,
    )
    targets = forward_target_indices(
        horizon=candidate.horizon,
        timeframe_min=target_timeframe_min,
        split_labels=split_labels,
        reset_mask=bars.reset,
        session_close=bars.session_close,
    )
    target_returns = forward_returns(bars.close, targets)
    split_mask = split_labels == split_name
    masked_signal: np.ndarray = np.full(signal.size, np.nan, dtype=np.float64)
    split_indices = np.flatnonzero(split_mask)
    if not (0.0 < float(evaluation_fraction) <= 1.0):
        raise ValueError("evaluation_fraction must be in (0, 1]")
    recent_start = int(split_indices.size * (1.0 - float(evaluation_fraction)))
    recent_indices = split_indices[recent_start:]
    masked_signal[recent_indices] = signal[recent_indices]
    active = activation_mask(
        masked_signal,
        direction=candidate.direction,
        threshold=candidate.threshold,
    )
    if np.any(active):
        execution = simulate_next_bar_execution(
            signal=masked_signal,
            direction=candidate.direction,
            threshold=candidate.threshold,
            target_indices=targets,
            bid_open=bars.bid_open,
            ask_open=bars.ask_open,
            bid_close=bars.bid_close,
            ask_close=bars.ask_close,
            reset_mask=bars.reset,
            instrument_profile=_profile_for_root(candidate.root),
            contracts=bars.contract,
            cost_mode=cost_mode,
        )
    else:
        execution = ExecutionResult(
            trade_pnl=np.asarray([], dtype=np.float64),
            entry_indices=np.asarray([], dtype=np.int64),
            exit_indices=np.asarray([], dtype=np.int64),
            net_edge=0.0,
            net_sharpe=0.0,
            turnover=0.0,
        )
    horizon_step = (
        1
        if candidate.horizon == "session"
        else max(1, int(np.ceil((60 if candidate.horizon == "1h" else 240) / target_timeframe_min)))
    )
    kill = evaluate_recent_kill_criteria(
        signal=masked_signal[split_mask],
        direction=candidate.direction,
        target_returns=target_returns[split_mask],
        execution=execution,
        root=candidate.root,
        nonoverlap_step=horizon_step,
        trading_days=bars.trading_day,
        recent_fraction=evaluation_fraction,
        trade_activity_reason="no_executable_trades" if np.any(active) else "insufficient_trigger_activity",
    )
    return CandidateResult(
        candidate=candidate,
        kill=kill,
        stage=split_name,
        status="passed" if kill.passed else "killed",
        failure_reason=",".join(kill.reasons),
    )


_DISCOVERY_WORKER_CONTEXT: (
    tuple[
        GovernedBars,
        GovernedBars,
        Mapping[str, np.ndarray],
        np.ndarray,
        str,
    ]
    | None
) = None


def _initialize_discovery_worker(
    dataset: GovernedBars,
    bars: GovernedBars,
    signals: Mapping[str, np.ndarray],
    labels: np.ndarray,
    cost_mode: str,
) -> None:
    """Freeze one group's read-only inputs in each discovery worker."""
    global _DISCOVERY_WORKER_CONTEXT
    _DISCOVERY_WORKER_CONTEXT = (dataset, bars, signals, labels, cost_mode)


def _evaluate_discovery_worker(candidate: Candidate) -> CandidateResult:
    """Evaluate one candidate without sending group arrays with every task."""
    if _DISCOVERY_WORKER_CONTEXT is None:
        raise RuntimeError("discovery worker context was not initialized")
    dataset, bars, signals, labels, cost_mode = _DISCOVERY_WORKER_CONTEXT
    return _evaluate_candidate(
        candidate,
        dataset=dataset,
        bars=bars,
        signal=signals[candidate.expression],
        split_name="discovery",
        split_labels=labels,
        evaluation_fraction=0.25,
        cost_mode=cost_mode,
    )


@contextmanager
def _single_threaded_discovery_worker_environment() -> Iterator[None]:
    """Prevent each process worker from creating its own native thread pool."""
    prior = {name: os.environ.get(name) for name in DISCOVERY_NUMERIC_THREAD_ENV}
    os.environ.update(dict.fromkeys(DISCOVERY_NUMERIC_THREAD_ENV, "1"))
    try:
        yield
    finally:
        for name, value in prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _discovery_process_pool(
    *,
    workers: int,
    dataset: GovernedBars,
    bars: GovernedBars,
    signals: Mapping[str, np.ndarray],
    labels: np.ndarray,
    cost_mode: str,
    has_work: bool,
) -> Iterator[ProcessPoolExecutor | None]:
    if workers <= 1 or not has_work:
        yield None
        return
    with _single_threaded_discovery_worker_environment():
        with ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context(DISCOVERY_PROCESS_START_METHOD),
            initializer=_initialize_discovery_worker,
            initargs=(dataset, bars, signals, labels, cost_mode),
        ) as executor:
            yield executor


def _result_sort_key(result: CandidateResult) -> tuple[float, float, float, float, int, str]:
    return (
        -result.kill.net_edge,
        -result.kill.detrended_ic,
        -result.kill.net_sharpe,
        result.kill.turnover,
        result.candidate.complexity,
        result.candidate.candidate_id,
    )


def _signal_correlation(lhs: np.ndarray, rhs: np.ndarray) -> float:
    if lhs.size != rhs.size:
        return 1.0
    valid = np.isfinite(lhs) & np.isfinite(rhs)
    if int(np.count_nonzero(valid)) < 5:
        return 1.0
    left, right = lhs[valid], rhs[valid]
    if np.ptp(left) <= 1e-12 or np.ptp(right) <= 1e-12:
        return 1.0
    value = float(np.corrcoef(left, right)[0, 1])
    return abs(value) if np.isfinite(value) else 1.0


def _timestamp_signal_correlation(
    lhs_ts: np.ndarray,
    lhs: np.ndarray,
    rhs_ts: np.ndarray,
    rhs: np.ndarray,
) -> float:
    _common, lhs_indices, rhs_indices = np.intersect1d(
        np.asarray(lhs_ts, dtype=np.int64),
        np.asarray(rhs_ts, dtype=np.int64),
        assume_unique=True,
        return_indices=True,
    )
    return _signal_correlation(lhs[lhs_indices], rhs[rhs_indices])


def _subset_bars(bars: GovernedBars, mask: np.ndarray) -> GovernedBars:
    indices = np.flatnonzero(np.asarray(mask, dtype=np.bool_))
    if indices.size == 0:
        raise ValueError("robustness slice is empty")
    dataset_type = type(bars)
    return dataset_type(**{field: np.asarray(getattr(bars, field))[indices] for field in bars.__dataclass_fields__})


def evaluate_robustness_slices(
    dataset: GovernedBars,
    result: CandidateResult,
    *,
    evaluation_scope: str = "final_holdout",
    family: str = "smma",
    cost_mode: str = "root_proxy",
) -> dict[str, Any]:
    """Evaluate preregistered day-only and daily sensitivity slices."""
    if evaluation_scope not in {"final_holdout", "pre_final_screen"}:
        raise ValueError(f"unsupported robustness evaluation scope: {evaluation_scope}")
    candidate = result.candidate
    slices: dict[str, tuple[GovernedBars, Candidate]] = {}
    intraday = dataset.group(candidate.root, candidate.timeframe_min)
    day_only = _subset_bars(intraday, intraday.session == "day")
    slices["day_only"] = (day_only, candidate)
    daily = dataset.group(candidate.root, 1440)
    daily_candidate = Candidate(
        candidate_id=candidate.candidate_id,
        root=candidate.root,
        timeframe_min=1440,
        expression=candidate.expression,
        horizon="session",
        direction=candidate.direction,
        threshold=candidate.threshold,
        seed=candidate.seed,
        complexity=candidate.complexity,
        family=candidate.family,
        threshold_quantile=candidate.threshold_quantile,
        entry_rule_version=candidate.entry_rule_version,
        threshold_resolution=candidate.threshold_resolution,
        duplicate_of=candidate.duplicate_of,
    )
    slices["daily_sensitivity"] = (daily, daily_candidate)

    evidence: dict[str, Any] = {}
    for name, (bars, slice_candidate) in slices.items():
        plan = build_split_plan(bars.trading_day)
        if evaluation_scope == "final_holdout":
            split_labels = plan.labels
            split_name = "final_holdout"
        else:
            split_labels = np.where(plan.labels == "final_holdout", "excluded", "screen")
            split_name = "screen"
        adapter = FAMILY_REGISTRY[family]
        features = adapter.build_features_for_expression(bars, slice_candidate.expression)
        signal = adapter.evaluate_expression(slice_candidate.expression, features, bars.reset)
        evaluated = _evaluate_candidate(
            slice_candidate,
            dataset=dataset,
            bars=bars,
            signal=signal,
            split_name=split_name,
            split_labels=split_labels,
            cost_mode=cost_mode,
        )
        evidence[name] = {
            "root": slice_candidate.root,
            "timeframe_min": slice_candidate.timeframe_min,
            "horizon": slice_candidate.horizon,
            "evaluation_scope": evaluation_scope,
            "metrics": metrics_to_dict(evaluated.kill),
        }
    return evidence


def _current_rss_bytes() -> int:
    status_path = Path("/proc/self/status")
    try:
        for line in status_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage * 1024)


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def resource_decision(*, rss_bytes: int, output_bytes: int) -> str:
    if output_bytes >= OUTPUT_STOP_BYTES:
        return "stop_output_limit"
    if rss_bytes >= RSS_STOP_BYTES:
        return "stop_rss_limit"
    if rss_bytes >= RSS_PAUSE_BYTES:
        return "pause_rss"
    return "continue"


def apply_resource_policy() -> dict[str, Any]:
    """Best-effort CPU 0-13, nice=10, and idle IO policy for this process."""
    evidence: dict[str, Any] = {}
    try:
        allowed = sorted(os.sched_getaffinity(0))
        selected = set(allowed[:14])
        if selected:
            os.sched_setaffinity(0, selected)
        evidence["cpu_affinity"] = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError) as exc:
        evidence["cpu_affinity_error"] = str(exc)
    try:
        current_nice = os.nice(0)
        if current_nice < 10:
            os.nice(10 - current_nice)
        evidence["nice"] = os.nice(0)
    except OSError as exc:
        evidence["nice_error"] = str(exc)
    ionice = shutil.which("ionice")
    if ionice:
        result = subprocess.run(
            [ionice, "-c", "3", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            check=False,
        )
        evidence["ionice_exit_code"] = result.returncode
    return evidence


class MiningRun:
    def __init__(self, config: RunConfig) -> None:
        config.validate()
        self.config = config
        self.run_dir = config.run_dir.resolve()
        if self.run_dir in {Path("/"), Path.home().resolve(), Path.cwd().resolve()}:
            raise ValueError("run_dir must be a dedicated child directory, not a broad filesystem root")
        self.dataset_path = self.run_dir / "dataset.npz"
        self.manifest_path = self.run_dir / "run_manifest.json"
        self.checkpoint_path = self.run_dir / "checkpoint.json"
        self.heartbeat_path = self.run_dir / "heartbeat.json"
        self.report_path = self.run_dir / "report.json"
        self._ledger = HashChainLedger(self.run_dir / "trials.jsonl")
        self._unlocks = SplitUnlockGuard(self.run_dir / "split_access.jsonl")
        self._started_monotonic = time.monotonic()
        self._last_checkpoint = self._started_monotonic
        self._last_checkpoint_row_count = 0
        self._last_heartbeat = 0.0
        self._heartbeat_lock = threading.Lock()
        self._active_checkpoint_stage = "initialized"
        self._active_checkpoint_state: dict[str, Any] = {}
        self._terminal_stop_reason: str | None = None
        self._deadline_epoch_s: float | None = None
        self._effective_trial_counts: dict[str, int] = {}
        self._expression_supply: dict[str, dict[str, int]] = {}
        self._generation_evidence: dict[str, dict[str, Any]] = {}
        self._dataset_cache_evidence: dict[str, Any] = {"enabled": False, "hit": False}
        self._code_fingerprint = code_fingerprint()

    @property
    def _family_roots(self) -> tuple[str, ...]:
        """Iterate the active family's frozen roots, never the SMMA constant."""
        return FAMILY_REGISTRY[self.config.family].dataset.roots

    def _adaptive_search_budget(self) -> dict[str, int | str]:
        group_count = len(self._family_roots) * len(self.config.timeframes_minutes)
        expression_slots = self.config.max_candidates // (group_count * CANDIDATES_PER_EXPRESSION)
        feedback_slots = int(self.config.feedback_expressions_per_group)
        generation_zero_slots = expression_slots - feedback_slots
        allocated_candidates = expression_slots * group_count * CANDIDATES_PER_EXPRESSION
        return {
            "strategy": "discovery_feedback_v1",
            "groups": group_count,
            "candidate_variants_per_expression": CANDIDATES_PER_EXPRESSION,
            "generation_zero_expressions_per_group": generation_zero_slots,
            "feedback_expressions_per_group": feedback_slots,
            "allocated_candidate_ceiling": allocated_candidates,
            "unallocated_candidate_tail": self.config.max_candidates - allocated_candidates,
            "feedback_generator_version": FEEDBACK_GENERATOR_VERSION,
        }

    def _load_or_export_dataset(self) -> GovernedBars:
        family_dataset = FAMILY_REGISTRY[self.config.family].dataset
        if not self.dataset_path.exists():
            if self.config.resume:
                raise RunIntegrityError("resume requested but governed dataset is missing")
            export_arguments = {
                "code_fingerprint": self._code_fingerprint,
                "date_from": family_dataset.date_from,
                "date_to": family_dataset.date_to,
                "timeframes_minutes": self._required_dataset_timeframes(),
            }
            if self.config.dataset_cache_dir is None:
                family_dataset.export(self.dataset_path, **export_arguments)
            else:
                cache_dir = Path(self.config.dataset_cache_dir).resolve()
                cache_dir.mkdir(parents=True, exist_ok=True)
                trading_window = build_trading_date_window(
                    family_dataset.date_from,
                    family_dataset.date_to,
                )
                cache_key = _canonical_hash(
                    {
                        "dataset_kind": "tick" if self.config.family == "tick" else "bar",
                        "dataset_schema": (TICK_DATASET_SCHEMA if self.config.family == "tick" else DATASET_SCHEMA),
                        "date_from": family_dataset.date_from,
                        "date_to": family_dataset.date_to,
                        "calendar_name": trading_window.calendar_name,
                        "calendar_package_version": trading_window.calendar_package_version,
                        "calendar_mapping_hash": trading_window.calendar_mapping_hash,
                        "roots": family_dataset.roots,
                        "timeframes_minutes": self._required_dataset_timeframes(),
                        "code_fingerprint": self._code_fingerprint,
                    }
                )
                cached_dataset = cache_dir / f"{cache_key}.npz"
                cached_sidecar = Path(str(cached_dataset) + ".meta.json")
                if cached_dataset.exists() != cached_sidecar.exists():
                    raise RunIntegrityError(f"incomplete dataset cache entry: {cache_key}")
                cache_hit = cached_dataset.exists() and cached_sidecar.exists()
                if cache_hit:
                    family_dataset.load(cached_dataset)
                else:
                    family_dataset.export(cached_dataset, **export_arguments)
                shutil.copy2(cached_dataset, self.dataset_path)
                shutil.copy2(cached_sidecar, Path(str(self.dataset_path) + ".meta.json"))
                self._dataset_cache_evidence = {
                    "enabled": True,
                    "hit": cache_hit,
                    "cache_key": cache_key,
                }
        dataset = family_dataset.load(self.dataset_path)
        sidecar = json.loads(Path(str(self.dataset_path) + ".meta.json").read_text(encoding="utf-8"))
        if sidecar.get("code_fingerprint") != self._code_fingerprint:
            raise RunIntegrityError("governed dataset code fingerprint mismatch")
        return dataset

    def _dataset_fingerprint(self) -> str:
        sidecar = json.loads(Path(str(self.dataset_path) + ".meta.json").read_text(encoding="utf-8"))
        return str(sidecar["content_sha256"])

    def _required_dataset_timeframes(self) -> tuple[int, ...]:
        required = {int(value) for value in self.config.timeframes_minutes}
        required.update((60, 1440))
        return tuple(sorted(required))

    def _validate_frozen_dataset_scope(self, dataset: GovernedBars) -> None:
        family_dataset = FAMILY_REGISTRY[self.config.family].dataset
        sidecar = json.loads(Path(str(self.dataset_path) + ".meta.json").read_text(encoding="utf-8"))
        if int(sidecar.get("schema_version", 0)) < 2 or sidecar.get("governance_complete") is not True:
            raise RunIntegrityError("new mining runs require a complete governed dataset schema v2 export")
        required_governance_fields = {
            "calendar_name",
            "calendar_package_version",
            "calendar_mapping_hash",
            "expected_trading_dates",
            "eligible_trading_dates",
            "missing_expected_trading_dates",
            "excluded_partial_trading_dates",
            "query_wall_time_from",
            "query_wall_time_to",
        }
        missing_fields = required_governance_fields - set(sidecar)
        if missing_fields:
            raise RunIntegrityError(f"governed dataset v2 evidence is incomplete: {sorted(missing_fields)}")
        if (
            sidecar.get("requested_date_from") != family_dataset.date_from
            or sidecar.get("requested_date_to") != family_dataset.date_to
        ):
            raise RunIntegrityError("governed dataset query date range does not match frozen contract")
        if set(str(value) for value in np.unique(dataset.root)) != set(family_dataset.roots):
            raise RunIntegrityError("governed dataset must contain both TXF and TMF")
        required_timeframes = set(self._required_dataset_timeframes())
        if set(int(value) for value in np.unique(dataset.timeframe_min)) != required_timeframes:
            raise RunIntegrityError(
                f"governed dataset must contain exactly the configured execution/feature bars: "
                f"{sorted(required_timeframes)}"
            )
        evidence = sidecar.get("query_evidence")
        if not isinstance(evidence, list):
            raise RunIntegrityError("governed dataset query evidence is missing")
        guarded_timeframes = {
            int(item["timeframe_min"])
            for item in evidence
            if isinstance(item, Mapping) and item.get("guard_overall") == "pass" and "timeframe_min" in item
        }
        if guarded_timeframes != required_timeframes:
            raise RunIntegrityError("governed dataset query evidence is incomplete")
        expected_days = set(str(value) for value in np.unique(dataset.trading_day))
        eligible_days = {str(value) for value in sidecar["eligible_trading_dates"]}
        if expected_days != eligible_days or int(sidecar.get("trading_day_count", -1)) != len(eligible_days):
            raise RunIntegrityError("dataset rows do not match sidecar eligible trading dates")
        for root in family_dataset.roots:
            for timeframe in self._required_dataset_timeframes():
                group_days = set(str(value) for value in np.unique(dataset.group(root, timeframe).trading_day))
                if group_days != expected_days:
                    raise RunIntegrityError(f"governed dataset trading-day coverage differs for {root}/{timeframe}m")
        coverage = self._cost_profile_coverage(dataset)
        if self.config.cost_mode == "per_contract" and coverage["missing_contracts"]:
            raise RunIntegrityError(
                f"per-contract cost preflight failed; missing frozen profiles: {coverage['missing_contracts']}"
            )

    @staticmethod
    def _cost_profile_coverage(dataset: GovernedBars) -> dict[str, Any]:
        observed = sorted(str(value) for value in np.unique(dataset.contract))
        available: list[str] = []
        missing: list[str] = []
        for contract in observed:
            try:
                load_cost_profile(contract)
            except KeyError:
                missing.append(contract)
            else:
                available.append(contract)
        return {
            "observed_contracts": observed,
            "profiled_contracts": available,
            "missing_contracts": missing,
            "complete": not missing,
        }

    def _manifest_identity(self, dataset: GovernedBars) -> dict[str, Any]:
        global_plan = build_split_plan(dataset.trading_day)
        dataset_sidecar = json.loads(Path(str(self.dataset_path) + ".meta.json").read_text(encoding="utf-8"))
        trading_day_count = len(set(str(value) for value in dataset.trading_day))
        warnings: list[str] = []
        if trading_day_count < MIN_DAYS_FOR_PROMOTION:
            warnings.append(
                f"only {trading_day_count} trading days; achievable verdict is capped below promotion "
                f"until {MIN_DAYS_FOR_PROMOTION} days"
            )
        feature_history_exact = self.config.family in _FINITE_FEATURE_HISTORY_BY_FAMILY
        if not feature_history_exact:
            warnings.append(
                "SMMA recursive/global normalizer history has no finite exact lookback; "
                "Validation-v3 locked walk-forward is fail-closed for this family"
            )
        edge_thresholds: dict[str, dict[str, float | str]] = {}
        cost_coverage = self._cost_profile_coverage(dataset)
        for root in FAMILY_REGISTRY[self.config.family].dataset.roots:
            profile_name = _profile_for_root(root)
            profile = load_cost_profile(profile_name)
            minimum_points = float(MINIMUM_EDGE_POINTS[root])
            strict_points = float(STRICT_EDGE_TARGET_POINTS[root])
            edge_thresholds[root] = {
                "cost_profile": profile_name,
                "minimum_edge_points": minimum_points,
                "minimum_edge_nwd": minimum_points * profile.point_value_nwd,
                "minimum_edge_rt_cost_multiple": minimum_points / profile.rt_cost_pts,
                "strict_edge_target_points": strict_points,
                "strict_edge_target_nwd": strict_points * profile.point_value_nwd,
                "strict_edge_rt_cost_multiple": strict_points / profile.rt_cost_pts,
            }
        return {
            "schema": RUN_SCHEMA,
            "family": self.config.family,
            "wall_time_hours": float(self.config.wall_time_hours),
            "max_candidates": int(self.config.max_candidates),
            "workers": int(self.config.workers),
            "discovery_executor": (
                "single_process" if self.config.workers == 1 else f"process_pool:{DISCOVERY_PROCESS_START_METHOD}"
            ),
            "discovery_worker_numeric_threads": 1 if self.config.workers > 1 else None,
            "seeds": list(self.config.seeds),
            "dataset_fingerprint": self._dataset_fingerprint(),
            "dataset_date_from": FAMILY_REGISTRY[self.config.family].dataset.date_from,
            "dataset_date_to": FAMILY_REGISTRY[self.config.family].dataset.date_to,
            "dataset_governance": {
                "schema": dataset_sidecar.get("schema"),
                "calendar_name": dataset_sidecar.get("calendar_name"),
                "calendar_package_version": dataset_sidecar.get("calendar_package_version"),
                "calendar_mapping_hash": dataset_sidecar.get("calendar_mapping_hash"),
                "expected_trading_day_count": len(dataset_sidecar.get("expected_trading_dates", ())),
                "eligible_trading_day_count": len(dataset_sidecar.get("eligible_trading_dates", ())),
                "missing_expected_trading_dates": dataset_sidecar.get("missing_expected_trading_dates", ()),
                "excluded_partial_trading_dates": dataset_sidecar.get("excluded_partial_trading_dates", ()),
            },
            "code_fingerprint": self._code_fingerprint,
            "split_hash": global_plan.split_hash,
            "split_assignments": dict(global_plan.assignments),
            "roots": list(FAMILY_REGISTRY[self.config.family].dataset.roots),
            "timeframes_minutes": list(self.config.timeframes_minutes),
            "dataset_timeframes_minutes": list(self._required_dataset_timeframes()),
            "trading_day_count": trading_day_count,
            "achievable_verdict_ceiling": (
                "DISCOVERY_SELECTION_ONLY"
                if not feature_history_exact
                else ("NEEDS_MORE_DAYS" if trading_day_count < MIN_DAYS_FOR_PROMOTION else "SCREEN_ONLY")
            ),
            "validation_v3_feature_history_eligible": feature_history_exact,
            "startup_warnings": warnings,
            "edge_thresholds": edge_thresholds,
            "cost_mode": self.config.cost_mode,
            "cost_profile_coverage": cost_coverage,
            "cost_claim_eligible": bool(
                self.config.cost_mode == "per_contract"
                and cost_coverage["complete"]
                and not self.config.posthoc_diagnostic
            ),
            "cost_profile_resolution": (
                "entry-contract profile"
                if self.config.cost_mode == "per_contract"
                else "canonical D6 root proxy; diagnostic only and cost-claim-ineligible"
            ),
            "smma_lengths": list(self.config.smma_lengths),
            "robustness_timeframes_minutes": [1440],
            "horizons": ["1h", "4h", "session"],
            "threshold_quantiles": list(THRESHOLD_QUANTILES),
            "entry_rule_version": ENTRY_RULE_VERSION,
            "entry_comparator": ">=",
            "directions": [1, -1],
            "search_strategy": ("discovery_feedback_v1" if self.config.feedback_expressions_per_group else "blind_v1"),
            "feedback_expressions_per_group": int(self.config.feedback_expressions_per_group),
            "adaptive_search_budget": (
                self._adaptive_search_budget() if self.config.feedback_expressions_per_group else None
            ),
            "screen_only": True,
            "posthoc_diagnostic": bool(self.config.posthoc_diagnostic),
            "final_holdout_unlocked": bool(self.config.unlock_final_holdout),
            "final_holdout_claim_eligible": bool(self.config.unlock_final_holdout)
            and not self.config.posthoc_diagnostic,
        }

    def _ensure_manifest(self, dataset: GovernedBars, resource_policy: Mapping[str, Any]) -> dict[str, Any]:
        identity = self._manifest_identity(dataset)
        if self.manifest_path.exists():
            existing = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            _verify_integrity_hash(existing, "manifest_hash", artifact="run manifest")
            existing_identity = dict(existing)
            existing_identity.pop("created_at", None)
            existing_identity.pop("resource_policy", None)
            existing_identity.pop("manifest_hash", None)
            if existing_identity != identity:
                raise RunIntegrityError("resume manifest/data/code/split fingerprint mismatch")
            if not self.config.resume:
                raise RunIntegrityError("run manifest already exists; pass --resume or choose a new run-dir")
            self._deadline_epoch_s = (
                datetime.fromisoformat(str(existing["created_at"])).timestamp() + self.config.wall_time_hours * 3600.0
            )
            return existing
        if self.config.resume:
            raise RunIntegrityError("resume requested but run manifest is missing")
        partial_artifacts = [
            self.checkpoint_path,
            self.heartbeat_path,
            self.report_path,
            self.run_dir / "trials.jsonl",
            self.run_dir / "split_access.jsonl",
        ]
        if any(path.exists() for path in partial_artifacts):
            raise RunIntegrityError("partial run artifacts exist without an immutable manifest")
        created_at = datetime.now(UTC)
        manifest = {
            **identity,
            "created_at": created_at.isoformat(),
            "resource_policy": dict(resource_policy),
        }
        manifest = _with_integrity_hash(manifest, "manifest_hash")
        self._deadline_epoch_s = created_at.timestamp() + self.config.wall_time_hours * 3600.0
        self.run_dir.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(self.manifest_path, flags, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        return manifest

    def _heartbeat(self, *, stage: str, force: bool = False) -> None:
        with self._heartbeat_lock:
            now = time.monotonic()
            if not force and (now - self._last_heartbeat) < HEARTBEAT_SECONDS:
                return
            heartbeat = _with_integrity_hash(
                {
                    "schema": "alpha_mining_heartbeat.v2",
                    "stage": stage,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "trials": self._ledger.unique_candidates,
                    "ledger_rows": self._ledger.row_count,
                    "rss_bytes": _current_rss_bytes(),
                    "output_bytes": _directory_size(self.run_dir),
                },
                "heartbeat_hash",
            )
            _atomic_json(self.heartbeat_path, heartbeat)
            self._last_heartbeat = now

    def _checkpoint(self, *, stage: str, state: Mapping[str, Any], force: bool = False) -> None:
        self._active_checkpoint_stage = stage
        self._active_checkpoint_state = dict(state)
        now = time.monotonic()
        ledger_rows = self._ledger.row_count
        due_trials = ledger_rows - self._last_checkpoint_row_count >= CHECKPOINT_TRIALS
        if not force and not due_trials and (now - self._last_checkpoint) < CHECKPOINT_SECONDS:
            return
        payload = {
            "schema": CHECKPOINT_SCHEMA,
            "stage": stage,
            "dataset_fingerprint": self._dataset_fingerprint(),
            "code_fingerprint": self._code_fingerprint,
            "trials": self._ledger.unique_candidates,
            "ledger_rows": ledger_rows,
            "state": dict(state),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        payload = _with_integrity_hash(payload, "checkpoint_hash")
        _atomic_json(self.checkpoint_path, payload)
        self._last_checkpoint = now
        self._last_checkpoint_row_count = ledger_rows

    def _load_checkpoint(self) -> dict[str, Any]:
        if not self.checkpoint_path.exists():
            return {}
        payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        _verify_integrity_hash(payload, "checkpoint_hash", artifact="checkpoint")
        if payload.get("dataset_fingerprint") != self._dataset_fingerprint():
            raise RunIntegrityError("checkpoint dataset fingerprint mismatch")
        if payload.get("code_fingerprint") != self._code_fingerprint:
            raise RunIntegrityError("checkpoint code fingerprint mismatch")
        checkpoint_rows = int(payload.get("ledger_rows", payload.get("trials", 0)))
        if self._ledger.row_count < checkpoint_rows:
            raise RunIntegrityError("trial ledger is shorter than the durable checkpoint")
        checkpoint_trials = int(payload.get("trials", 0))
        if self._ledger.unique_candidates < checkpoint_trials:
            raise RunIntegrityError("trial ledger has fewer discovery candidates than the durable checkpoint")
        self._active_checkpoint_stage = str(payload.get("stage", "initialized"))
        self._last_checkpoint_row_count = checkpoint_rows
        state = payload.get("state")
        self._active_checkpoint_state = dict(state) if isinstance(state, Mapping) else {}
        return payload

    def _wall_time_reached(self) -> bool:
        return (
            time.time() >= self._deadline_epoch_s
            if self._deadline_epoch_s is not None
            else (time.monotonic() - self._started_monotonic) >= self.config.wall_time_hours * 3600.0
        )

    def _stop_reason(self, *, include_candidate_cap: bool = True) -> str | None:
        if self._wall_time_reached():
            return "wall_time"
        if include_candidate_cap and self._ledger.unique_candidates >= self.config.max_candidates:
            return "max_candidates"
        decision = resource_decision(
            rss_bytes=_current_rss_bytes(),
            output_bytes=_directory_size(self.run_dir),
        )
        if decision.startswith("stop_"):
            return decision
        if decision == "pause_rss":
            self._checkpoint(
                stage=self._active_checkpoint_stage,
                state=self._active_checkpoint_state,
                force=True,
            )
            while _current_rss_bytes() >= RSS_PAUSE_BYTES:
                self._heartbeat(stage="paused_rss", force=True)
                time.sleep(min(60.0, HEARTBEAT_SECONDS))
                if self._wall_time_reached():
                    return "wall_time"
                if _current_rss_bytes() >= RSS_STOP_BYTES:
                    return "stop_rss_limit"
        return None

    def _build_group_context(
        self,
        dataset: GovernedBars,
        root: str,
        timeframe: int,
        expression_limit: int,
        seed: int,
        *,
        record_effective_trials: bool = True,
        semantic_dedupe: bool = False,
    ) -> tuple[GovernedBars, np.ndarray, dict[str, np.ndarray], list[str], list[Candidate]]:
        bars = dataset.group(root, timeframe)
        plan = build_split_plan(bars.trading_day)
        discovery_mask = plan.mask("discovery")
        adapter = FAMILY_REGISTRY[self.config.family]
        features = adapter.build_features(bars, self.config)
        expected_history = _FINITE_FEATURE_HISTORY_BY_FAMILY.get(self.config.family)
        if expected_history is not None and set(features) != set(expected_history):
            missing = sorted(set(features) - set(expected_history))
            stale = sorted(set(expected_history) - set(features))
            raise RunIntegrityError(
                f"feature-history metadata does not match exported features: missing={missing}, stale={stale}"
            )
        usable_feature_names = [
            name
            for name in sorted(features)
            if validate_stationary_signal(adapter.evaluate_expression(name, features, bars.reset)[discovery_mask])[0]
        ]
        expressions = generated_gp_expressions(
            usable_feature_names,
            seed=seed,
            limit=expression_limit * 3,
        )
        valid_expressions: list[str] = []
        signals: dict[str, np.ndarray] = {}
        feature_history_rejected = 0
        semantic_duplicate_rejected = 0
        semantic_hashes: set[str] = set()
        for expression in expressions:
            try:
                feature_history_bars_for_expression(self.config.family, expression)
            except (KeyError, TypeError, ValueError, SyntaxError):
                feature_history_rejected += 1
                continue
            if semantic_dedupe:
                expression_semantic_hash = canonical_hash(expression)
                if expression_semantic_hash in semantic_hashes:
                    semantic_duplicate_rejected += 1
                    continue
                semantic_hashes.add(expression_semantic_hash)
            try:
                signal = adapter.evaluate_expression(expression, features, bars.reset)
            except (KeyError, TypeError, ValueError, SyntaxError):
                continue
            valid, _reason = validate_stationary_signal(signal[discovery_mask])
            if valid:
                valid_expressions.append(expression)
                signals[expression] = signal
                if len(valid_expressions) >= expression_limit:
                    break
        candidates = enumerate_candidates(
            family=self.config.family,
            root=root,
            timeframe_min=timeframe,
            expressions=valid_expressions,
            signals=signals,
            discovery_mask=discovery_mask,
            seed=seed,
        )
        if record_effective_trials:
            self._effective_trial_counts[f"{root}/{timeframe}"] = _effective_trigger_test_count(
                candidates=candidates,
                signals=signals,
                discovery_mask=discovery_mask,
                trading_days=bars.trading_day,
            )
        self._expression_supply[f"{root}/{timeframe}"] = {
            "requested": int(expression_limit),
            "valid": len(valid_expressions),
            "feature_history_rejected": feature_history_rejected,
            "semantic_duplicate_rejected": semantic_duplicate_rejected,
        }
        return bars, plan.labels, signals, valid_expressions, candidates

    def _append_generation_evidence(self, payload: Mapping[str, Any]) -> None:
        expected = dict(payload)
        candidate_id = str(expected["candidate_id"])
        prior = next(
            (row for row in self._ledger.rows(stage="generation") if str(row.get("candidate_id")) == candidate_id),
            None,
        )
        if prior is not None:
            comparable = dict(prior)
            comparable.pop("previous_hash", None)
            if comparable != expected:
                raise RunIntegrityError("adaptive generation evidence changed during resume")
            return
        if not self._ledger.append(expected):
            raise RunIntegrityError("adaptive generation evidence could not be appended")

    def _record_generation_zero(
        self,
        *,
        root: str,
        timeframe: int,
        seed: int,
        expressions: Sequence[str],
        candidates: Sequence[Candidate],
    ) -> dict[str, str]:
        candidate_ids_by_expression: dict[str, list[str]] = {}
        for candidate in candidates:
            candidate_ids_by_expression.setdefault(candidate.expression, []).append(candidate.candidate_id)
        proposal_ids: dict[str, str] = {}
        for ordinal, expression in enumerate(expressions):
            semantic_hash = canonical_hash(expression)
            proposal_id = _canonical_hash(
                {
                    "schema": "alpha_mining_generation_proposal.v1",
                    "family": self.config.family,
                    "root": root,
                    "timeframe_min": timeframe,
                    "generation": 0,
                    "ordinal": ordinal,
                    "seed": int(seed),
                    "semantic_hash": semantic_hash,
                }
            )
            payload = {
                "candidate_id": proposal_id,
                "stage": "generation",
                "status": "accepted",
                "event": "expression_proposal",
                "search_generation": 0,
                "family": self.config.family,
                "root": root,
                "timeframe_min": timeframe,
                "ordinal": ordinal,
                "seed": int(seed),
                "generator_version": "deterministic_gp_v1",
                "expression": expression,
                "semantic_hash": semantic_hash,
                "parent_candidate_ids": [],
                "parent_expressions": [],
                "candidate_ids": candidate_ids_by_expression.get(expression, []),
                "rejection_reason": "",
            }
            self._append_generation_evidence(payload)
            proposal_ids[expression] = proposal_id
        return proposal_ids

    def _generation_zero_parents(self, *, root: str, timeframe: int) -> list[CandidateResult]:
        generation_zero = [
            _result_from_kill_ledger_row(row)
            for row in self._ledger.rows(stage="discovery")
            if row.get("status") == "passed"
            and int(row.get("search_generation", 0)) == 0
            and str(row.get("candidate", {}).get("root")) == root
            and int(row.get("candidate", {}).get("timeframe_min", -1)) == timeframe
        ]
        distinct: list[CandidateResult] = []
        seen_semantics: set[str] = set()
        for result in sorted(generation_zero, key=_result_sort_key):
            semantic_hash = canonical_hash(result.candidate.expression)
            if semantic_hash in seen_semantics:
                continue
            seen_semantics.add(semantic_hash)
            distinct.append(result)
        return distinct

    def _build_feedback_context(
        self,
        *,
        root: str,
        timeframe: int,
        seed: int,
        bars: GovernedBars,
        labels: np.ndarray,
        generation_zero_expressions: Sequence[str],
        feedback_limit: int,
    ) -> tuple[dict[str, np.ndarray], list[str], list[Candidate], dict[str, str]]:
        group_key = f"{root}/{timeframe}"
        parents = self._generation_zero_parents(root=root, timeframe=timeframe)
        parent_by_expression = {result.candidate.expression: result for result in parents}
        parent_expressions = [result.candidate.expression for result in parents]
        rejection_counts: Counter[str] = Counter()
        accepted_expressions: list[str] = []
        signals: dict[str, np.ndarray] = {}
        candidates: list[Candidate] = []
        proposal_ids: dict[str, str] = {}
        adapter = FAMILY_REGISTRY[self.config.family]
        features = adapter.build_features(bars, self.config)
        discovery_mask = labels == "discovery"
        max_attempts = max(
            MIN_FEEDBACK_ATTEMPTS,
            int(feedback_limit) * FEEDBACK_ATTEMPTS_PER_EXPRESSION,
        )
        proposals = generated_feedback_proposals(
            parent_expressions,
            seed=seed,
            excluded_semantic_hashes=tuple(canonical_hash(item) for item in generation_zero_expressions),
            max_attempts=max_attempts,
        )
        for proposal in proposals:
            parent_candidate_ids = [
                parent_by_expression[parent].candidate.candidate_id for parent in proposal.parent_expressions
            ]
            proposal_id = _canonical_hash(
                {
                    "schema": "alpha_mining_generation_proposal.v1",
                    "family": self.config.family,
                    "root": root,
                    "timeframe_min": timeframe,
                    "generation": 1,
                    "attempt": proposal.attempt,
                    "attempt_seed": proposal.attempt_seed,
                    "semantic_hash": proposal.semantic_hash,
                    "parent_candidate_ids": parent_candidate_ids,
                }
            )
            status = proposal.generator_status
            reason = proposal.rejection_reason
            child_candidates: list[Candidate] = []
            signal: np.ndarray | None = None
            if status == "candidate":
                try:
                    feature_history_bars_for_expression(self.config.family, proposal.expression)
                    signal = adapter.evaluate_expression(proposal.expression, features, bars.reset)
                    valid, stationarity_reason = validate_stationary_signal(signal[discovery_mask])
                    if not valid:
                        status = "rejected"
                        reason = f"discovery_{stationarity_reason}"
                except (KeyError, TypeError, ValueError, SyntaxError) as exc:
                    status = "rejected"
                    reason = f"invalid_feedback_expression:{type(exc).__name__}"
            if status == "candidate" and signal is not None:
                child_candidates = enumerate_candidates(
                    family=self.config.family,
                    root=root,
                    timeframe_min=timeframe,
                    expressions=[proposal.expression],
                    signals={proposal.expression: signal},
                    discovery_mask=discovery_mask,
                    seed=seed,
                )
                status = "accepted"
                accepted_expressions.append(proposal.expression)
                signals[proposal.expression] = signal
                candidates.extend(child_candidates)
                proposal_ids[proposal.expression] = proposal_id
            else:
                rejection_counts[reason or "rejected"] += 1
            self._append_generation_evidence(
                {
                    "candidate_id": proposal_id,
                    "stage": "generation",
                    "status": status,
                    "event": "expression_proposal",
                    "search_generation": 1,
                    "family": self.config.family,
                    "root": root,
                    "timeframe_min": timeframe,
                    "ordinal": len(accepted_expressions) - 1 if status == "accepted" else None,
                    "attempt": proposal.attempt,
                    "seed": int(seed),
                    "attempt_seed": proposal.attempt_seed,
                    "generator_version": FEEDBACK_GENERATOR_VERSION,
                    "expression": proposal.expression,
                    "semantic_hash": proposal.semantic_hash,
                    "parent_candidate_ids": parent_candidate_ids,
                    "parent_expressions": list(proposal.parent_expressions),
                    "candidate_ids": [candidate.candidate_id for candidate in child_candidates],
                    "rejection_reason": reason,
                }
            )
            if len(accepted_expressions) >= int(feedback_limit):
                break
        closure_status = "closed" if len(accepted_expressions) == int(feedback_limit) else "closed_with_unused_budget"
        closure_reason = ""
        if len(parents) < 2:
            closure_reason = "insufficient_generation0_parent_diversity"
        elif len(accepted_expressions) < int(feedback_limit):
            closure_reason = "insufficient_novel_feedback_supply"
        closure_id = _canonical_hash(
            {
                "schema": "alpha_mining_generation_closure.v1",
                "family": self.config.family,
                "root": root,
                "timeframe_min": timeframe,
                "generation": 1,
                "seed": int(seed),
                "requested": int(feedback_limit),
            }
        )
        self._append_generation_evidence(
            {
                "candidate_id": closure_id,
                "stage": "generation",
                "status": closure_status,
                "event": "generation_closure",
                "search_generation": 1,
                "family": self.config.family,
                "root": root,
                "timeframe_min": timeframe,
                "seed": int(seed),
                "generator_version": FEEDBACK_GENERATOR_VERSION,
                "requested_expressions": int(feedback_limit),
                "accepted_expressions": len(accepted_expressions),
                "unused_expressions": int(feedback_limit) - len(accepted_expressions),
                "parent_expressions": len(parents),
                "rejection_counts": dict(sorted(rejection_counts.items())),
                "closure_reason": closure_reason,
                "candidate_ids": [candidate.candidate_id for candidate in candidates],
            }
        )
        self._generation_evidence[group_key] = {
            "generation_zero_parent_expressions": len(parents),
            "feedback_requested": int(feedback_limit),
            "feedback_accepted": len(accepted_expressions),
            "feedback_unused": int(feedback_limit) - len(accepted_expressions),
            "feedback_attempts": len(
                [
                    row
                    for row in self._ledger.rows(stage="generation")
                    if row.get("root") == root
                    and int(row.get("timeframe_min", -1)) == timeframe
                    and int(row.get("search_generation", -1)) == 1
                    and row.get("event") == "expression_proposal"
                ]
            ),
            "feedback_rejection_counts": dict(sorted(rejection_counts.items())),
            "closure_reason": closure_reason,
        }
        return signals, accepted_expressions, candidates, proposal_ids

    def _evaluate_discovery_candidates(
        self,
        *,
        dataset: GovernedBars,
        bars: GovernedBars,
        labels: np.ndarray,
        signals: Mapping[str, np.ndarray],
        candidates: Sequence[Candidate],
        passing: list[CandidateResult],
        search_generation: int,
        proposal_ids: Mapping[str, str],
    ) -> str | None:
        remaining = [
            candidate for candidate in candidates if not self._ledger.has(candidate.candidate_id, "discovery")
        ][: max(0, self.config.max_candidates - self._ledger.unique_candidates)]

        def evaluate(candidate: Candidate) -> CandidateResult:
            return _evaluate_candidate(
                candidate,
                dataset=dataset,
                bars=bars,
                signal=signals[candidate.expression],
                split_name="discovery",
                split_labels=labels,
                evaluation_fraction=0.25,
                cost_mode=self.config.cost_mode,
            )

        stop_reason: str | None = None
        batch_size = max(1, self.config.workers * 2)
        with _discovery_process_pool(
            workers=self.config.workers,
            dataset=dataset,
            bars=bars,
            signals=signals,
            labels=labels,
            cost_mode=self.config.cost_mode,
            has_work=any(candidate.duplicate_of is None for candidate in remaining),
        ) as executor:
            for batch_start in range(0, len(remaining), batch_size):
                batch = remaining[batch_start : batch_start + batch_size]
                evaluated_batch = [candidate for candidate in batch if candidate.duplicate_of is None]
                evaluated_results = (
                    (evaluate(candidate) for candidate in evaluated_batch)
                    if executor is None
                    else executor.map(_evaluate_discovery_worker, evaluated_batch, chunksize=1)
                )
                results_by_id = {result.candidate.candidate_id: result for result in evaluated_results}
                for candidate in batch:
                    common = {
                        "search_generation": int(search_generation),
                        "generation_proposal_id": proposal_ids[candidate.expression],
                    }
                    if candidate.duplicate_of is not None:
                        self._ledger.append(
                            {
                                "candidate_id": candidate.candidate_id,
                                "stage": "discovery",
                                "status": "deduplicated",
                                "candidate": asdict(candidate),
                                **common,
                                "failure_reason": "exact_resolved_cut_duplicate",
                                "reference_candidate_id": candidate.duplicate_of,
                                "threshold_resolution": (
                                    asdict(candidate.threshold_resolution)
                                    if candidate.threshold_resolution is not None
                                    else None
                                ),
                                "recorded_at_ns": timebase.now_ns(),
                            }
                        )
                    else:
                        result = results_by_id[candidate.candidate_id]
                        self._ledger.append(
                            {
                                "candidate_id": result.candidate.candidate_id,
                                "stage": "discovery",
                                "status": result.status,
                                "candidate": asdict(result.candidate),
                                **common,
                                "metrics": metrics_to_dict(result.kill),
                                "threshold_resolution": (
                                    asdict(result.candidate.threshold_resolution)
                                    if result.candidate.threshold_resolution is not None
                                    else None
                                ),
                                "failure_reason": result.failure_reason,
                                "recorded_at_ns": timebase.now_ns(),
                            }
                        )
                        if result.kill.passed:
                            passing.append(result)
                    self._heartbeat(stage="discovery")
                    self._checkpoint(
                        stage="discovery",
                        state={"discovery_passed": [item.to_dict() for item in passing]},
                    )
                    stop_reason = self._stop_reason()
                    if stop_reason:
                        if stop_reason != "max_candidates":
                            self._terminal_stop_reason = stop_reason
                        break
                if stop_reason:
                    break
        return stop_reason

    def _validate_adaptive_lineage(self) -> None:
        """Require every adaptive discovery row to match one accepted proposal."""
        accepted = [
            row
            for row in self._ledger.rows(stage="generation")
            if row.get("status") == "accepted" and row.get("event") == "expression_proposal"
        ]
        owners: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for proposal in accepted:
            proposal_id = str(proposal.get("candidate_id", ""))
            candidate_ids = proposal.get("candidate_ids")
            if not proposal_id or not isinstance(candidate_ids, list) or not candidate_ids:
                raise RunIntegrityError("accepted adaptive proposal has no candidate lineage")
            for raw_candidate_id in candidate_ids:
                candidate_id = str(raw_candidate_id)
                if candidate_id in owners:
                    raise RunIntegrityError("adaptive candidate belongs to multiple generation proposals")
                owners[candidate_id] = (proposal_id, proposal)

        discovery_rows = self._ledger.rows(stage="discovery")
        discovery_ids = {str(row.get("candidate_id", "")) for row in discovery_rows}
        if set(owners) != discovery_ids:
            raise RunIntegrityError("adaptive generation/discovery lineage does not conserve candidate IDs")

        generation_zero_passes = {
            str(row.get("candidate_id", ""))
            for row in discovery_rows
            if int(row.get("search_generation", -1)) == 0 and row.get("status") == "passed"
        }
        for proposal in accepted:
            generation = int(proposal.get("search_generation", -1))
            parent_ids = [str(value) for value in proposal.get("parent_candidate_ids", ())]
            if generation == 0 and parent_ids:
                raise RunIntegrityError("generation-0 proposal unexpectedly declares parents")
            if generation == 1 and (
                len(parent_ids) != 2 or any(item not in generation_zero_passes for item in parent_ids)
            ):
                raise RunIntegrityError("generation-1 proposal parents are not generation-0 discovery passes")

        for row in discovery_rows:
            candidate_id = str(row.get("candidate_id", ""))
            proposal_id, owned_proposal = owners[candidate_id]
            generation = int(owned_proposal.get("search_generation", -1))
            if str(row.get("generation_proposal_id", "")) != proposal_id:
                raise RunIntegrityError("adaptive discovery row links to the wrong generation proposal")
            if int(row.get("search_generation", -1)) != generation:
                raise RunIntegrityError("adaptive discovery row has the wrong search generation")
            candidate = row.get("candidate")
            if not isinstance(candidate, Mapping) or str(candidate.get("candidate_id", "")) != candidate_id:
                raise RunIntegrityError("adaptive discovery row candidate payload does not match its identity")
            expected_fields = {
                "family": str(owned_proposal.get("family", "")),
                "root": str(owned_proposal.get("root", "")),
                "timeframe_min": int(owned_proposal.get("timeframe_min", -1)),
                "expression": str(owned_proposal.get("expression", "")),
            }
            actual_fields = {
                "family": str(candidate.get("family", "")),
                "root": str(candidate.get("root", "")),
                "timeframe_min": int(candidate.get("timeframe_min", -1)),
                "expression": str(candidate.get("expression", "")),
            }
            if actual_fields != expected_fields:
                raise RunIntegrityError("adaptive discovery candidate does not match its generation proposal")

    def _adaptive_discover(self, dataset: GovernedBars, restored: Sequence[CandidateResult]) -> list[CandidateResult]:
        passing_by_id = {item.candidate.candidate_id: item for item in restored}
        for row in self._ledger.rows(stage="discovery"):
            if row.get("status") == "passed":
                recovered = _result_from_kill_ledger_row(row)
                passing_by_id[recovered.candidate.candidate_id] = recovered
        passing = list(passing_by_id.values())
        budget = self._adaptive_search_budget()
        generation_zero_limit = int(budget["generation_zero_expressions_per_group"])
        feedback_limit = int(budget["feedback_expressions_per_group"])
        groups = [(root, timeframe) for root in self._family_roots for timeframe in self.config.timeframes_minutes]
        contexts: dict[
            tuple[str, int],
            tuple[GovernedBars, np.ndarray, dict[str, np.ndarray], list[str], list[Candidate], dict[str, str]],
        ] = {}

        for group_index, (root, timeframe) in enumerate(groups):
            stop_reason = self._stop_reason()
            if stop_reason:
                if stop_reason != "max_candidates":
                    self._terminal_stop_reason = stop_reason
                    return passing
                raise RunIntegrityError("adaptive candidate budget exhausted before generation-0 closure")
            seed = self.config.seeds[group_index % len(self.config.seeds)]
            bars, labels, signals, expressions, candidates = self._build_group_context(
                dataset,
                root,
                timeframe,
                generation_zero_limit,
                seed,
                record_effective_trials=False,
                semantic_dedupe=True,
            )
            proposal_ids = self._record_generation_zero(
                root=root,
                timeframe=timeframe,
                seed=seed,
                expressions=expressions,
                candidates=candidates,
            )
            contexts[(root, timeframe)] = (bars, labels, signals, expressions, candidates, proposal_ids)
            stop_reason = self._evaluate_discovery_candidates(
                dataset=dataset,
                bars=bars,
                labels=labels,
                signals=signals,
                candidates=candidates,
                passing=passing,
                search_generation=0,
                proposal_ids=proposal_ids,
            )
            if stop_reason:
                if stop_reason != "max_candidates":
                    return passing
                raise RunIntegrityError("adaptive candidate budget exhausted before generation-0 closure")

        for group_index, (root, timeframe) in enumerate(groups):
            stop_reason = self._stop_reason()
            if stop_reason:
                if stop_reason != "max_candidates":
                    self._terminal_stop_reason = stop_reason
                    return passing
                raise RunIntegrityError("adaptive candidate budget exhausted before generation-1 closure")
            base_seed = self.config.seeds[group_index % len(self.config.seeds)]
            seed = _feedback_seed(
                family=self.config.family,
                root=root,
                timeframe_min=timeframe,
                base_seed=base_seed,
            )
            bars, labels, signals, expressions, candidates, _proposal_ids = contexts[(root, timeframe)]
            feedback_signals, feedback_expressions, feedback_candidates, feedback_proposal_ids = (
                self._build_feedback_context(
                    root=root,
                    timeframe=timeframe,
                    seed=seed,
                    bars=bars,
                    labels=labels,
                    generation_zero_expressions=expressions,
                    feedback_limit=feedback_limit,
                )
            )
            union_signals = {**signals, **feedback_signals}
            union_candidates = [*candidates, *feedback_candidates]
            discovery_mask = labels == "discovery"
            self._effective_trial_counts[f"{root}/{timeframe}"] = _effective_trigger_test_count(
                candidates=union_candidates,
                signals=union_signals,
                discovery_mask=discovery_mask,
                trading_days=bars.trading_day,
            )
            supply = self._expression_supply[f"{root}/{timeframe}"]
            supply.update(
                {
                    "feedback_requested": feedback_limit,
                    "feedback_valid": len(feedback_expressions),
                    "union_valid": len(expressions) + len(feedback_expressions),
                }
            )
            stop_reason = self._evaluate_discovery_candidates(
                dataset=dataset,
                bars=bars,
                labels=labels,
                signals=feedback_signals,
                candidates=feedback_candidates,
                passing=passing,
                search_generation=1,
                proposal_ids=feedback_proposal_ids,
            )
            if stop_reason and stop_reason != "max_candidates":
                return passing
            if stop_reason == "max_candidates" and group_index != len(groups) - 1:
                raise RunIntegrityError("adaptive candidate budget exhausted before generation-1 closure")

        self._validate_adaptive_lineage()
        return self._finalize_discovery(passing)

    def _finalize_discovery(self, passing: Sequence[CandidateResult]) -> list[CandidateResult]:
        discovery_rows = self._ledger.rows(stage="discovery")
        generation_rows = [
            {key: value for key, value in row.items() if key != "previous_hash"}
            for row in self._ledger.rows(stage="generation")
        ]
        search_space = _with_integrity_hash(
            {
                "schema": "alpha_mining_search_space.v3",
                "family": self.config.family,
                "code_fingerprint": self._code_fingerprint,
                "dataset_fingerprint": self._dataset_fingerprint(),
                "search_strategy": (
                    "discovery_feedback_v1" if self.config.feedback_expressions_per_group else "blind_v1"
                ),
                "generation_count": 2 if self.config.feedback_expressions_per_group else 1,
                "feedback_generator_version": (
                    FEEDBACK_GENERATOR_VERSION if self.config.feedback_expressions_per_group else None
                ),
                "adaptive_search_budget": (
                    self._adaptive_search_budget() if self.config.feedback_expressions_per_group else None
                ),
                "generation_evidence_by_group": dict(sorted(self._generation_evidence.items())),
                "lineage_hash": _canonical_hash({"generation_rows": generation_rows}),
                "union_candidate_hash": _canonical_hash(
                    {"candidate_ids": sorted(str(row["candidate_id"]) for row in discovery_rows)}
                ),
                "effective_trial_counts_by_group": dict(sorted(self._effective_trial_counts.items())),
                "effective_trials_total": max(1, sum(self._effective_trial_counts.values())),
                "expression_supply_by_group": dict(sorted(self._expression_supply.items())),
                "hypotheses_considered": len(discovery_rows),
                "raw_trials_evaluated": sum(row.get("status") in {"passed", "killed"} for row in discovery_rows),
                "exact_cut_duplicates": sum(row.get("status") == "deduplicated" for row in discovery_rows),
                "entry_rule_version": ENTRY_RULE_VERSION,
                "entry_comparator": ">=",
                "estimator": "Li-Ji eigenvalue count over discovery-day trigger activation profiles",
            },
            "search_space_hash",
        )
        _atomic_json(self.run_dir / "search_space.json", search_space)
        pollution_groups: dict[tuple[Any, ...], dict[str, CandidateResult]] = {}
        discovery_results = [
            _result_from_kill_ledger_row(row) for row in discovery_rows if row.get("status") in {"passed", "killed"}
        ]
        for result in discovery_results:
            candidate = result.candidate
            key = (
                candidate.root,
                candidate.timeframe_min,
                candidate.expression,
                candidate.direction,
                candidate.threshold,
            )
            pollution_groups.setdefault(key, {})[candidate.horizon] = result
        polluted = {
            result.candidate.candidate_id
            for metrics in pollution_groups.values()
            if monotonic_horizon_pollution({name: item.kill for name, item in metrics.items()})
            for result in metrics.values()
        }
        passing_by_id = {result.candidate.candidate_id: result for result in passing}
        filtered = [result for result in passing_by_id.values() if result.candidate.candidate_id not in polluted]
        per_root: list[CandidateResult] = []
        advanced_ids: set[str] = set()
        ranks: dict[str, int] = {}
        for root in self._family_roots:
            all_ranked = sorted(
                (item for item in filtered if item.candidate.root == root),
                key=_result_sort_key,
            )
            ranks.update({item.candidate.candidate_id: index for index, item in enumerate(all_ranked, start=1)})
            ranked = all_ranked[:100]
            per_root.extend(ranked)
            advanced_ids.update(item.candidate.candidate_id for item in ranked)
        pollution_evidence = {
            item.candidate.candidate_id: {
                name: metrics_to_dict(horizon_result.kill)
                for name, horizon_result in pollution_groups[
                    (
                        item.candidate.root,
                        item.candidate.timeframe_min,
                        item.candidate.expression,
                        item.candidate.direction,
                        item.candidate.threshold,
                    )
                ].items()
            }
            for item in passing_by_id.values()
            if item.candidate.candidate_id in polluted
        }
        for candidate_id, result in sorted(passing_by_id.items()):
            if candidate_id in polluted:
                status = "filtered_monotonic_horizon"
                reason = "monotonic_horizon_pollution"
                evidence: Mapping[str, Any] = {"horizon_metrics": pollution_evidence[candidate_id]}
            elif candidate_id in advanced_ids:
                status = "advanced"
                reason = ""
                evidence = {"root_rank": ranks[candidate_id], "root_cap": 100}
            else:
                status = "rank_capped"
                reason = "post_discovery_root_rank_cap"
                evidence = {"root_rank": ranks[candidate_id], "root_cap": 100}
            self._ledger.append(
                {
                    "candidate_id": candidate_id,
                    "stage": "post_discovery",
                    "status": status,
                    "candidate": asdict(result.candidate),
                    "disposition_reason": reason,
                    "evidence": dict(evidence),
                    "recorded_at_ns": timebase.now_ns(),
                }
            )
        post_discovery_ids = {str(row["candidate_id"]) for row in self._ledger.rows(stage="post_discovery")}
        if post_discovery_ids != set(passing_by_id):
            raise RunIntegrityError("post-discovery dispositions do not conserve discovery passes")
        self._checkpoint(
            stage="discovery_complete",
            state={"discovery_passed": [item.to_dict() for item in per_root]},
            force=True,
        )
        return per_root

    def _discover(self, dataset: GovernedBars, restored: Sequence[CandidateResult]) -> list[CandidateResult]:
        if self.config.feedback_expressions_per_group:
            return self._adaptive_discover(dataset, restored)
        passing_by_id = {item.candidate.candidate_id: item for item in restored}
        for row in self._ledger.rows(stage="discovery"):
            if row.get("status") == "passed":
                recovered = _result_from_kill_ledger_row(row)
                passing_by_id[recovered.candidate.candidate_id] = recovered
        passing = list(passing_by_id.values())
        denominator = len(self._family_roots) * len(self.config.timeframes_minutes) * 3 * 2 * 4
        expression_limit = max(1, int(np.ceil(self.config.max_candidates / denominator)))
        groups = [(root, timeframe) for root in self._family_roots for timeframe in self.config.timeframes_minutes]
        for group_index, (root, timeframe) in enumerate(groups):
            stop_reason = self._stop_reason()
            if stop_reason:
                if stop_reason != "max_candidates":
                    self._terminal_stop_reason = stop_reason
                break
            seed = self.config.seeds[group_index % len(self.config.seeds)]
            bars, labels, signals, _expressions, candidates = self._build_group_context(
                dataset,
                root,
                timeframe,
                expression_limit,
                seed,
            )
            remaining = [
                candidate for candidate in candidates if not self._ledger.has(candidate.candidate_id, "discovery")
            ][: max(0, self.config.max_candidates - self._ledger.unique_candidates)]

            def evaluate(
                candidate: Candidate,
                bars: GovernedBars = bars,
                signals: Mapping[str, np.ndarray] = signals,
                labels: np.ndarray = labels,
            ) -> CandidateResult:
                return _evaluate_candidate(
                    candidate,
                    dataset=dataset,
                    bars=bars,
                    signal=signals[candidate.expression],
                    split_name="discovery",
                    split_labels=labels,
                    evaluation_fraction=0.25,
                    cost_mode=self.config.cost_mode,
                )

            batch_size = max(1, self.config.workers * 2)
            with _discovery_process_pool(
                workers=self.config.workers,
                dataset=dataset,
                bars=bars,
                signals=signals,
                labels=labels,
                cost_mode=self.config.cost_mode,
                has_work=any(candidate.duplicate_of is None for candidate in remaining),
            ) as executor:
                for batch_start in range(0, len(remaining), batch_size):
                    batch = remaining[batch_start : batch_start + batch_size]
                    evaluated_batch = [candidate for candidate in batch if candidate.duplicate_of is None]
                    evaluated_results = (
                        (evaluate(candidate) for candidate in evaluated_batch)
                        if executor is None
                        else executor.map(
                            _evaluate_discovery_worker,
                            evaluated_batch,
                            chunksize=1,
                        )
                    )
                    results_by_id = {result.candidate.candidate_id: result for result in evaluated_results}
                    for candidate in batch:
                        if candidate.duplicate_of is not None:
                            self._ledger.append(
                                {
                                    "candidate_id": candidate.candidate_id,
                                    "stage": "discovery",
                                    "status": "deduplicated",
                                    "candidate": asdict(candidate),
                                    "failure_reason": "exact_resolved_cut_duplicate",
                                    "reference_candidate_id": candidate.duplicate_of,
                                    "threshold_resolution": (
                                        asdict(candidate.threshold_resolution)
                                        if candidate.threshold_resolution is not None
                                        else None
                                    ),
                                    "recorded_at_ns": timebase.now_ns(),
                                }
                            )
                            self._heartbeat(stage="discovery")
                            self._checkpoint(
                                stage="discovery",
                                state={"discovery_passed": [item.to_dict() for item in passing]},
                            )
                            continue
                        result = results_by_id[candidate.candidate_id]
                        self._ledger.append(
                            {
                                "candidate_id": result.candidate.candidate_id,
                                "stage": "discovery",
                                "status": result.status,
                                "candidate": asdict(result.candidate),
                                "metrics": metrics_to_dict(result.kill),
                                "threshold_resolution": (
                                    asdict(result.candidate.threshold_resolution)
                                    if result.candidate.threshold_resolution is not None
                                    else None
                                ),
                                "failure_reason": result.failure_reason,
                                "recorded_at_ns": timebase.now_ns(),
                            }
                        )
                        if result.kill.passed:
                            passing.append(result)
                        self._heartbeat(stage="discovery")
                        self._checkpoint(
                            stage="discovery",
                            state={"discovery_passed": [item.to_dict() for item in passing]},
                        )
                        stop_reason = self._stop_reason()
                        if stop_reason:
                            if stop_reason != "max_candidates":
                                self._terminal_stop_reason = stop_reason
                            break
                    if stop_reason:
                        break
        return self._finalize_discovery(passing)

    def _record_selection_disposition(
        self,
        *,
        candidate: Candidate,
        selection_ts: np.ndarray,
        selection_signal: np.ndarray,
        signals_kept: Sequence[tuple[np.ndarray, np.ndarray, str]],
        selected_for_root: int,
        selection_rows: dict[str, dict[str, Any]],
    ) -> bool:
        prior_row = selection_rows.get(candidate.candidate_id)
        if prior_row is not None and prior_row.get("status") in {
            "correlation_deduplicated",
            "rank_capped",
        }:
            return True
        if prior_row is not None:
            return False
        if selected_for_root >= 10:
            payload = {
                "candidate_id": candidate.candidate_id,
                "stage": "selection",
                "status": "rank_capped",
                "candidate": asdict(candidate),
                "disposition_reason": "selection_root_pass_cap",
                "evidence": {"root_pass_cap": 10},
                "recorded_at_ns": timebase.now_ns(),
            }
            self._ledger.append(payload)
            selection_rows[candidate.candidate_id] = payload
            return True
        correlations = [
            (
                _timestamp_signal_correlation(selection_ts, selection_signal, kept_ts, kept_signal),
                kept_id,
            )
            for kept_ts, kept_signal, kept_id in signals_kept
        ]
        correlated = max(correlations, default=(0.0, ""))
        if correlated[0] <= 0.5:
            return False
        payload = {
            "candidate_id": candidate.candidate_id,
            "stage": "selection",
            "status": "correlation_deduplicated",
            "candidate": asdict(candidate),
            "disposition_reason": "selection_signal_correlation",
            "reference_candidate_id": correlated[1],
            "evidence": {"absolute_correlation": correlated[0], "maximum_allowed": 0.5},
            "recorded_at_ns": timebase.now_ns(),
        }
        self._ledger.append(payload)
        selection_rows[candidate.candidate_id] = payload
        return True

    def _restore_selection_rows(
        self,
        discovery_ids: set[str],
        restored: Sequence[CandidateResult],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, CandidateResult]]:
        selection_rows = {str(row["candidate_id"]): row for row in self._ledger.rows(stage="selection")}
        recorded = {
            candidate_id: _result_from_kill_ledger_row(row)
            for candidate_id, row in selection_rows.items()
            if row.get("status") in {"passed", "killed"}
        }
        if not set(selection_rows).issubset(discovery_ids):
            raise RunIntegrityError("selection ledger contains a candidate outside the frozen discovery set")
        for item in restored:
            if item.candidate.candidate_id not in discovery_ids:
                raise RunIntegrityError("selection checkpoint contains a candidate outside the frozen discovery set")
            prior = recorded.setdefault(item.candidate.candidate_id, item)
            if prior.to_dict() != item.to_dict():
                raise RunIntegrityError("selection checkpoint/ledger result mismatch")
        return selection_rows, recorded

    def _selection(
        self,
        dataset: GovernedBars,
        discovery: Sequence[CandidateResult],
        restored: Sequence[CandidateResult] = (),
    ) -> list[CandidateResult]:
        discovery_ids = {item.candidate.candidate_id for item in discovery}
        if len(discovery_ids) != len(discovery):
            raise RunIntegrityError("discovery checkpoint contains duplicate candidates")
        selection_rows, recorded = self._restore_selection_rows(discovery_ids, restored)
        selected: list[CandidateResult] = []
        adapter = FAMILY_REGISTRY[self.config.family]
        group_contexts: dict[tuple[str, int], _SelectionGroupContext] = {}

        def checkpoint_state() -> dict[str, Any]:
            return {
                "discovery_passed": [item.to_dict() for item in discovery],
                "selection_passed": [item.to_dict() for item in selected],
            }

        self._checkpoint(stage="selection", state=checkpoint_state())
        for root in self._family_roots:
            root_results = sorted((item for item in discovery if item.candidate.root == root), key=_result_sort_key)
            signals_kept: list[tuple[np.ndarray, np.ndarray, str]] = []
            for result in root_results:
                stop_reason = self._stop_reason(include_candidate_cap=False)
                if stop_reason is not None:
                    self._terminal_stop_reason = stop_reason
                    break
                candidate = result.candidate
                group_key = (root, candidate.timeframe_min)
                context = group_contexts.get(group_key)
                if context is None:
                    bars = dataset.group(*group_key)
                    plan = build_split_plan(bars.trading_day)
                    selection_mask = plan.mask("selection")
                    context = _SelectionGroupContext(
                        bars=bars,
                        split_labels=plan.labels,
                        selection_mask=selection_mask,
                        selection_ts=bars.ts_ns[selection_mask],
                        features=adapter.build_features(bars, self.config),
                        signals={},
                    )
                    group_contexts[group_key] = context
                if candidate.expression not in context.signals:
                    full_signal = adapter.evaluate_expression(
                        candidate.expression,
                        context.features,
                        context.bars.reset,
                    )
                    context.signals[candidate.expression] = (
                        full_signal,
                        full_signal[context.selection_mask],
                    )
                signal, selection_signal = context.signals[candidate.expression]
                if self._record_selection_disposition(
                    candidate=candidate,
                    selection_ts=context.selection_ts,
                    selection_signal=selection_signal,
                    signals_kept=signals_kept,
                    selected_for_root=len([item for item in selected if item.candidate.root == root]),
                    selection_rows=selection_rows,
                ):
                    self._checkpoint(stage="selection", state=checkpoint_state())
                    continue
                evaluated = recorded.get(candidate.candidate_id)
                if evaluated is None:
                    evaluated = _evaluate_candidate(
                        candidate,
                        dataset=dataset,
                        bars=context.bars,
                        signal=signal,
                        split_name="selection",
                        split_labels=context.split_labels,
                        cost_mode=self.config.cost_mode,
                    )
                    self._ledger.append(
                        {
                            "candidate_id": candidate.candidate_id,
                            "stage": "selection",
                            "status": evaluated.status,
                            "candidate": asdict(candidate),
                            "metrics": metrics_to_dict(evaluated.kill),
                            "failure_reason": evaluated.failure_reason,
                            "recorded_at_ns": timebase.now_ns(),
                        }
                    )
                    selection_rows[candidate.candidate_id] = self._ledger.rows(stage="selection")[-1]
                elif evaluated.candidate != candidate:
                    raise RunIntegrityError("selection ledger candidate mismatch")
                if evaluated.kill.passed:
                    selected.append(evaluated)
                    signals_kept.append((context.selection_ts, selection_signal, candidate.candidate_id))
                    self._unlocks.freeze_locked(candidate.candidate_id)
                self._checkpoint(stage="selection", state=checkpoint_state())
        if self._terminal_stop_reason is None:
            selection_ids = {str(row["candidate_id"]) for row in self._ledger.rows(stage="selection")}
            if selection_ids != discovery_ids:
                raise RunIntegrityError("selection dispositions do not conserve post-discovery advances")
        self._checkpoint(
            stage="selection_complete",
            state={"selection_passed": [item.to_dict() for item in selected]},
            force=True,
        )
        return selected

    def _locked(
        self,
        dataset: GovernedBars,
        selection: Sequence[CandidateResult],
        restored: Sequence[CandidateResult] = (),
    ) -> list[CandidateResult]:
        self._restore_search_space_evidence()
        selection_ids = {item.candidate.candidate_id for item in selection}
        if len(selection_ids) != len(selection):
            raise RunIntegrityError("selection checkpoint contains duplicate candidates")
        restored_by_id = {item.candidate.candidate_id: item for item in restored}
        if len(restored_by_id) != len(restored):
            raise RunIntegrityError("locked-validation checkpoint contains duplicate candidates")
        if not set(restored_by_id).issubset(selection_ids):
            raise RunIntegrityError(
                "locked-validation checkpoint contains a candidate outside the frozen selection set"
            )
        locked_results: list[CandidateResult] = []
        pvalues: list[float] = []
        actual_trials = max(1, self._ledger.unique_candidates)
        effective_trials = max(
            1,
            min(actual_trials, sum(self._effective_trial_counts.values()) or actual_trials),
        )
        trial_sharpe_std = _discovery_trial_sharpe_std(
            self._ledger.rows(stage="discovery"),
            evaluated_only=bool(self.config.feedback_expressions_per_group),
        )

        def checkpoint_state() -> dict[str, Any]:
            return {
                "selection_passed": [item.to_dict() for item in selection],
                "locked_evaluated": [item.to_dict() for item in locked_results],
            }

        self._checkpoint(stage="locked_validation", state=checkpoint_state())
        for index, selected in enumerate(selection):
            stop_reason = self._stop_reason(include_candidate_cap=False)
            if stop_reason is not None:
                self._terminal_stop_reason = stop_reason
                break
            candidate = selected.candidate
            result = restored_by_id.get(candidate.candidate_id)
            if result is not None:
                if result.candidate != candidate or result.locked is None:
                    raise RunIntegrityError("locked-validation checkpoint candidate mismatch")
                locked = result.locked
            else:
                if not self._unlocks.has_granted_access(candidate.candidate_id, "locked_validation"):
                    self._unlocks.require(candidate.candidate_id, "locked_validation")
                bars = dataset.group(candidate.root, candidate.timeframe_min)
                plan = build_split_plan(bars.trading_day)
                adapter = FAMILY_REGISTRY[self.config.family]
                features = adapter.build_features(bars, self.config)
                signal = adapter.evaluate_expression(candidate.expression, features, bars.reset)
                feature_history_bars = feature_history_bars_for_expression(
                    candidate.family,
                    candidate.expression,
                )
                (
                    evaluation_bars,
                    evaluation_signal,
                    evaluation_labels,
                    target_timeframe_min,
                    signal_history_starts,
                ) = _exact_horizon_inputs(
                    dataset=dataset,
                    candidate=candidate,
                    feature_bars=bars,
                    feature_signal=signal,
                    split_labels=plan.labels,
                    feature_history_bars=feature_history_bars,
                )
                targets = forward_target_indices(
                    horizon=candidate.horizon,
                    timeframe_min=target_timeframe_min,
                    split_labels=evaluation_labels,
                    reset_mask=evaluation_bars.reset,
                    session_close=evaluation_bars.session_close,
                )
                target_returns = forward_returns(evaluation_bars.close, targets)
                mask = evaluation_labels == "locked_validation"
                permutation_clusters = np.asarray(
                    [
                        f"{day}:{session}"
                        for day, session in zip(
                            evaluation_bars.trading_day[mask],
                            evaluation_bars.session[mask],
                            strict=True,
                        )
                    ],
                    dtype="<U32",
                )
                masked_signal = np.full(evaluation_signal.size, np.nan)
                masked_signal[mask] = evaluation_signal[mask]
                execution = simulate_next_bar_execution(
                    signal=masked_signal,
                    direction=candidate.direction,
                    threshold=candidate.threshold,
                    target_indices=targets,
                    bid_open=evaluation_bars.bid_open,
                    ask_open=evaluation_bars.ask_open,
                    bid_close=evaluation_bars.bid_close,
                    ask_close=evaluation_bars.ask_close,
                    reset_mask=evaluation_bars.reset,
                    instrument_profile=_profile_for_root(candidate.root),
                    contracts=evaluation_bars.contract,
                    cost_mode=self.config.cost_mode,
                )
                locked = locked_validation(
                    signal=evaluation_signal[mask] * float(candidate.direction),
                    target_returns=target_returns[mask],
                    execution=execution,
                    actual_trials=actual_trials,
                    effective_trials=effective_trials,
                    trial_sharpe_std=trial_sharpe_std,
                    trading_days=evaluation_bars.trading_day,
                    validation_days=tuple(dict.fromkeys(str(day) for day in evaluation_bars.trading_day[mask])),
                    permutation_clusters=permutation_clusters,
                    permutation_strata=evaluation_bars.session[mask],
                    permutation_positions=evaluation_bars.ts_ns[mask] % DAY_NS,
                    signal_history_start_indices=signal_history_starts,
                    feature_history_exact=feature_history_bars is not None,
                    feature_history_reason=("" if feature_history_bars is not None else "unbounded_feature_history"),
                    feature_history_bars=feature_history_bars,
                    seed=self.config.seeds[index % len(self.config.seeds)],
                )
                result = CandidateResult(
                    candidate=candidate,
                    kill=selected.kill,
                    locked=locked,
                    stage="locked_validation",
                    status="passed" if locked.passed else "killed",
                )
            locked_results.append(result)
            pvalues.append(max(locked.bootstrap_pvalue, locked.permutation_pvalue))
            self._checkpoint(stage="locked_validation", state=checkpoint_state())
        bh_mask = benjamini_hochberg(pvalues, q=0.10)
        survivors = [
            result
            for result, bh_pass in zip(locked_results, bh_mask, strict=True)
            if result.locked is not None and result.locked.passed and bool(bh_pass)
        ]
        for result, bh_pass in zip(locked_results, bh_mask, strict=True):
            self._ledger.append(
                {
                    "candidate_id": result.candidate.candidate_id,
                    "stage": "locked_validation",
                    "status": "passed" if result in survivors else "killed",
                    "candidate": asdict(result.candidate),
                    "metrics": metrics_to_dict(result.locked) if result.locked else {},
                    "bh_q10_passed": bool(bh_pass),
                    "recorded_at_ns": timebase.now_ns(),
                }
            )

        def locked_rank_key(item: CandidateResult) -> tuple[float, float, float, float, int, str]:
            if item.locked is None:
                raise RunIntegrityError("locked-validation survivor is missing locked metrics")
            return candidate_rank_key(
                locked=item.locked,
                kill=item.kill,
                complexity=item.candidate.complexity,
                candidate_id=item.candidate.candidate_id,
            )

        final_frozen: list[CandidateResult] = []
        for root in self._family_roots:
            ranked = sorted(
                (item for item in survivors if item.candidate.root == root),
                key=locked_rank_key,
            )[:3]
            for result in ranked:
                self._unlocks.freeze_final(result.candidate.candidate_id)
            final_frozen.extend(ranked)
        self._checkpoint(
            stage="locked_complete",
            state={"locked_passed": [item.to_dict() for item in final_frozen]},
            force=True,
        )
        return final_frozen

    def _restore_search_space_evidence(self, *, required: bool = False) -> None:
        path = self.run_dir / "search_space.json"
        if not path.exists():
            if required:
                raise RunIntegrityError("completed discovery checkpoint has no search-space evidence")
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        _verify_integrity_hash(payload, "search_space_hash", artifact="search space")
        if (
            payload.get("schema") not in {"alpha_mining_search_space.v2", "alpha_mining_search_space.v3"}
            or payload.get("code_fingerprint") != self._code_fingerprint
            or payload.get("dataset_fingerprint") != self._dataset_fingerprint()
        ):
            raise RunIntegrityError("search-space evidence fingerprint mismatch")
        if payload.get("schema") == "alpha_mining_search_space.v3":
            generation_rows = [
                {key: value for key, value in row.items() if key != "previous_hash"}
                for row in self._ledger.rows(stage="generation")
            ]
            expected_lineage_hash = _canonical_hash({"generation_rows": generation_rows})
            if payload.get("lineage_hash") != expected_lineage_hash:
                raise RunIntegrityError("search-space lineage hash does not match the trial ledger")
            discovery_rows = self._ledger.rows(stage="discovery")
            expected_union_hash = _canonical_hash(
                {"candidate_ids": sorted(str(row.get("candidate_id", "")) for row in discovery_rows)}
            )
            if payload.get("union_candidate_hash") != expected_union_hash:
                raise RunIntegrityError("search-space candidate-union hash does not match the trial ledger")
            if payload.get("search_strategy") == "discovery_feedback_v1":
                if not self.config.feedback_expressions_per_group:
                    raise RunIntegrityError("adaptive search-space evidence does not match the run configuration")
                self._validate_adaptive_lineage()
        counts = payload.get("effective_trial_counts_by_group")
        if not isinstance(counts, Mapping) or not counts:
            raise RunIntegrityError("search-space evidence has no effective trial counts")
        restored = {str(key): int(value) for key, value in counts.items()}
        if any(value < 1 for value in restored.values()):
            raise RunIntegrityError("search-space effective trial count must be positive")
        self._effective_trial_counts = restored
        supply = payload.get("expression_supply_by_group")
        if isinstance(supply, Mapping):
            self._expression_supply = {
                str(key): {str(name): int(count) for name, count in dict(value).items()}
                for key, value in supply.items()
                if isinstance(value, Mapping)
            }
        generation_evidence = payload.get("generation_evidence_by_group")
        if isinstance(generation_evidence, Mapping):
            self._generation_evidence = {
                str(key): dict(value) for key, value in generation_evidence.items() if isinstance(value, Mapping)
            }

    def _final(self, dataset: GovernedBars, frozen: Sequence[CandidateResult]) -> list[CandidateResult]:
        results: list[CandidateResult] = []
        recorded = {str(row.get("candidate_id")): row for row in self._ledger.rows(stage="final_holdout")}
        for item in frozen:
            stop_reason = self._stop_reason(include_candidate_cap=False)
            if stop_reason is not None:
                self._terminal_stop_reason = stop_reason
                break
            candidate = item.candidate
            prior = recorded.get(candidate.candidate_id)
            if prior is not None:
                recovered = _result_from_kill_ledger_row(prior, locked=item.locked)
                if recovered.candidate != candidate:
                    raise RunIntegrityError("final-holdout ledger candidate mismatch")
                if recovered.status == "passed":
                    results.append(recovered)
                continue
            if self._unlocks.has_granted_access(candidate.candidate_id, "final_holdout"):
                raise RunIntegrityError(
                    "final holdout was unlocked without a durable result; refusing to evaluate it twice"
                )
            self._unlocks.require(candidate.candidate_id, "final_holdout")
            bars = dataset.group(candidate.root, candidate.timeframe_min)
            plan = build_split_plan(bars.trading_day)
            adapter = FAMILY_REGISTRY[self.config.family]
            features = adapter.build_features(bars, self.config)
            signal = adapter.evaluate_expression(candidate.expression, features, bars.reset)
            result = _evaluate_candidate(
                candidate,
                dataset=dataset,
                bars=bars,
                signal=signal,
                split_name="final_holdout",
                split_labels=plan.labels,
                cost_mode=self.config.cost_mode,
            )
            self._ledger.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "stage": "final_holdout",
                    "status": result.status,
                    "candidate": asdict(candidate),
                    "metrics": metrics_to_dict(result.kill),
                    "screen_only": True,
                    "recorded_at_ns": timebase.now_ns(),
                }
            )
            if result.kill.passed:
                results.append(
                    CandidateResult(
                        candidate=candidate,
                        kill=result.kill,
                        locked=item.locked,
                        stage="final_holdout",
                        status="passed",
                    )
                )
        return results

    def _screen_survivors(self, survivors: Sequence[CandidateResult]) -> list[dict[str, Any]]:
        import yaml

        from research.combinatorial.promote import promote_smma_candidate

        if self.config.family != "smma":
            raise RunIntegrityError(
                f"screen adapter for family {self.config.family!r} is not implemented; refusing to mislabel it as SMMA"
            )
        outcomes: list[dict[str, Any]] = []
        evidence_dir = self.run_dir / "screen_evidence"
        for result in survivors:
            stop_reason = self._stop_reason(include_candidate_cap=False)
            if stop_reason is not None:
                self._terminal_stop_reason = stop_reason
                break
            candidate = result.candidate
            alpha_id = f"{candidate.family}_{candidate.root.lower()}_{candidate.candidate_id[:12]}"
            evidence_path = evidence_dir / f"{candidate.candidate_id}.json"
            if evidence_path.exists():
                cached = json.loads(evidence_path.read_text(encoding="utf-8"))
                _verify_integrity_hash(cached, "evidence_hash", artifact="screen evidence")
                if (
                    cached.get("candidate_id") != candidate.candidate_id
                    or cached.get("expression") != candidate.expression
                ):
                    raise RunIntegrityError("screen evidence candidate fingerprint mismatch")
                if cached.get("state") != "complete":
                    raise RunIntegrityError(
                        "screen may have run without durable evidence; refusing to run Gate A-C twice"
                    )
                cached_outcome = dict(cached["outcome"])
                if int(cached_outcome.get("screen_exit_code", 1)) == 0:
                    cached_scorecard_path = Path(str(cached_outcome.get("scorecard_path", "")))
                    scorecard = json.loads(cached_scorecard_path.read_text(encoding="utf-8"))
                    embedded_hash = str(scorecard.pop("scorecard_hash", ""))
                    if (
                        cached_outcome.get("screen_only") is not True
                        or scorecard.get("screen_only") is not True
                        or scorecard.get("promotion_eligible") is not False
                        or embedded_hash != cached_outcome.get("scorecard_hash")
                        or _canonical_hash(scorecard) != embedded_hash
                    ):
                        raise RunIntegrityError("cached screen scorecard hash mismatch")
                outcomes.append(cached_outcome)
                continue
            alpha_dir = Path("research/alphas") / alpha_id
            if alpha_dir.exists():
                manifest_path = alpha_dir / "manifest.yaml"
                manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
                metadata = manifest.get("experiment_metadata", {})
                if (
                    manifest.get("formula") != candidate.expression
                    or metadata.get("family") != candidate.family
                    or metadata.get("candidate_spec") != asdict(candidate)
                    or metadata.get("screen_only_required") is not True
                ):
                    raise RunIntegrityError(f"existing SMMA package does not match survivor: {alpha_dir}")
            else:
                alpha_dir = promote_smma_candidate(
                    candidate.expression,
                    alpha_id=alpha_id,
                    owner="research",
                    instrument=_profile_for_root(candidate.root),
                    candidate_spec=asdict(candidate),
                )
            _atomic_json(
                evidence_path,
                _with_integrity_hash(
                    {
                        "schema": "smma_screen_evidence.v1",
                        "state": "started",
                        "candidate_id": candidate.candidate_id,
                        "expression": candidate.expression,
                        "started_at": datetime.now(UTC).isoformat(),
                    },
                    "evidence_hash",
                ),
            )
            command = [
                sys.executable,
                "-m",
                "hft_platform.cli",
                "alpha",
                "screen",
                "--alpha-id",
                alpha_id,
                "--data",
                str(self.dataset_path),
                "--skip-gate-b-tests",
                "--experiments-dir",
                str(self.run_dir / "screen"),
            ]
            remaining_seconds = (
                max(1.0, self._deadline_epoch_s - time.time())
                if self._deadline_epoch_s is not None
                else self.config.wall_time_hours * 3600.0
            )
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=min(1_800.0, remaining_seconds),
                )
                return_code = completed.returncode
                stdout = completed.stdout
                stderr = completed.stderr
            except subprocess.TimeoutExpired as exc:
                return_code = 124
                stdout = str(exc.stdout or "")
                stderr = f"screen timeout: {exc}"
            scorecard_path = ""
            scorecard_hash = ""
            try:
                summary = json.loads(stdout)
                scorecard_path = str(summary["scorecard_path"])
                scorecard = json.loads(Path(scorecard_path).read_text(encoding="utf-8"))
                embedded_hash = str(scorecard.pop("scorecard_hash", ""))
                if (
                    summary.get("screen_only") is not True
                    or summary.get("promotion_eligible") is not False
                    or scorecard.get("screen_only") is not True
                    or scorecard.get("promotion_eligible") is not False
                    or not embedded_hash
                    or _canonical_hash(scorecard) != embedded_hash
                ):
                    raise RunIntegrityError("screen scorecard is not valid screen-only evidence")
                scorecard_hash = embedded_hash
            except (KeyError, OSError, ValueError, RunIntegrityError) as exc:
                stderr = f"{stderr}\ninvalid screen evidence: {exc}".strip()
                if return_code == 0:
                    return_code = 2
            outcome = {
                "alpha_id": alpha_id,
                "alpha_dir": str(alpha_dir),
                "screen_only": True,
                "screen_exit_code": return_code,
                "scorecard_path": scorecard_path,
                "scorecard_hash": scorecard_hash,
                "screen_stdout_tail": "\n".join(stdout.splitlines()[-20:]),
                "screen_stderr_tail": "\n".join(stderr.splitlines()[-20:]),
            }
            _atomic_json(
                evidence_path,
                _with_integrity_hash(
                    {
                        "schema": "smma_screen_evidence.v1",
                        "state": "complete",
                        "candidate_id": candidate.candidate_id,
                        "expression": candidate.expression,
                        "outcome": outcome,
                        "completed_at": datetime.now(UTC).isoformat(),
                    },
                    "evidence_hash",
                ),
            )
            outcomes.append(outcome)
        return outcomes

    def _robustness_survivors(
        self,
        dataset: GovernedBars,
        survivors: Sequence[CandidateResult],
    ) -> dict[str, Any]:
        evidence_dir = self.run_dir / "robustness_evidence"
        evidence: dict[str, Any] = {}
        for result in survivors:
            stop_reason = self._stop_reason(include_candidate_cap=False)
            if stop_reason is not None:
                self._terminal_stop_reason = stop_reason
                break
            candidate = result.candidate
            path = evidence_dir / f"{candidate.candidate_id}.json"
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                _verify_integrity_hash(cached, "evidence_hash", artifact="robustness evidence")
                if (
                    cached.get("candidate_id") != candidate.candidate_id
                    or cached.get("expression") != candidate.expression
                ):
                    raise RunIntegrityError("robustness evidence candidate fingerprint mismatch")
                slices = cached.get("slices")
                if not isinstance(slices, Mapping):
                    raise RunIntegrityError("robustness evidence is incomplete")
            else:
                slices = evaluate_robustness_slices(
                    dataset,
                    result,
                    family=self.config.family,
                    cost_mode=self.config.cost_mode,
                )
                _atomic_json(
                    path,
                    _with_integrity_hash(
                        {
                            "schema": "smma_robustness_evidence.v1",
                            "candidate_id": candidate.candidate_id,
                            "expression": candidate.expression,
                            "slices": slices,
                            "recorded_at": datetime.now(UTC).isoformat(),
                        },
                        "evidence_hash",
                    ),
                )
            evidence[candidate.candidate_id] = dict(slices)
        return evidence

    @contextmanager
    def _heartbeat_monitor(self) -> Iterator[None]:
        stop = threading.Event()
        failure: list[Exception] = []

        def monitor() -> None:
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    self._heartbeat(stage=self._active_checkpoint_stage, force=True)
                except Exception as exc:
                    failure.append(exc)
                    return

        self._heartbeat(stage="initialized", force=True)
        thread = threading.Thread(target=monitor, name="smma-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=5.0)
            if failure:
                raise RunIntegrityError(f"heartbeat writer failed: {failure[0]}") from failure[0]

    def run(self) -> dict[str, Any]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        policy = apply_resource_policy()
        dataset = self._load_or_export_dataset()
        self._validate_frozen_dataset_scope(dataset)
        manifest = self._ensure_manifest(
            dataset,
            {**policy, "dataset_cache": dict(self._dataset_cache_evidence)},
        )
        with self._heartbeat_monitor():
            return self._run_stages(dataset, manifest)

    @staticmethod
    def _restored_validation_checkpoint(
        stage: str,
        state: Mapping[str, Any],
    ) -> tuple[list[CandidateResult] | None, list[CandidateResult]]:
        if stage == "locked_validation":
            return (
                [CandidateResult.from_dict(item) for item in state.get("selection_passed", ())],
                [CandidateResult.from_dict(item) for item in state.get("locked_evaluated", ())],
            )
        if stage == "selection_complete":
            return (
                [CandidateResult.from_dict(item) for item in state.get("selection_passed", ())],
                [],
            )
        return None, []

    def _restore_complete_report(self, state: Mapping[str, Any]) -> dict[str, Any]:
        if not self.report_path.exists():
            raise RunIntegrityError("complete checkpoint has no durable report")
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        _verify_integrity_hash(report, "report_hash", artifact="report")
        checkpoint_report = state.get("report")
        if isinstance(checkpoint_report, Mapping) and dict(checkpoint_report) != report:
            raise RunIntegrityError("complete checkpoint report does not match the durable report")
        search_space_complete = bool(report.get("search_space_complete", False))
        if (
            self.config.feedback_expressions_per_group
            and not search_space_complete
            and not report.get("terminal_stop_reason")
        ):
            raise RunIntegrityError("adaptive complete report has neither search-space nor terminal-stop evidence")
        self._restore_search_space_evidence(
            required=bool(self.config.feedback_expressions_per_group) and search_space_complete
        )
        return report

    def _run_stages(
        self,
        dataset: GovernedBars,
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        checkpoint = self._load_checkpoint() if self.config.resume else {}
        raw_state = checkpoint.get("state")
        state: Mapping[str, Any] = raw_state if isinstance(raw_state, Mapping) else {}
        stage = str(checkpoint.get("stage", ""))
        if stage == "complete":
            return self._restore_complete_report(state)
        self._restore_search_space_evidence(
            required=bool(self.config.feedback_expressions_per_group)
            and stage
            in {
                "discovery_complete",
                "selection",
                "selection_complete",
                "locked_validation",
                "locked_complete",
            }
        )
        if self._wall_time_reached():
            self._terminal_stop_reason = "wall_time"
            return self._finish(
                manifest,
                dataset,
                [],
                [],
                "KILL",
                "mining stopped before completion: wall_time",
            )
        if stage == "locked_complete":
            frozen = [CandidateResult.from_dict(item) for item in state.get("locked_passed", ())]
        else:
            selection, locked_evaluated = self._restored_validation_checkpoint(stage, state)
            if selection is None:
                discovery = (
                    [CandidateResult.from_dict(item) for item in state.get("discovery_passed", ())]
                    if stage in {"discovery", "discovery_complete", "selection"}
                    else []
                )
                if stage not in {"discovery_complete", "selection"}:
                    discovery = self._discover(dataset, discovery)
                if self._terminal_stop_reason is not None:
                    return self._finish(
                        manifest,
                        dataset,
                        [],
                        [],
                        "KILL",
                        f"mining stopped before validation: {self._terminal_stop_reason}",
                    )
                restored_selection = [CandidateResult.from_dict(item) for item in state.get("selection_passed", ())]
                selection = self._selection(dataset, discovery, restored_selection)
            if self._terminal_stop_reason is not None:
                return self._finish(
                    manifest,
                    dataset,
                    [],
                    [],
                    "KILL",
                    f"mining stopped during selection: {self._terminal_stop_reason}",
                )
            if not selection:
                return self._finish(manifest, dataset, [], [], "KILL", "no candidates survived selection")
            frozen = self._locked(dataset, selection, locked_evaluated)
        if self._terminal_stop_reason is not None:
            return self._finish(
                manifest,
                dataset,
                [],
                [],
                "KILL",
                f"mining stopped during locked validation: {self._terminal_stop_reason}",
            )
        locked_terminal = self._locked_terminal_report(manifest, dataset, frozen)
        if locked_terminal is not None:
            return locked_terminal
        final = self._final(dataset, frozen)
        if self._terminal_stop_reason is not None:
            return self._finish(
                manifest,
                dataset,
                [],
                [],
                "KILL",
                f"mining stopped during final holdout: {self._terminal_stop_reason}",
            )
        if not final:
            return self._finish(manifest, dataset, [], [], "KILL", "all candidates killed by final holdout")
        robustness = self._robustness_survivors(dataset, final)
        screens = self._screen_survivors(final)
        if self._terminal_stop_reason is not None:
            return self._finish(
                manifest,
                dataset,
                final,
                screens,
                "KILL",
                f"mining stopped during screen-only Gate A-C: {self._terminal_stop_reason}",
                robustness=robustness,
            )
        unique_days = len(set(str(value) for value in dataset.trading_day))
        if any(int(screen.get("screen_exit_code", 1)) != 0 for screen in screens):
            verdict = "KILL"
            reason = "one or more screen-only Gate A-C evaluations failed"
        else:
            verdict = "NEEDS_MORE_DAYS" if unique_days < MIN_DAYS_FOR_PROMOTION else "SCREEN_ONLY"
            reason = ""
        return self._finish(manifest, dataset, final, screens, verdict, reason, robustness=robustness)

    def _locked_terminal_report(
        self,
        manifest: Mapping[str, Any],
        dataset: GovernedBars,
        frozen: Sequence[CandidateResult],
    ) -> dict[str, Any] | None:
        if not frozen:
            return self._finish(
                manifest,
                dataset,
                [],
                [],
                "KILL",
                "all candidates killed by locked validation",
            )
        if not self.config.unlock_final_holdout:
            return self._finish(
                manifest,
                dataset,
                frozen,
                [],
                "LOCKED_DIAGNOSTIC",
                "locked validation completed; final holdout remains locked pending explicit approval",
            )
        return None

    def _funnel_evidence(
        self,
    ) -> tuple[
        dict[str, dict[str, int]],
        dict[str, int],
        dict[str, dict[str, int]],
        dict[str, Any],
        list[dict[str, Any]],
    ]:
        stages: dict[str, Counter[str]] = {}
        failures: Counter[str] = Counter()
        dispositions: dict[str, Counter[str]] = {}
        near_misses: list[tuple[tuple[float, float, float], dict[str, Any]]] = []
        for row in self._ledger.rows():
            stage = str(row.get("stage", "unknown"))
            status = str(row.get("status", "unknown"))
            stages.setdefault(stage, Counter())[status] += 1
            if status not in {"passed", "killed"}:
                dispositions.setdefault(stage, Counter())[status] += 1
            if status != "killed":
                continue
            reasons = [value for value in str(row.get("failure_reason", "")).split(",") if value]
            metrics = row.get("metrics")
            metric_values = dict(metrics) if isinstance(metrics, Mapping) else {}
            locked_reasons = metric_values.get("failure_reasons", ())
            if isinstance(locked_reasons, Sequence) and not isinstance(locked_reasons, str):
                reasons.extend(str(value) for value in locked_reasons)
            for value in set(reasons):
                failures[value] += 1
            rank = (
                float(metric_values.get("deflated_sharpe", 0.0)),
                float(metric_values.get("detrended_ic", 0.0)),
                float(metric_values.get("net_edge", 0.0)),
            )
            near_misses.append(
                (
                    rank,
                    {
                        "candidate_id": str(row.get("candidate_id", "")),
                        "stage": stage,
                        "failure_reasons": sorted(set(reasons)),
                        "rank_metrics": {
                            "deflated_sharpe": rank[0],
                            "detrended_ic": rank[1],
                            "net_edge": rank[2],
                        },
                    },
                )
            )
        funnel = {stage: dict(sorted(counts.items())) for stage, counts in sorted(stages.items())}
        disposition_counts = {stage: dict(sorted(counts.items())) for stage, counts in sorted(dispositions.items())}
        discovery_passed = stages.get("discovery", Counter()).get("passed", 0)
        post_discovery_total = sum(stages.get("post_discovery", Counter()).values())
        post_discovery_advanced = stages.get("post_discovery", Counter()).get("advanced", 0)
        selection_total = sum(stages.get("selection", Counter()).values())
        conservation = {
            "discovery_passed": discovery_passed,
            "post_discovery_dispositions": post_discovery_total,
            "post_discovery_conserved": discovery_passed == post_discovery_total,
            "post_discovery_advanced": post_discovery_advanced,
            "selection_dispositions": selection_total,
            "selection_conserved": post_discovery_advanced == selection_total,
        }
        if self._terminal_stop_reason is None and not conservation["post_discovery_conserved"]:
            raise RunIntegrityError("report funnel does not conserve discovery passes")
        if self._terminal_stop_reason is None and not conservation["selection_conserved"]:
            raise RunIntegrityError("report funnel does not conserve selection dispositions")
        ranked = [payload for _rank, payload in sorted(near_misses, key=lambda item: item[0], reverse=True)[:20]]
        return funnel, dict(sorted(failures.items())), disposition_counts, conservation, ranked

    def _finish(
        self,
        manifest: Mapping[str, Any],
        dataset: GovernedBars,
        survivors: Sequence[CandidateResult],
        screens: Sequence[Mapping[str, Any]],
        verdict: str,
        reason: str,
        *,
        robustness: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        funnel, gate_failures, dispositions, conservation, near_misses = self._funnel_evidence()
        report = _with_integrity_hash(
            {
                "schema": "alpha_mining_report.v3",
                "verdict": verdict,
                "reason": reason,
                "screen_only": True,
                "promotion_eligible": False,
                "posthoc_diagnostic": bool(self.config.posthoc_diagnostic),
                "cost_mode": self.config.cost_mode,
                "cost_claim_eligible": bool(manifest.get("cost_claim_eligible", False)),
                "final_holdout_unlocked": bool(self.config.unlock_final_holdout),
                "final_holdout_claim_eligible": bool(self.config.unlock_final_holdout)
                and not self.config.posthoc_diagnostic,
                "run_manifest_hash": str(manifest["manifest_hash"]),
                "trading_day_count": len(set(str(value) for value in dataset.trading_day)),
                "minimum_days_for_promotion": MIN_DAYS_FOR_PROMOTION,
                "unique_hypotheses": self._ledger.unique_candidates,
                "evaluated_hypotheses": sum(
                    row.get("status") in {"passed", "killed"} for row in self._ledger.rows(stage="discovery")
                ),
                "survivors": [item.to_dict() for item in survivors],
                "survivor_stage": (
                    survivors[0].stage if survivors and len({item.stage for item in survivors}) == 1 else ""
                ),
                "screens": [dict(item) for item in screens],
                "robustness_slices": dict(robustness or {}),
                "funnel": funnel,
                "gate_failure_histogram": gate_failures,
                "non_gate_dispositions": dispositions,
                "stage_conservation": conservation,
                "near_misses": near_misses,
                "terminal_stop_reason": self._terminal_stop_reason or "",
                "search_space_complete": (self.run_dir / "search_space.json").exists(),
                "effective_trial_counts_by_group": dict(sorted(self._effective_trial_counts.items())),
                "expression_supply_by_group": dict(sorted(self._expression_supply.items())),
                "completed_at": datetime.now(UTC).isoformat(),
            },
            "report_hash",
        )
        _atomic_json(self.report_path, report)
        self._checkpoint(stage="complete", state={"report": report}, force=True)
        self._heartbeat(stage="complete", force=True)
        return report


def run_mining(config: RunConfig) -> dict[str, Any]:
    return MiningRun(config).run()


def mining_status(run_dir: str | Path) -> dict[str, Any]:
    root = Path(run_dir).resolve()
    payload: dict[str, Any] = {
        "run_dir": str(root),
        "manifest_exists": (root / "run_manifest.json").exists(),
        "checkpoint_exists": (root / "checkpoint.json").exists(),
        "heartbeat_exists": (root / "heartbeat.json").exists(),
        "report_exists": (root / "report.json").exists(),
    }
    for name in ("run_manifest", "checkpoint", "heartbeat", "report"):
        path = root / f"{name}.json"
        if path.exists():
            artifact = json.loads(path.read_text(encoding="utf-8"))
            hash_field = {
                "run_manifest": "manifest_hash",
                "checkpoint": "checkpoint_hash",
                "heartbeat": "heartbeat_hash",
                "report": "report_hash",
            }[name]
            _verify_integrity_hash(artifact, hash_field, artifact=name.replace("_", " "))
            payload[name] = artifact
    ledger = HashChainLedger(root / "trials.jsonl")
    HashChainLedger(root / "split_access.jsonl")
    payload["unique_hypotheses"] = ledger.unique_candidates
    dataset_path = root / "dataset.npz"
    if dataset_path.exists() or Path(str(dataset_path) + ".meta.json").exists():
        manifest = payload.get("run_manifest")
        family = str(manifest.get("family", "smma")) if isinstance(manifest, Mapping) else "smma"
        adapter = FAMILY_REGISTRY.get(family)
        if adapter is None:
            raise RunIntegrityError(f"run manifest declares an unsupported family: {family!r}")
        adapter.dataset.load(dataset_path)
        payload["dataset_valid"] = True
    return payload
