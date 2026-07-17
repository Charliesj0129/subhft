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

These resolvers feature-detect at call time so a single code path is correct
on both SDKs and stops touching the deprecated surface on 1.5.x. No SDK import
happens at module load.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


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
        if hasattr(item, "code"):
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
        if hasattr(item, "code"):
            groups.setdefault(str(getattr(item, "category", "") or ""), []).append(item)
        else:
            items = list(item)
            root = str(getattr(items[0], "category", "") or "") if items else ""
            groups.setdefault(root, []).extend(items)
    return groups
