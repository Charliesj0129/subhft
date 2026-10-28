"""Dual-version Shioaji compatibility resolvers.

The adapter must run on both the legacy 1.3.3 SDK and the target 1.5.6 SDK.
Across the 1.5.x line the quote API moved:

* enums ``QuoteType`` / ``QuoteVersion`` moved from ``sj.constant.*`` to
  top-level ``sj.*`` (the ``sj.constant`` shim survives in 1.5.x only as a
  ``DeprecationWarning``);
* ``subscribe`` / ``unsubscribe`` / the v1 quote-callback setters /
  ``set_event_callback`` moved from the ``api.quote`` proxy to the top-level
  ``api`` (the proxy survives in 1.5.x only as a ``DeprecationWarning``);
* the v0 quote-callback setters were removed from the 1.5.x API (the adapter
  already feature-detects v0 vs v1, so the resolver lets that probe report
  "unsupported" truthfully).

The 1.5.x execution callbacks have the same shape problem in the other
direction: they deliver Rust mapping types (``OrderEventDict``,
``EventOrderStatusDict``, ``OperationDict``, ...) that implement the whole
mapping protocol but are neither ``dict`` subclasses nor registered with
``collections.abc.Mapping``. :func:`to_plain_payload` normalizes them back to
plain containers at the boundary — see its docstring for what that cost the
platform in production.

These resolvers feature-detect at call time so a single code path is correct
on both SDKs and stops touching the deprecated surface on 1.5.x. No SDK import
happens at module load.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

from structlog import get_logger

from hft_platform.feed_adapter.shioaji._infra import scrub_broker_error

logger = get_logger("feed_adapter.shioaji.compat")

# Real execution payloads nest 2-3 deep (event -> order/status/contract ->
# scalars). The cap is generous enough to never truncate one while still
# terminating on a self-referencing mapping, which would otherwise take the
# broker callback thread down with a RecursionError.
_MAX_PAYLOAD_DEPTH = 12


def _is_sdk_mapping(obj: Any) -> bool:
    """True for a mapping-shaped object that ``isinstance(obj, dict)`` rejects.

    Detection is structural rather than by type name so it also covers the
    sibling Rust types (``EventOrderStatusDict``, ``OperationDict``,
    ``FuturesOrderDetailDict``, ...) and survives the SDK renaming any of them.
    """
    if isinstance(obj, (dict, str, bytes, bytearray)):
        return False
    keys = getattr(obj, "keys", None)
    return callable(keys) and hasattr(obj, "__getitem__")


def to_plain_payload(obj: Any, _depth: int = 0) -> Any:
    """Recursively convert shioaji 1.5.x Rust payloads into plain dicts/lists.

    ``shioaji._core.OrderEventDict`` and its siblings expose ``keys`` ``values``
    ``items`` ``get`` ``__getitem__`` ``__iter__`` ``__len__`` ``__contains__``
    — everything except being a ``dict``. They are structurally mappings and
    nominally nothing, so they fail every ``isinstance(x, dict)`` gate
    downstream *and* ``orjson``, which serializes ``dict`` rather than
    dict-shaped things.

    Measured consequence on THESHOW 2026-08-10, on the first two orders in two
    months: ``normalize_order`` returned ``None`` at its ``isinstance`` guard
    without logging, ``normalize_fill`` read ``qty`` as 0 through the ``getattr``
    fallback a Rust mapping does not answer, and the WAL write raised
    ``Type is not JSON serializable``. No callback reached the strategy, whose
    pending counters therefore never decremented, and it stopped quoting for two
    days across 31.4M events.

    Conversion is recursive because ``normalize_order`` rejects the whole event
    when the nested ``order`` is not a dict, so a shallow copy still drops it.
    This runs on the execution callback path (a few events per second), not the
    market-data tick path, so rebuilding the containers is not a Core Law #1
    concern.

    Plain payloads — everything the 1.3.3 SDK delivers — pass through with the
    same contents, so this is a no-op on the legacy SDK.
    """
    if _depth >= _MAX_PAYLOAD_DEPTH:
        return None
    if _is_sdk_mapping(obj):
        return {str(k): to_plain_payload(obj[k], _depth + 1) for k in obj.keys()}
    if isinstance(obj, dict):
        return {k: to_plain_payload(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain_payload(v, _depth + 1) for v in obj]
    return obj


def resolve_quote_enum(sj: Any, name: str) -> Any:
    """Return a quote enum (``QuoteType`` / ``QuoteVersion``) from whichever
    location the installed SDK exposes.

    Prefers the 1.5 top-level ``sj.<name>`` and falls back to the 1.3.3
    ``sj.constant.<name>``. Raises ``AttributeError`` if neither exists.
    """
    enum = getattr(sj, name, None)
    if enum is not None:
        return enum
    const = getattr(sj, "constant", None)
    enum = getattr(const, name, None) if const is not None else None
    if enum is None:
        raise AttributeError(f"Shioaji exposes neither {name} nor constant.{name}")
    return enum


def resolve_quote_api(api: Any) -> Any | None:
    """Return the object that owns the quote API (``subscribe`` / setters /
    ``set_event_callback``).

    1.5 exposes these on the top-level ``api``; 1.3.3 only on the ``api.quote``
    proxy. Preferring the top-level ``api`` when it carries ``subscribe`` /
    ``unsubscribe`` migrates the whole quote surface off the deprecated proxy
    on 1.5.x in one place, while transparently falling back to the proxy on
    1.3.3. Returns ``None`` when no usable quote surface is available (e.g.
    not logged in), so callers fail closed instead of dispatching onto a stub.
    """
    if api is None:
        return None
    if hasattr(api, "subscribe") and hasattr(api, "unsubscribe"):
        return api
    quote = getattr(api, "quote", None)
    if quote is not None and hasattr(quote, "subscribe") and hasattr(quote, "unsubscribe"):
        return quote
    return None


def is_leaf_contract(item: Any) -> bool:
    """True when ``item`` is a contract rather than a group of contracts.

    1.5.x iterates a category flat (contracts); 1.7.x yields ``ContractGroup``
    objects whose ``__getattr__`` looks the name up as a contract code and
    raises ``KeyError`` on a miss. ``hasattr`` only swallows
    ``AttributeError``, so ``hasattr(group, "code")`` escapes with
    ``KeyError('code')`` (THESHOW 1.7.9 deploy, 2026-10-27: family populator
    failed and R47 bound no symbols).
    """
    try:
        item.code  # noqa: B018 - attribute probe; a group raises instead of returning
    except (AttributeError, LookupError):
        return False
    return True


def contract_member(container: Any, name: str) -> Any | None:
    """``getattr(container, name, None)`` that also maps a lookup miss to ``None``.

    1.7.x ``ContractCategory`` / ``ContractGroup`` resolve attributes as group
    or contract codes and raise ``KeyError`` for an unknown one, which
    ``getattr``'s default does not catch (see :func:`is_leaf_contract`).
    """
    if container is None:
        return None
    try:
        return getattr(container, name)
    except (AttributeError, LookupError):
        return None


def iter_contract_category(category: Any) -> Iterator[Any]:
    """Yield leaf contract objects from a ``Contracts.<Futures|Options|Stocks>``
    category across SDK generations.

    1.3.3 categories are pydantic dict-likes: ``.keys()`` yields root groups
    (``TXF``, ``TXO``, ...) and ``category[root]`` iterates that group's
    contracts. The 1.5.x Rust core drops the dict protocol (``.keys()`` raises
    ``ContractCategory 'FUT' has no group 'keys'``) and instead iterates flat,
    yielding contract objects directly (official 1.5.6 CONTRACTS.md pattern:
    ``[c for c in api.Contracts.Futures]``).
    """
    try:
        roots = list(category.keys())
    except Exception:
        roots = None
    if roots is not None:
        for root in roots:
            yield from category[root]
        return
    for item in category:
        if is_leaf_contract(item):
            yield item
        else:
            yield from item


def contract_category_groups(category: Any) -> dict[str, list[Any]]:
    """Return ``{root: [contracts...]}`` for a contract category across SDK
    generations (see :func:`iter_contract_category` for the version split).

    On 1.5.x the flat contract stream is regrouped by each contract's
    ``category`` attribute (``TXF`` / ``TXO`` / ...); contracts without one
    land under ``""`` so callers can decide whether to skip them.
    """
    try:
        roots = list(category.keys())
    except Exception:
        roots = None
    if roots is not None:
        return {str(root): list(category[root]) for root in roots}
    groups: dict[str, list[Any]] = {}
    for item in category:
        if is_leaf_contract(item):
            groups.setdefault(str(getattr(item, "category", "") or ""), []).append(item)
        else:
            items = list(item)
            root = str(getattr(items[0], "category", "") or "") if items else ""
            groups.setdefault(root, []).extend(items)
    return groups


def resolve_trading_account(api: Any, attr: str) -> Any | None:
    """Return ``api.<attr>`` (``stock_account`` / ``futopt_account``) or ``None``.

    1.3.3 exposes the default-account attributes as plain values that are
    simply ``None`` before login. The 1.5.x Rust core turns them into
    properties that raise ``AuthError: Not authenticated`` until a session
    exists — an exception that neither ``getattr(..., default)`` nor
    ``hasattr`` swallows (``AuthError`` is not an ``AttributeError``), so the
    1.3.3-era guards crash at ``HFTSystem`` construction and in logged-out
    reconnect windows. Mapping any raising accessor to ``None`` restores the
    1.3.3 "no account available yet" semantics on both SDKs. The broad catch
    is deliberate: the exception type lives at different import paths across
    SDK generations, and this resolver's only contract is "account or None".
    """
    if api is None:
        return None
    try:
        return getattr(api, attr, None)
    except Exception:
        return None


# The categories this platform subscribes or trades (config/symbols.yaml holds
# futures, options and TSE stocks). Indexs is left out on purpose: nothing
# depends on it, and waiting on it would only add a way to never be ready.
_READY_CATEGORIES = ("Futures", "Options", "Stocks")


def contracts_loaded(api: Any) -> bool:
    """True once every category in ``_READY_CATEGORIES`` is non-empty.

    ``hasattr(api, "Contracts")`` is always True once logged in, and the 1.7.x
    contracts object has no ``status`` attribute at runtime although its stub
    declares one (live probe 2026-10-27), so "the category has something in it"
    is the only readiness signal that holds on every SDK generation. Plain
    iteration is used on purpose: 1.5 yields contracts and 1.7 yields groups,
    and telling those apart would mean probing a Rust ``__getattr__`` whose
    exception type is not pinned.
    """
    try:
        contracts = api.Contracts
        for name in _READY_CATEGORIES:
            if next(iter(getattr(contracts, name)), None) is None:
                return False
    except Exception:
        return False
    return True


def wait_contracts_ready(
    api: Any,
    *,
    timeout_s: float,
    fetch_after_s: float,
    fetch: Callable[[], Any] | None = None,
    poll_s: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, float, bool]:
    """Wait for the contracts that ``login()`` loads; fetch at most once.

    From shioaji 1.7 ``login()`` starts loading contracts itself, and an
    explicit ``fetch_contracts`` issued meanwhile fails at once with "exclusive
    access lost (concurrent API call started)" (probe 2026-10-27, 0.0 ms). The
    1.5-era login treated that as a failed login and logged in a second time
    on the same session. So: poll until contracts resolve, call ``fetch`` only
    if nothing has loaded by ``fetch_after_s``, and treat a failed fetch as
    "keep waiting". Never logs in again. Blocking; call it off the event loop.

    Returns ``(ready, elapsed_s, fetched)``.
    """
    start = clock()
    fetched = False
    while True:
        elapsed = clock() - start
        if contracts_loaded(api):
            return True, elapsed, fetched
        if elapsed >= timeout_s:
            return False, elapsed, fetched
        if fetch is not None and not fetched and elapsed >= fetch_after_s:
            fetched = True
            try:
                fetch()
            except Exception as exc:
                # "exclusive access lost" included: the load in flight may still finish.
                logger.warning("contracts_fetch_failed_waiting", error=scrub_broker_error(exc))
            continue
        sleep(poll_s)
