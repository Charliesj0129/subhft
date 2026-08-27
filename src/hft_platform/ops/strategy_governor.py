from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from structlog import get_logger

from hft_platform.contracts.strategy import TIF, IntentType, Side
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops.autonomy import AutonomyMode, AutonomyTransition
from hft_platform.ops.evidence import get_shared_autonomy_evidence_writer

logger = get_logger("strategy_governor")


def _noop_persist() -> None:
    """Nothing to write: the decision was 'no'."""


_AUTONOMY_MODE_VALUES = {
    AutonomyMode.NORMAL: 0,
    AutonomyMode.STRATEGY_QUARANTINED: 1,
    AutonomyMode.PLATFORM_REDUCE_ONLY: 2,
    AutonomyMode.HALT: 3,
}


class StrategyQuarantineStateCorrupt(RuntimeError):
    """The persisted safety state exists but cannot be read as a latch record.

    Distinct from "no document" (a cold start, genuinely nothing latched) and
    from ``RuntimeStateUnreadable`` (the JSON itself will not parse). This is
    the middle case the first version missed: a *syntactically valid* document
    whose ``strategies`` section has the wrong shape. Normalising that to "no
    quarantines" is the same fail-open the restore exists to remove, so it
    raises instead.
    """


@dataclass(slots=True, frozen=True)
class PersistedQuarantine:
    """One latched strategy as it was found on disk. No live state, no IO."""

    strategy_id: str
    reason: str
    #: ``None`` for a latch written before tokens existed; the caller mints one.
    token: str | None


def parse_persisted_quarantines(snapshot: Any) -> dict[str, PersistedQuarantine | None]:
    """Validate a safety snapshot and extract every latched strategy.

    Pure: no filesystem, no metrics, no governor. Split out of the restore so
    the *validation* can run at a point in startup where refusing costs
    nothing, while the *application* runs later, once a governor exists to
    apply it to. Keeping them in one method meant a malformed individual entry
    raised only at the second point -- after the Redis lease and its refresh
    thread were live -- so under ``restart: always`` every attempt leaked a
    lease it held until TTL. Validating the section container early and the
    entries late is not "validated early"; the entries are where the malformed
    shapes actually live.

    Returns *every* strategy the document mentions: a ``PersistedQuarantine``
    where the latch is set, ``None`` where the document explicitly records it
    as clear. Dropping the cleared ones and returning only the latched set is
    lossy in a way that matters to the caller's merge -- "this run never heard
    of the strategy" and "this run watched an operator clear it" are different
    facts, and collapsing them let a stale snapshot resurrect a latch that had
    just been authorized away.

    Fail-closed on every reading it cannot trust. A snapshot with no
    ``strategies`` key at all is the one benign case, and only because
    ``read_state_strict`` will not hand one out for a document that exists:
    there, section absence is already rejected. A *directly supplied* snapshot
    bypasses that reader, so the same distinctions are enforced here rather
    than assumed -- ``{"strategies": null}`` and a non-object snapshot both
    used to land on the cold-start return.
    """
    if not isinstance(snapshot, dict):
        raise StrategyQuarantineStateCorrupt(
            f"safety snapshot is {type(snapshot).__name__}, not an object; "
            "refusing to read it as 'no strategy is quarantined'"
        )
    if "strategies" not in snapshot:
        return {}
    strategies = snapshot["strategies"]
    if not isinstance(strategies, dict):
        raise StrategyQuarantineStateCorrupt(
            f"persisted 'strategies' section is {type(strategies).__name__}, not an object; "
            "refusing to start rather than assume no strategy is quarantined"
        )

    seen: dict[str, PersistedQuarantine | None] = {}
    for strategy_id in sorted(strategies):
        entry = strategies[strategy_id]
        if not isinstance(entry, dict):
            raise StrategyQuarantineStateCorrupt(
                f"persisted entry for strategy {strategy_id!r} is {type(entry).__name__}, not an "
                "object; its latch state cannot be read and must not be assumed clear"
            )
        required = entry.get("manual_rearm_required")
        if not isinstance(required, bool):
            # ``bool(entry.get(...))`` collapsed missing, null, "" and 0 into a
            # deliberate ``False`` -- so a record carrying a real reason and a
            # real token, whose flag had been truncated to null, read as "this
            # strategy is free to trade". Every writer of this field writes an
            # actual bool (``ops/autonomy.py``, ``ops/evidence.py``), so
            # anything else is damage, and damage to *this* field is not
            # something to resolve in the trading direction.
            raise StrategyQuarantineStateCorrupt(
                f"persisted entry for strategy {strategy_id!r} has "
                f"manual_rearm_required={required!r} ({type(required).__name__}), not a bool; "
                "refusing to read it as 'not quarantined'"
            )
        if not required:
            seen[strategy_id] = None
            continue
        if "quarantine_token" not in entry:
            # A latch written before tokens existed. The caller mints one.
            token: str | None = None
        else:
            token = entry["quarantine_token"]
            if not isinstance(token, str) or not token:
                # Present but malformed is NOT legacy, and treating it as legacy
                # produced a latch nothing could clear: restore minted a live
                # token, ``_persist_legacy_tokens`` saw the existing truthy
                # value and declined to overwrite it, so disk kept e.g. ``123``
                # while memory held the new token. The CLI rejected the disk
                # value, the governor rejected the CLI's, and every restart
                # repeated it while logging that the migration had succeeded.
                raise StrategyQuarantineStateCorrupt(
                    f"persisted entry for strategy {strategy_id!r} has "
                    f"quarantine_token={token!r} ({type(token).__name__}), not a non-empty string; "
                    "it names the latch an operator must authorize and cannot be guessed"
                )
        seen[strategy_id] = PersistedQuarantine(
            strategy_id=strategy_id,
            reason=str(entry.get("reason") or "restored_from_runtime_state"),
            token=token,
        )
    return seen


@dataclass(slots=True, frozen=True)
class StrategyQuarantine:
    strategy_id: str
    reason: str
    transition: AutonomyTransition
    #: Unique per quarantine *instance*, not per strategy. An operator re-arm
    #: must name the exact token it intends to clear; see ``rearm``.
    token: str = ""


#: How long ``quarantine_async`` blocks the strategy consumer on the durable
#: write before letting it finish in the background. Two orders of magnitude
#: above a healthy write (two fsyncs) and an order of magnitude below
#: ``locked_state``'s own two-second lock deadline, so a contended lock costs
#: the bus 200 ms rather than 2 s.


class StrategyHealthGovernor:
    def __init__(self, metrics=None, evidence_writer=None):
        self.metrics = metrics or MetricsRegistry.get()
        self.evidence_writer = evidence_writer or get_shared_autonomy_evidence_writer()
        self._quarantined: dict[str, StrategyQuarantine] = {}
        self._quarantine_seq: int = 0
        #: Identifies this governor instance. A quarantine token carries it so a
        #: re-arm request issued for an *earlier* quarantine can never match a
        #: later one, even when the process is the same: the per-instance
        #: counter restarts at 1 on every rebuild, so a module-scoped id would
        #: hand the first quarantine of a rebuilt governor a token a previous
        #: instance had already issued. Full uuid4, not a truncation -- a PID
        #: and a sequence number both repeat, so this is the only part carrying
        #: the non-collision guarantee.
        self._run_id: str = f"{os.getpid():d}-{uuid.uuid4().hex}"
        # Tokens minted by an earlier run and adopted by ``restore_persisted_quarantines``.
        # They carry that run's id, not ours, so ``owns_token`` cannot recognise
        # them by prefix. See its docstring.
        self._restored_tokens: set[str] = set()
        #: Strong references to in-flight durable writes.
        #: ``asyncio`` keeps only a weak reference to a running future, so a
        #: fire-and-forget submission whose only other reference is a local
        #: variable can be garbage-collected mid-write. This set is what makes
        #: "the write continues in the background" true rather than merely
        #: stated. Entries are discarded by the done callback, so it is bounded
        #: by the number of writes actually in flight.
        self._persist_tasks: set[asyncio.Future[Any]] = set()

    def _mint_token(self, strategy_id: str) -> str:
        """A token names one quarantine *instance*, unique within this process."""
        self._quarantine_seq += 1
        return f"{self._run_id}:{strategy_id}:{self._quarantine_seq}"

    def quarantine_token(self, strategy_id: str) -> str | None:
        """Token of the strategy's live quarantine, or ``None`` if not quarantined."""
        entry = self._quarantined.get(strategy_id)
        return entry.token if entry is not None else None

    def owns_token(self, token: str) -> bool:
        """True when this engine could have issued ``token``.

        ``_run_id`` is ``pid-uuid4``, so a token names not just a quarantine
        instance but the engine run that minted it. That matters because the
        re-arm request directory is shared state and the session-ownership
        preflight is advisory -- ``build()`` continues past a conflicting owner
        -- so two engines can legitimately be scanning the same directory.

        The consumer retires (unlinks) any request whose token does not match
        the live latch. Without this check, engine A retires a request that
        names engine B's quarantine: B never sees it, and the operator's
        authorization is destroyed by an engine that was never entitled to
        judge it. The operator gets no error, because from A's side the request
        looked stale.

        Tokens restored from disk count as ours: the latch is this engine's now,
        and if it is later cleared and re-quarantined, the older request must
        still be retirable or it would linger with nothing able to match it.
        """
        return token.startswith(f"{self._run_id}:") or token in self._restored_tokens

    def _latch_quarantine(self, strategy_id: str, *, reason: str) -> tuple[AutonomyTransition, Callable[[], None]]:
        """Apply the in-memory latch; return it with the durable write deferred.

        The split exists because the two halves have opposite constraints. The
        latch has to be immediate -- it is what stops the strategy -- and costs
        a dict write. The durable record has to be *serialized against other
        processes*, so it goes through ``locked_state``, which polls ``flock``
        and then reads and rewrites a JSON file. Doing that inline puts an
        unbounded wait on whatever thread called ``quarantine``.
        """
        from_mode = AutonomyMode.STRATEGY_QUARANTINED if strategy_id in self._quarantined else AutonomyMode.NORMAL
        transition = self._build_transition(from_mode=from_mode, reason=reason)
        token = self._mint_token(strategy_id)
        self._quarantined[strategy_id] = StrategyQuarantine(
            strategy_id=strategy_id,
            reason=reason,
            transition=transition,
            token=token,
        )
        self._set_strategy_quarantine_active(strategy_id, active=True)
        self._set_strategy_scope_state()
        transition.record_transition(self.metrics)
        logger.warning("strategy_quarantined", strategy_id=strategy_id, reason=reason, quarantine_token=token)

        writer = self.evidence_writer

        def _persist() -> None:
            if writer is None:
                return
            writer.record_transition(
                scope="strategy",
                mode=transition.to_mode.value,
                reason=transition.reason,
                manual_rearm_required=transition.manual_rearm_required,
                metadata={"strategy_id": strategy_id, "quarantine_token": token},
            )

        return transition, _persist

    def quarantine(self, strategy_id: str, *, reason: str) -> AutonomyTransition:
        """Latch the strategy and persist the latch, synchronously.

        For callers that are not on the event loop -- the CLI, tests, boot-time
        paths. The trading loop must use :meth:`quarantine_async`.
        """
        transition, persist = self._latch_quarantine(strategy_id, reason=reason)
        persist()
        return transition

    def _log_persist_outcome(self, strategy_id: str, task: "asyncio.Future[None]") -> None:
        """Report a durable-write failure that nobody is waiting on any more."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "strategy_quarantine_persist_failed",
                strategy_id=strategy_id,
                error=str(exc),
                error_type=type(exc).__name__,
                consequence="latch held in memory only; a restart will not restore it",
            )

    async def quarantine_async(self, strategy_id: str, *, reason: str) -> AutonomyTransition:
        """Latch the strategy now; write the durable record off the event loop.

        ``StrategyRunner.process_event`` quarantines from inside the dispatch
        path, so the durable write ran on the event loop: ``locked_state`` polls
        ``flock`` with ``time.sleep(0.01)`` up to a two-second deadline and then
        reads and rewrites JSON. A CLI ``rearm-strategy`` or a second engine
        holding that lock would therefore stall feed, risk and order processing
        for up to two seconds -- against a 1 ms budget (``.agent/rules``
        ``01-core-laws.md``), and precisely while a strategy is failing.

        The latch itself stays inline and immediate; only the write moves. Two
        further properties matter, and neither is obvious:

        **Nothing waits for it.** An earlier version awaited the write for up
        to 200 ms "so the common case is fully reported". That reasoning was
        wrong twice over. This coroutine is awaited by ``StrategyRunner``'s
        *only* bus consumer, so every millisecond spent here is a millisecond
        the ``RingBufferBus`` is not drained -- for every strategy, not just the
        failing one -- against a 1 ms budget (``.agent/rules/01-core-laws.md``).
        And it bought nothing: ``transition`` is built by ``_latch_quarantine``
        before the task exists, the caller reads only ``transition.reason``, and
        ``_log_persist_outcome`` is a *done callback* that reports success or
        failure whether or not anyone awaited. A wait that yields no information
        and no ordering guarantee is pure dispatch latency.

        **The task is held, not just launched.** ``asyncio`` keeps only a weak
        reference to a running task. The old code's sole strong reference was
        the local ``task`` variable, alive exactly as long as the await -- so
        once the 200 ms timeout fired, the very write whose log line promised it
        "continues in the background" was eligible for garbage collection.
        ``_persist_tasks`` is the strong reference that makes that promise true.

        **It cannot raise into the dispatch path.** The call site is inside
        ``process_event``'s ``except`` handler, and ``process_event`` is awaited
        directly from ``async for event in self.bus.consume(...)``, which
        catches only ``CancelledError``. An exception here would therefore not
        merely fail the write -- it would tear down the strategy consumer and
        stop *all* strategies from receiving events, turning a disk problem into
        a total dispatch outage. The strategy is already latched and skipped in
        memory by this point, so the honest failure mode is a loud log saying
        the latch may not survive a restart.
        """
        transition, persist = self._latch_quarantine(strategy_id, reason=reason)
        try:
            # ``run_in_executor``, deliberately, not ``to_thread``:
            # ``to_thread`` is a coroutine, so ``ensure_future`` only *schedules*
            # the submission -- the work reaches the pool on the loop's next
            # iteration. ``asyncio.run`` cancels every pending task before it
            # joins the executor, so a quarantine raised as the last act before
            # shutdown produced a cancelled task whose outcome was discarded by
            # ``_log_persist_outcome``'s ``task.cancelled()`` guard: a failed
            # write, reported to nobody. ``run_in_executor`` submits
            # synchronously here and returns a Future rather than a Task, so
            # the cancel-all does not reach it and the outcome survives.
            task = asyncio.get_running_loop().run_in_executor(None, persist)
        except RuntimeError:
            # No running loop. The latch is already applied in memory, and
            # raising here would tear down the bus consumer (see above), so the
            # honest failure is a loud log, not an exception.
            logger.error(
                "strategy_quarantine_persist_not_scheduled",
                strategy_id=strategy_id,
                consequence="latch held in memory only; a restart will not restore it",
            )
            return transition
        self._persist_tasks.add(task)
        task.add_done_callback(self._persist_tasks.discard)
        task.add_done_callback(lambda t: self._log_persist_outcome(strategy_id, t))
        return transition

    def is_quarantined(self, strategy_id: str) -> bool:
        return strategy_id in self._quarantined

    def restore_persisted_quarantines(
        self,
        *,
        state_path: str | Path | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> list[str]:
        """Hydrate every strategy quarantine the previous run left latched.

        Without this, **a restart is an unauthenticated re-arm**: ``_quarantined``
        starts empty, ``manual_rearm_required{scope="strategy"}`` therefore reads
        0, and the ``ManualRearmRequired`` alert *resolves itself* -- reporting to
        the operator that a safety latch was handled when nothing handled it.

        Observed in production 2026-08-25T14:16:29Z: the engine was restarted
        with ``R47_MAKER_TMF`` quarantined, both ``ManualRearmRequired`` and
        ``StrategyQuarantineActive`` resolved 70 s later with no operator action,
        and the strategy resumed quoting -- while the persisted document still
        read ``manual_rearm_required: true``. The durable record was intact and
        correct; nothing ever read it back.

        **This is hydration, not a new quarantine.** The distinction is the whole
        design, and the first version got it backwards by re-entering through
        ``quarantine()`` to mint a fresh token. Three consequences, all wrong:

        1. An operator re-arm request published *before* the restart names the
           token of this same latch. Minting a new one makes that request look
           superseded, so the consumer unlinks it unacknowledged -- one restart
           silently destroys a legitimate authorization, and a restart loop
           destroys every retry. The token exists to stop an authorization for
           *quarantine instance N* from clearing *instance N+1*; a restart does
           not create an instance N+1. It is the same latch.
        2. Re-entering through the live path appends a transition, a digest and
           a manual-rearm record on every boot, and rewrites the whole ``events``
           array each time -- O(n^2) bytes under a restart loop, which is exactly
           when the disk can least afford it.
        3. Reading from ``state_path`` while writing through the evidence
           writer's own directory splits the two halves of the protocol, so a
           gate on the supplied path would never see the token it must name.

        So this path reuses the persisted token verbatim, sets the in-memory
        latch and the two gauges, and writes nothing. It is idempotent, and the
        structlog record below is the bounded boot evidence.

        Fail-closed on every reading it cannot trust: unparseable JSON raises
        ``RuntimeStateUnreadable``; a valid document with a malformed
        ``strategies`` section, a malformed entry, or a latched entry carrying no
        token raises ``StrategyQuarantineStateCorrupt``. A latch that cannot be
        read must not be assumed absent -- that assumption is the defect this
        method exists to remove. Only a *missing* section is a cold start.

        ``snapshot`` lets the caller supply state it has already read and
        validated, so the read can happen before any startup side effect exists
        to unwind. See the call site in ``services.bootstrap``.

        Returns the strategy ids restored, in sorted order.
        """
        from hft_platform.ops.manual_rearm import ManualRearmGate

        if snapshot is None:
            seen = parse_persisted_quarantines(ManualRearmGate(state_path=state_path).snapshot())
        else:
            # The supplied snapshot was read at the top of ``build()``, before
            # the session-ownership preflight. Re-read here to pick up anything
            # written since, and merge whole *records* rather than latched ones:
            #
            #   fresh says latched   -> latched, with the fresher identity
            #   fresh says cleared   -> cleared; an operator authorized that
            #   fresh never mentions -> whatever the snapshot said
            #
            # Merging only the latched set instead let a stale snapshot
            # resurrect a latch the fresh read showed as cleared, and since the
            # restore does not rewrite the record, memory said "quarantined"
            # while disk said "no re-arm required" -- a split brain the CLI
            # refuses to re-arm out of.
            #
            # NOT a fence. ``SystemBootstrapper._check_session_ownership`` is a
            # non-blocking preflight (B-OPS-03): on a conflicting owner, or with
            # Redis down, it logs, returns False and ``build()`` continues. So a
            # previous engine that is still alive can write after this read too.
            # The re-read narrows that window from the whole of ``build()`` to
            # the few instructions below it; closing it needs an exclusive
            # startup fence, which is a separate change with its own
            # availability tradeoff (an engine that refuses to start when Redis
            # is unreachable).
            seen = parse_persisted_quarantines(snapshot)
            try:
                fresh = parse_persisted_quarantines(ManualRearmGate(state_path=state_path).snapshot())
            except Exception as exc:
                # Do NOT raise: the supplied snapshot is itself a validated
                # reading of this file, so every latch it holds is still
                # applied below. Raising here would move a fail-closed read back
                # after the lease -- the crash loop this split exists to avoid.
                logger.error(
                    "quarantine_reread_failed",
                    error=str(exc),
                    note="applying the earlier snapshot; a latch written during startup may be missed",
                )
            else:
                seen = {**seen, **fresh}

        latched = {sid: rec for sid, rec in seen.items() if rec is not None}
        if not latched:
            self._set_strategy_scope_state()
            return []

        restored: list[str] = []
        legacy_tokens: dict[str, str] = {}
        for strategy_id in sorted(latched):
            persisted = latched[strategy_id]
            reason = persisted.reason
            token = persisted.token
            if token is None:
                # A latch written by an engine from before tokens existed. Refusing
                # to start would turn the upgrade that introduces this code into an
                # outage, and minting here destroys nothing: ``rearm_strategy``
                # refuses to publish a request for a tokenless entry, so no
                # authorization can be in flight for this latch. Verified against
                # the production document 2026-08-26, which carries exactly this
                # shape for ``R47_MAKER_TMF``.
                #
                # The latch is kept -- only its identity is filled in -- so the
                # migration is fail-closed, and it is the one case where restore
                # writes. It happens once: the next boot reads the token back.
                token = self._mint_token(strategy_id)
                legacy_tokens[strategy_id] = token
            # Built for ``build_cancel_intents``, deliberately NOT recorded: this
            # is the same transition the previous run already counted.
            transition = self._build_transition(from_mode=AutonomyMode.STRATEGY_QUARANTINED, reason=reason)
            self._quarantined[strategy_id] = StrategyQuarantine(
                strategy_id=strategy_id,
                reason=reason,
                transition=transition,
                token=token,
            )
            self._set_strategy_quarantine_active(strategy_id, active=True)
            self._restored_tokens.add(token)
            restored.append(strategy_id)

        self._set_strategy_scope_state()
        if legacy_tokens:
            self._persist_legacy_tokens(legacy_tokens, state_path=state_path)
        if restored:
            logger.warning(
                "strategy_quarantines_restored",
                strategy_ids=restored,
                count=len(restored),
                note="latch survived restart; the pre-restart re-arm token is still the one that clears it",
            )
        return restored

    def _persist_legacy_tokens(self, tokens: dict[str, str], *, state_path: str | Path | None) -> None:
        """Give pre-token latches an identity so the authorized re-arm path can name them.

        Best-effort by design: the latch is already held in memory, so a failed
        write leaves the strategy *stopped* and merely un-clearable until the
        next boot retries. That is the fail-closed direction. It is logged at
        error level because the operator needs to know why ``rearm-strategy``
        would refuse.
        """
        from hft_platform.ops.manual_rearm import DEFAULT_RUNTIME_STATE_PATH
        from hft_platform.ops.runtime_state_store import locked_state

        path = Path(state_path) if state_path is not None else DEFAULT_RUNTIME_STATE_PATH
        adopted: dict[str, str] = {}
        try:
            with locked_state(path) as state:
                strategies = state.get("strategies")
                if not isinstance(strategies, dict):
                    return
                for strategy_id, token in tokens.items():
                    entry = strategies.get(strategy_id)
                    if not isinstance(entry, dict):
                        continue
                    # Re-check under the lock: another engine migrating the same
                    # tokenless latch may have got here first, and overwriting
                    # its token would strand an in-flight request. Keyed on
                    # *absence*, matching the parse: a present-but-malformed
                    # token never reaches here (it is corrupt, not legacy), and
                    # testing truthiness instead meant a malformed value blocked
                    # its own replacement forever.
                    existing = entry.get("quarantine_token")
                    if "quarantine_token" not in entry:
                        entry["quarantine_token"] = token
                    elif isinstance(existing, str) and existing and existing != token:
                        # Someone else's token won the lock. Keeping ours would
                        # split the latch's identity: the CLI reads *their*
                        # token off disk and this governor would reject every
                        # request naming it, leaving a quarantine no operator
                        # can clear. Disk is the authority -- adopt it.
                        adopted[strategy_id] = existing
        except Exception as exc:
            logger.error(
                "strategy_quarantine_legacy_token_persist_failed",
                strategy_ids=sorted(tokens),
                error=str(exc),
                note="latch is held in memory but cannot be re-armed until a boot persists a token",
            )
            return
        for strategy_id, token in adopted.items():
            held = self._quarantined.get(strategy_id)
            if held is not None:
                self._quarantined[strategy_id] = replace(held, token=token)
        if adopted:
            logger.warning(
                "strategy_quarantine_legacy_token_adopted",
                strategy_ids=sorted(adopted),
                note="another start migrated this latch first; adopted its token so the CLI and this governor agree",
            )
        minted = sorted(set(tokens) - set(adopted))
        if minted:
            logger.warning(
                "strategy_quarantine_legacy_tokens_minted",
                strategy_ids=minted,
                note="latch predates quarantine tokens; identity filled in so it can be re-armed",
            )

    def reconcile_persisted_quarantines(self, snapshot: Any) -> list[str]:
        """Adopt latches this process does not hold. Never touch one it does.

        ``restore_persisted_quarantines`` reads once, at boot, and session
        ownership is a non-blocking preflight -- so a previous engine that is
        still alive can persist a quarantine a moment after that read. Nothing
        looked at strategy latch state again: the supervisor tick only consumes
        re-arm *request* files. The new engine therefore never learned about
        that latch and kept dispatching the strategy, which is a fail-open on
        the one piece of state that exists to stop it.

        Strictly additive, and that is the whole difference from the boot
        restore. The restore *replaces* the in-memory record, which is right
        when nothing is in memory and wrong on a tick: ``quarantine_async``
        applies the latch before its write lands, so a tick landing in that
        window would overwrite the new token with the previous one still on
        disk -- silently breaking the re-arm protocol the token exists for.

        A tokenless legacy latch is skipped rather than minted here. Minting is
        a *write*, the boot restore already owns that migration, and doing it on
        a tick would race the other engine that is still writing this document.

        Returns the strategy ids adopted, in sorted order.
        """
        latched = {sid: rec for sid, rec in parse_persisted_quarantines(snapshot).items() if rec is not None}
        adopted: list[str] = []
        for strategy_id in sorted(latched):
            if strategy_id in self._quarantined:
                continue
            persisted = latched[strategy_id]
            if persisted.token is None:
                continue
            transition = self._build_transition(
                from_mode=AutonomyMode.STRATEGY_QUARANTINED,
                reason=persisted.reason,
            )
            self._quarantined[strategy_id] = StrategyQuarantine(
                strategy_id=strategy_id,
                reason=persisted.reason,
                transition=transition,
                token=persisted.token,
            )
            self._set_strategy_quarantine_active(strategy_id, active=True)
            # Adopted from another engine's write: the token carries *its* run
            # id, but the latch is ours to clear now, so a request naming it is
            # ours to judge. See ``owns_token``.
            self._restored_tokens.add(persisted.token)
            adopted.append(strategy_id)
        if adopted:
            self._set_strategy_scope_state()
            logger.warning(
                "strategy_quarantines_adopted",
                strategy_ids=adopted,
                count=len(adopted),
                note="written by another engine after this one restored at boot",
            )
        return adopted

    def rearm(self, strategy_id: str, *, expected_token: str, request_id: str | None = None) -> bool:
        """Clear one strategy's quarantine, and only the exact one authorized.

        ``expected_token`` names a specific quarantine *instance*, so an
        authorization issued for an earlier failure can never release a later
        one. The comparison and the removal happen together, with no await and
        no IO between them, so nothing can interleave: on the event loop this is
        atomic by construction.

        The evidence write follows the decision rather than gating it, and a
        failed write must not block a recovery the operator already authorized.
        Since ``restore_persisted_quarantines`` exists, that record is also the
        durability mechanism, so losing it has a consequence worth naming: the
        persisted document keeps ``manual_rearm_required: true`` and the next
        restart re-latches a strategy that was legitimately re-armed. That is
        the *fail-closed* direction -- the strategy stops rather than trades --
        which is why the ordering stands. It is recorded best-effort and logged
        loudly on failure so the operator can re-issue the request.

        Returns ``True`` only when a live quarantine was actually cleared.
        """
        cleared, persist = self._clear_quarantine(strategy_id, expected_token=expected_token, request_id=request_id)
        if cleared:
            persist()
        return cleared

    def _clear_quarantine(
        self,
        strategy_id: str,
        *,
        expected_token: str,
        request_id: str | None,
    ) -> tuple[bool, Callable[[], None]]:
        """Apply the re-arm decision; return it with the durable write deferred.

        The mirror of ``_latch_quarantine``, and for the same reason: the
        decision is a dict comparison that must be atomic on the loop, and the
        record is a lock-then-rewrite that must not be.
        """
        entry = self._quarantined.get(strategy_id)
        if entry is None or entry.token != expected_token:
            return False, _noop_persist

        # Decide and mutate with nothing in between.
        self._quarantined.pop(strategy_id, None)
        self._set_strategy_quarantine_active(strategy_id, active=False)
        self._set_strategy_scope_state()
        logger.info(
            "strategy_rearmed",
            strategy_id=strategy_id,
            quarantine_token=entry.token,
            request_id=request_id,
        )

        writer = self.evidence_writer
        token = entry.token

        def _persist() -> None:
            if writer is None:
                return
            try:
                writer.record_transition(
                    scope="strategy",
                    mode=AutonomyMode.NORMAL.value,
                    reason="manual_rearm",
                    manual_rearm_required=False,
                    metadata={
                        "strategy_id": strategy_id,
                        "quarantine_token": token,
                        "request_id": request_id,
                    },
                )
            except Exception as exc:
                # The strategy is re-armed either way; losing the audit record
                # must not be silent.
                logger.error(
                    "strategy_rearm_evidence_write_failed",
                    strategy_id=strategy_id,
                    request_id=request_id,
                    error=str(exc),
                )

        return True, _persist

    async def rearm_async(self, strategy_id: str, *, expected_token: str, request_id: str | None = None) -> bool:
        """Re-arm now; write the durable record off the event loop.

        The supervisor consumes operator re-arm requests on its tick, which runs
        on the event loop, and the record goes through ``locked_state`` -- the
        same bounded ``flock`` poll and JSON rewrite that made
        ``quarantine_async`` necessary. Fixing one side and leaving the other
        would keep the stall exactly on the path an operator triggers by hand.
        """
        cleared, persist = self._clear_quarantine(strategy_id, expected_token=expected_token, request_id=request_id)
        if cleared:
            await asyncio.to_thread(persist)
        return cleared

    def build_cancel_intents(
        self,
        strategy_id: str,
        *,
        live_orders: Iterable[tuple[str, str]],
        intent_factory,
        source_ts_ns: int | None = None,
        trace_id: str | None = None,
    ) -> list[Any]:
        quarantine = self._quarantined.get(strategy_id)
        if quarantine is None:
            return []

        tagged_reason = f"strategy_quarantined:{quarantine.transition.reason}"
        intents = []
        for symbol, order_id in live_orders:
            intent = intent_factory(
                strategy_id=strategy_id,
                symbol=symbol,
                side=Side.BUY,
                price=0,
                qty=0,
                tif=TIF.LIMIT,
                intent_type=IntentType.CANCEL,
                target_order_id=order_id,
                source_ts_ns=source_ts_ns,
                trace_id=trace_id,
            )
            intent = self._tag_intent_reason(intent, tagged_reason)
            intents.append(intent)
        return intents

    def _set_strategy_quarantine_active(self, strategy_id: str, *, active: bool) -> None:
        if not self.metrics:
            return
        metric = getattr(self.metrics, "strategy_quarantine_active", None)
        if metric is None:
            return
        metric.labels(strategy=strategy_id).set(1 if active else 0)

    def _set_strategy_scope_state(self) -> None:
        if not self.metrics:
            return
        mode = AutonomyMode.STRATEGY_QUARANTINED if self._quarantined else AutonomyMode.NORMAL

        autonomy_mode = getattr(self.metrics, "autonomy_mode", None)
        if autonomy_mode is not None:
            autonomy_mode.labels(scope="strategy").set(_AUTONOMY_MODE_VALUES[mode])

        manual_rearm_required = getattr(self.metrics, "manual_rearm_required", None)
        if manual_rearm_required is not None:
            manual_rearm_required.labels(scope="strategy").set(1 if self._quarantined else 0)

    def _build_transition(self, *, from_mode: AutonomyMode, reason: str) -> AutonomyTransition:
        transition = AutonomyTransition(
            scope="strategy",
            from_mode=from_mode,
            to_mode=AutonomyMode.STRATEGY_QUARANTINED,
            reason=reason,
            manual_rearm_required=True,
        )
        if transition.metric_reason != "unknown":
            return transition

        strategy_reason = f"strategy_{reason}"
        mapped_transition = AutonomyTransition(
            scope="strategy",
            from_mode=from_mode,
            to_mode=AutonomyMode.STRATEGY_QUARANTINED,
            reason=strategy_reason,
            manual_rearm_required=True,
        )
        if mapped_transition.metric_reason != "unknown":
            return mapped_transition
        return transition

    def _tag_intent_reason(self, intent: Any, reason: str) -> Any:
        if isinstance(intent, tuple) and len(intent) >= 16 and intent[0] == "typed_intent_v1":
            tagged_intent = list(intent)
            tagged_intent[12] = reason
            return tuple(tagged_intent)
        if isinstance(intent, dict):
            intent["reason"] = reason
            return intent
        if hasattr(intent, "reason"):
            intent.reason = reason
        return intent
