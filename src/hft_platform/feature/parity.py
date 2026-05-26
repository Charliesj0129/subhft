"""Cross-path feature parity harness for the promoted microstructure family.

Research, replay and live all share the same Python :class:`FeatureEngine`. Drift
can only enter from two seams:

1. **Kernel numerics** — the Rust backend (``kernel_backend="rust"``) re-implements
   the v1 kernel in ``rust_core/src/feature.rs``; float-accumulated EMA features are
   rounded to ``i64`` and may differ from Python by a rounding ULP.
2. **Input derivation** — how each path turns a raw book snapshot into the
   ``LOBStatsEvent`` fields the engine consumes.

This module drives one shared sequence of raw book snapshots through each compute
path and compares the *promoted feature family* (see
:func:`hft_platform.feature.registry.promoted_indices`) with per-feature tolerance,
reporting the **first divergence** with full coordinates (path pair, symbol,
timestamp, feature_id, expected, actual).

It is intentionally free of test-only dependencies so it can be reused by CI tests,
a future drift-check CLI, and live/shadow promotion gates.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feature.registry import (
    FeatureSet,
    default_feature_registry,
    promoted_feature_ids,
    promoted_indices,
)
from hft_platform.feed_adapter.lob_engine import LOBEngine

# Canonical synthetic book scale: 0.1 * 10_000 (matches feed_adapter conventions).
_SELFTEST_TICK = 1_000
_SELFTEST_SYMBOL = "PARITYR1"


def _rust_available() -> bool:
    for name in ("hft_platform.rust_core", "rust_core"):
        try:
            mod = importlib.import_module(name)
        except Exception:
            continue
        if getattr(mod, "RustFeaturePipelineV1", None) is not None:
            return True
    return False


@dataclass(frozen=True, slots=True)
class LobInputFrame:
    """One raw L1–L5 book snapshot fed identically to every compute path.

    ``bids``/``asks`` are ``(N, 2)`` int64 arrays of ``[price_scaled, qty]`` rows,
    bids descending by price and asks ascending — the same convention as
    :class:`hft_platform.events.BidAskEvent`.
    """

    symbol: str
    ts: int
    bids: np.ndarray
    asks: np.ndarray
    is_reset: bool = False

    def to_bidask_event(self, seq: int) -> BidAskEvent:
        return BidAskEvent(
            meta=MetaData(seq=seq, source_ts=self.ts, local_ts=self.ts),
            symbol=self.symbol,
            bids=np.asarray(self.bids, dtype=np.int64).reshape(-1, 2),
            asks=np.asarray(self.asks, dtype=np.int64).reshape(-1, 2),
            is_snapshot=True,
        )


@dataclass(frozen=True, slots=True)
class FrameResult:
    """Per-frame output captured from a single compute path."""

    symbol: str
    timestamp: int
    feature_ids: tuple[str, ...]
    values: tuple[int | float, ...]
    warmup_ready_mask: int


# A path result is the per-frame outputs, indexed positionally with the input frames.
PathResult = list[FrameResult]


@dataclass(frozen=True, slots=True)
class Divergence:
    """First point at which two paths disagree (criterion-5 coordinates)."""

    path_pair: str
    frame_index: int
    symbol: str
    timestamp: int
    feature_id: str
    index: int
    expected: int | float
    actual: int | float
    abs_diff: int | float
    tolerance: int

    def format(self) -> str:
        return (
            f"FEATURE PARITY DIVERGENCE [{self.path_pair}]\n"
            f"  frame_index = {self.frame_index}\n"
            f"  symbol      = {self.symbol}\n"
            f"  timestamp   = {self.timestamp}\n"
            f"  feature_id  = {self.feature_id} (index {self.index})\n"
            f"  expected    = {self.expected}\n"
            f"  actual      = {self.actual}\n"
            f"  abs_diff    = {self.abs_diff}  (tolerance {self.tolerance})"
        )


@dataclass(slots=True)
class ParityReport:
    """Outcome of comparing two or more compute paths over one frame sequence."""

    ok: bool = True
    n_frames: int = 0
    compared_paths: tuple[str, ...] = ()
    first_divergence: Divergence | None = None
    schema_mismatch: str | None = None
    warmup_mismatch: str | None = None

    def format(self) -> str:
        if self.ok:
            return (
                f"PARITY OK: {self.n_frames} frames, paths={list(self.compared_paths)}"
            )
        if self.schema_mismatch is not None:
            return f"SCHEMA MISMATCH: {self.schema_mismatch}"
        if self.warmup_mismatch is not None:
            return f"WARMUP/RESET MISMATCH: {self.warmup_mismatch}"
        assert self.first_divergence is not None
        return self.first_divergence.format()

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise AssertionError(self.format())


# ---------------------------------------------------------------------------
# Compute-path runners
# ---------------------------------------------------------------------------
def _run_engine(
    frames: Sequence[LobInputFrame],
    *,
    backend: str,
    wire_lob_feature_attr: bool = False,
) -> PathResult:
    """Drive ``frames`` through ``LOBEngine`` -> ``FeatureEngine`` for one backend.

    ``wire_lob_feature_attr`` mirrors the hftbacktest adapter wiring
    (``lob_engine.feature_engine = fe``, per ``backtest/adapter.py``); it is a
    no-op for the engine numerics but documents that the adapter seam matches the
    direct live wiring.
    """
    lob = LOBEngine()
    fe = FeatureEngine(kernel_backend=backend, emit_events=True)
    if wire_lob_feature_attr:
        lob.feature_engine = fe
    out: PathResult = []
    for seq, frame in enumerate(frames):
        if frame.is_reset:
            # Simulate a gap-triggered reset of both planes (criterion 1).
            fe.reset_symbol(frame.symbol)
            book = lob.books.pop(frame.symbol, None)
            if book is not None and lob._last_symbol == frame.symbol:  # noqa: SLF001
                lob._last_symbol = None  # noqa: SLF001
                lob._last_book = None  # noqa: SLF001
        bidask = frame.to_bidask_event(seq)
        stats = lob.process_event(bidask)
        if not isinstance(stats, LOBStatsEvent):
            continue
        evt = fe.process_lob_update(bidask, stats, local_ts_ns=frame.ts)
        if evt is None:
            continue
        out.append(
            FrameResult(
                symbol=evt.symbol,
                timestamp=int(evt.ts),
                feature_ids=tuple(evt.feature_ids),
                values=tuple(evt.values),
                warmup_ready_mask=int(evt.warmup_ready_mask),
            )
        )
    return out


def run_python_engine(frames: Sequence[LobInputFrame]) -> PathResult:
    """Live/research direct path: Python FeatureEngine over LOBEngine-derived stats."""
    return _run_engine(frames, backend="python")


def run_rust_engine(frames: Sequence[LobInputFrame]) -> PathResult | None:
    """Rust kernel path. Returns ``None`` when the Rust extension is unavailable."""
    if not _rust_available():
        return None
    return _run_engine(frames, backend="rust")


def run_hftbacktest_shared(frames: Sequence[LobInputFrame]) -> PathResult:
    """hftbacktest ``feature_mode='lob_feature'`` wiring (sans market simulator).

    Mirrors ``backtest/_hbt_utils.build_lob_event``: BidAskEvent -> LOBEngine ->
    FeatureEngine.process_lob_update, with ``lob_engine.feature_engine`` wired.
    """
    return _run_engine(frames, backend="python", wire_lob_feature_attr=True)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def compare_paths(
    results_by_name: dict[str, PathResult],
    *,
    feature_set: FeatureSet | None = None,
    promoted_only: bool = True,
) -> ParityReport:
    """Compare two or more path results frame-by-frame against the first path.

    The first entry (insertion order) is the reference ("expected"); every other
    path is the candidate ("actual"). Schema (feature_ids) and warmup masks are
    checked first, then promoted-family values within ``spec.parity_atol``.
    Short-circuits on the first divergence.
    """
    if feature_set is None:
        feature_set = default_feature_registry().get_default()
    names = list(results_by_name)
    if len(names) < 2:
        raise ValueError("compare_paths needs at least two paths")

    ref_name = names[0]
    ref = results_by_name[ref_name]
    specs = feature_set.features
    if promoted_only:
        compare_idx = promoted_indices(feature_set)
    else:
        compare_idx = tuple(range(len(specs)))

    report = ParityReport(
        n_frames=len(ref), compared_paths=tuple(names)
    )

    for cand_name in names[1:]:
        cand = results_by_name[cand_name]
        pair = f"{ref_name} vs {cand_name}"
        if len(ref) != len(cand):
            report.ok = False
            report.schema_mismatch = (
                f"{pair}: frame count {len(ref)} != {len(cand)}"
            )
            return report
        for fi, (r, c) in enumerate(zip(ref, cand, strict=True)):
            if r.feature_ids != c.feature_ids:
                report.ok = False
                report.schema_mismatch = (
                    f"{pair} frame {fi}: feature_ids differ "
                    f"({r.feature_ids} != {c.feature_ids})"
                )
                return report
            if r.warmup_ready_mask != c.warmup_ready_mask:
                report.ok = False
                report.warmup_mismatch = (
                    f"{pair} frame {fi} symbol={r.symbol} ts={r.timestamp}: "
                    f"warmup_ready_mask {r.warmup_ready_mask:#b} != {c.warmup_ready_mask:#b}"
                )
                return report
            for idx in compare_idx:
                exp = r.values[idx]
                act = c.values[idx]
                tol = int(specs[idx].parity_atol)
                diff = abs(exp - act)
                if diff > tol:
                    report.ok = False
                    report.first_divergence = Divergence(
                        path_pair=pair,
                        frame_index=fi,
                        symbol=r.symbol,
                        timestamp=r.timestamp,
                        feature_id=specs[idx].feature_id,
                        index=idx,
                        expected=exp,
                        actual=act,
                        abs_diff=diff,
                        tolerance=tol,
                    )
                    return report
    return report


@dataclass(slots=True)
class RecordingFeatureSink:
    """Capture promoted-family feature tuples emitted by a full backtest run.

    Used by the integration test to harvest per-event feature output from the real
    ``HftBacktestAdapter`` (which calls ``FeatureEngine.process_lob_update``).
    """

    feature_set: FeatureSet = field(default_factory=lambda: default_feature_registry().get_default())
    frames: PathResult = field(default_factory=list)

    def record(self, evt: object) -> None:
        if evt is None:
            return
        self.frames.append(
            FrameResult(
                symbol=str(getattr(evt, "symbol", "")),
                timestamp=int(getattr(evt, "ts", 0) or 0),
                feature_ids=tuple(getattr(evt, "feature_ids", ())),
                values=tuple(getattr(evt, "values", ())),
                warmup_ready_mask=int(getattr(evt, "warmup_ready_mask", 0) or 0),
            )
        )


# ---------------------------------------------------------------------------
# Synthetic self-test (shared by the unit test and the `hft feature parity` CLI)
# ---------------------------------------------------------------------------
def _selftest_book(best_bid: int, best_ask: int, bid_qty: int, ask_qty: int) -> tuple[np.ndarray, np.ndarray]:
    bids = np.array(
        [[best_bid - i * _SELFTEST_TICK, max(1, bid_qty - i * 3)] for i in range(5)],
        dtype=np.int64,
    )
    asks = np.array(
        [[best_ask + i * _SELFTEST_TICK, max(1, ask_qty - i * 3)] for i in range(5)],
        dtype=np.int64,
    )
    return bids, asks


def build_synthetic_frames(symbol: str = _SELFTEST_SYMBOL) -> list[LobInputFrame]:
    """Deterministic frame sequence exercising warmup, one-sided book, and reset re-warm.

    Shared by ``tests/unit/test_feature_promoted_parity.py`` and the
    ``hft feature parity`` CLI so the test and the ops gate cover the same inputs.
    """
    frames: list[LobInputFrame] = []
    base_bid = 1_000_000
    ts = 1_000
    for i in range(40):  # warmup ramp + steady book with moving top-of-book
        bb = base_bid + (i % 6) * _SELFTEST_TICK
        ba = bb + 2 * _SELFTEST_TICK
        bids, asks = _selftest_book(bb, ba, 50 + i, 40 + (i % 7))
        frames.append(LobInputFrame(symbol, ts, bids, asks))
        ts += 125
    for _ in range(5):  # one-sided thin ask book
        bb = base_bid + 3 * _SELFTEST_TICK
        ba = bb + _SELFTEST_TICK
        bids, asks = _selftest_book(bb, ba, 80, 1)
        frames.append(LobInputFrame(symbol, ts, bids, asks))
        ts += 125
    bb = base_bid + 2 * _SELFTEST_TICK  # gap-triggered reset, then re-warm
    bids, asks = _selftest_book(bb, bb + 2 * _SELFTEST_TICK, 60, 55)
    frames.append(LobInputFrame(symbol, ts, bids, asks, is_reset=True))
    ts += 125
    for i in range(10):
        bb = base_bid + (i % 4) * _SELFTEST_TICK
        bids, asks = _selftest_book(bb, bb + 2 * _SELFTEST_TICK, 45 + i, 50 - i)
        frames.append(LobInputFrame(symbol, ts, bids, asks))
        ts += 125
    return frames


def run_self_test(*, feature_set: FeatureSet | None = None) -> dict:
    """Run the synthetic parity gate across all available paths.

    Returns a JSON-serializable summary: per-pair parity results vs the Python
    reference, with the first divergence's full coordinates when a pair fails.
    Suitable for a CI/ops gate (``ok`` is False on any divergence).
    """
    if feature_set is None:
        feature_set = default_feature_registry().get_default()
    frames = build_synthetic_frames()

    python = run_python_engine(frames)
    rust = run_rust_engine(frames)
    hftbt = run_hftbacktest_shared(frames)

    candidates: dict[str, PathResult] = {"hftbacktest_shared": hftbt}
    if rust is not None:
        candidates["rust"] = rust

    comparisons = []
    overall_ok = True
    for name, result in candidates.items():
        report = compare_paths({"python": python, name: result}, feature_set=feature_set)
        overall_ok = overall_ok and report.ok
        entry: dict = {"pair": f"python vs {name}", "ok": report.ok, "n_frames": report.n_frames}
        if report.schema_mismatch is not None:
            entry["schema_mismatch"] = report.schema_mismatch
        if report.warmup_mismatch is not None:
            entry["warmup_mismatch"] = report.warmup_mismatch
        if report.first_divergence is not None:
            d = report.first_divergence
            entry["first_divergence"] = {
                "frame_index": d.frame_index,
                "symbol": d.symbol,
                "timestamp": d.timestamp,
                "feature_id": d.feature_id,
                "index": d.index,
                "expected": d.expected,
                "actual": d.actual,
                "abs_diff": d.abs_diff,
                "tolerance": d.tolerance,
            }
        comparisons.append(entry)

    return {
        "ok": overall_ok,
        "feature_set_id": feature_set.feature_set_id,
        "schema_version": int(feature_set.schema_version),
        "n_frames": len(python),
        "rust_available": rust is not None,
        "promoted_feature_ids": list(promoted_feature_ids(feature_set)),
        "comparisons": comparisons,
    }
