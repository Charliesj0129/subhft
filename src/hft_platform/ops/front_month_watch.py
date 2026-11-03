"""Watch the one invariant every contract roll exists for.

"Is the feed carrying, and is every strategy trading, the contract the exchange
says is the front month right now?" Before 2026-11 no signal asked that
directly. Each roll failure surfaced through a side effect instead -- a
subscription that failed (09-16), intents dropped by a gate (08-10), a position
that would not release a daily stop (10-21) -- and every alert watched a
counter of one such effect. ``strategy_bound_live_symbols`` is computed only at
connect and read 1 for a dead contract on 2026-09-16.

This module computes the answer once a minute from three independent inputs:

- the exchange rule (``core.front_month.expected_front_delivery``), with no
  broker data, so a broker list that lost its front month cannot vouch for
  itself;
- the codes the feed is subscribed to (plus resolved alias targets);
- the concrete symbols each strategy currently holds.

and publishes it as gauges an alert can read (see
``config/monitoring/alerts/rules.yaml``: ``FrontMonthNotSubscribed``,
``StrategyOffFrontMonth``, ``FrontMonthWatchStalled``).

Scope: monthly futures roots that some strategy trades. A strategy bound to a
``R2`` (next month) family is not reported as off the front month.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from structlog import get_logger

from hft_platform.contracts.ref import ContractFamily, FamilyCode, FutureRef, Product, parse_display
from hft_platform.core import timebase
from hft_platform.core.front_month import expected_front_delivery

logger = get_logger("ops.front_month_watch")

__all__ = [
    "FrontMonthReport",
    "evaluate_front_month",
    "expected_front_code",
    "publish_front_month",
    "run_front_month_watch",
]

#: ``HFT_FRONT_MONTH_WATCH_S``: seconds between checks; 0 disables the watch.
DEFAULT_INTERVAL_S = 60.0


@dataclass(frozen=True, slots=True)
class FrontMonthReport:
    """The front-month state of one futures root."""

    root: str
    #: The rule's front contract, e.g. ``TMFK6``.
    expected: str
    #: The feed carries ``expected``.
    subscribed: bool
    #: ``strategy_id -> True`` when every symbol it holds for this root is the
    #: front month (or an R1 family), ``False`` otherwise.
    strategies_on_front: Mapping[str, bool]
    #: ``(strategy_id, symbol)`` pairs holding another month of this root.
    off_front: tuple[tuple[str, str], ...]


def _future_root(symbol: str, base_year: int) -> tuple[str, FutureRef | ContractFamily] | None:
    try:
        ref = parse_display(symbol, base_year=base_year)
    except ValueError:
        return None
    if isinstance(ref, FutureRef):
        return ref.root, ref
    if isinstance(ref, ContractFamily) and ref.product is Product.FUTURE:
        return ref.root, ref
    return None


def evaluate_front_month(
    strategy_symbols: Mapping[str, Iterable[str]],
    subscribed: Iterable[str],
    *,
    now_ns: int,
    calendar: Any = None,
) -> list[FrontMonthReport]:
    """Compare every traded futures root against the exchange rule.

    Pure: no clock, no I/O beyond the calendar lookup inside the rule. One
    report per root that at least one strategy holds, sorted by root.
    """
    expected_delivery = expected_front_delivery(now_ns=now_ns, calendar=calendar)
    base_year = expected_delivery.year
    feed = {str(code) for code in subscribed}

    per_root: dict[str, dict[str, bool]] = {}
    off: dict[str, list[tuple[str, str]]] = {}
    for strategy_id, symbols in strategy_symbols.items():
        for symbol in symbols:
            parsed = _future_root(str(symbol), base_year)
            if parsed is None:
                continue
            root, ref = parsed
            states = per_root.setdefault(root, {})
            on_front = True
            if isinstance(ref, FutureRef):
                on_front = (ref.expiry.year, ref.expiry.month) == (expected_delivery.year, expected_delivery.month)
            elif ref.family is not FamilyCode.R1:
                # R2 and pinned families trade another month on purpose.
                on_front = True
            if not on_front:
                off.setdefault(root, []).append((strategy_id, str(symbol)))
            states[strategy_id] = states.get(strategy_id, True) and on_front

    reports: list[FrontMonthReport] = []
    for root in sorted(per_root):
        expected = FutureRef(root=root, expiry=expected_delivery).display()
        reports.append(
            FrontMonthReport(
                root=root,
                expected=expected,
                subscribed=expected in feed,
                strategies_on_front=dict(per_root[root]),
                off_front=tuple(sorted(off.get(root, ()))),
            )
        )
    return reports


def publish_front_month(reports: Iterable[FrontMonthReport], metrics: Any, *, now_s: float) -> None:
    """Write ``reports`` to the gauges; a metrics failure only logs."""
    if metrics is None:
        return
    try:
        for report in reports:
            metrics.front_month_subscribed.labels(root=report.root).set(1 if report.subscribed else 0)
            for strategy_id, on_front in report.strategies_on_front.items():
                metrics.front_month_strategy_on_front.labels(strategy_id=strategy_id, root=report.root).set(
                    1 if on_front else 0
                )
        metrics.front_month_check_timestamp_seconds.set(now_s)
    except Exception as exc:  # noqa: BLE001 — observability must not stop the watch
        logger.warning("front_month_watch_publish_failed", error=str(exc))


def _log(reports: list[FrontMonthReport], previous: dict[str, tuple[bool, tuple[tuple[str, str], ...]]]) -> None:
    """Log each root once per change of state, not once a minute."""
    for report in reports:
        state = (report.subscribed, report.off_front)
        if previous.get(report.root) == state:
            continue
        previous[report.root] = state
        if report.subscribed and not report.off_front:
            logger.info("front_month_ok", root=report.root, expected=report.expected)
        else:
            logger.error(
                "front_month_mismatch",
                root=report.root,
                expected=report.expected,
                subscribed=report.subscribed,
                off_front=[f"{sid}:{sym}" for sid, sym in report.off_front],
            )


def _snapshot_inputs(strategy_runner: Any, md_client: Any) -> tuple[dict[str, list[str]], set[str]]:
    strategies = {
        str(getattr(strategy, "strategy_id", "UNKNOWN")): [str(s) for s in (getattr(strategy, "symbols", ()) or ())]
        for strategy in list(getattr(strategy_runner, "strategies", ()) or ())
    }
    subscribed = set(getattr(md_client, "subscribed_codes", set()) or set())
    alias_map = getattr(md_client, "alias_to_actual", None) or {}
    subscribed |= {str(code) for code in alias_map.values()}
    return strategies, subscribed


async def run_front_month_watch(
    strategy_runner: Any,
    md_client: Any,
    *,
    metrics_getter: Callable[[], Any] | None = None,
    interval_s: float | None = None,
) -> None:
    """Evaluate and publish forever, on the engine loop.

    Inputs are copied on the loop (two set copies); the evaluation, which reads
    the market calendar, runs in a worker thread so it never holds the loop.
    """
    if interval_s is None:
        interval_s = float(os.getenv("HFT_FRONT_MONTH_WATCH_S", str(DEFAULT_INTERVAL_S)))
    if interval_s <= 0:
        logger.info("front_month_watch_disabled", interval_s=interval_s)
        return
    if metrics_getter is None:
        from hft_platform.observability.metrics import get_metrics

        metrics_getter = get_metrics

    previous: dict[str, tuple[bool, tuple[tuple[str, str], ...]]] = {}
    while True:
        try:
            strategies, subscribed = _snapshot_inputs(strategy_runner, md_client)
            now_ns = timebase.now_ns()
            reports = await asyncio.to_thread(evaluate_front_month, strategies, subscribed, now_ns=now_ns)
            publish_front_month(reports, metrics_getter(), now_s=now_ns / 1e9)
            _log(reports, previous)
        except Exception as exc:  # noqa: BLE001 — the watch outlives any single failure
            logger.warning("front_month_watch_failed", error=str(exc))
        await asyncio.sleep(interval_s)


def expected_front_code(root: str, *, now_ns: int, calendar: Any = None) -> str:
    """The rule's front contract code for ``root`` at ``now_ns`` (e.g. ``TMFK6``)."""
    delivery: dt.date = expected_front_delivery(now_ns=now_ns, calendar=calendar)
    return FutureRef(root=root, expiry=delivery).display()
