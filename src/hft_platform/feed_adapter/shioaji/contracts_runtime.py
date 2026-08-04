from __future__ import annotations

import datetime as dt
import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.feed_adapter.shioaji._compat import iter_contract_category
from hft_platform.feed_adapter.shioaji._infra import (
    acquire_login_slot,
    client_float,
    release_login_slot,
)

logger = get_logger("feed_adapter.contracts_runtime")

try:
    import shioaji as sj
except Exception:  # pragma: no cover
    sj = None

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji.client import ShioajiClient

# Month number → TAIFEX month letter (A=Jan .. L=Dec)
_MONTH_LETTERS = "ABCDEFGHIJKL"

#: Bounded label values for ``contract_update_events_total``. The SDK declares
#: ``ContractAction`` = FORCE|CHECK and ``ContractUpdateSecurityType`` =
#: ALL|IND|STK|FUT|OPT (``_core.pyi:116-126``), but these arrive as foreign enum
#: objects whose ``str()`` we do not control, and a future SDK may add variants.
#: Anything unrecognised collapses to ``other`` rather than minting a new
#: Prometheus series — same discipline as ``classify_login_failure``.
_CONTRACT_EVENT_ACTIONS = frozenset({"force", "check"})
_CONTRACT_EVENT_SECURITY_TYPES = frozenset({"all", "ind", "stk", "fut", "opt"})


@dataclass(frozen=True)
class _SharedRebuild:
    """One cycle's contract rebuild, shared across the pooled facades.

    The rebuild is process-global data wearing a per-facade coat. Every pooled
    facade resolves ``HFT_CONTRACT_CACHE_PATH`` to the *same* file
    (``config/contracts.json``, 13 MB on THESHOW) and every facade is logged in
    to the same broker account, so their SDK contract stores are identical.
    Measured on THESHOW 2026-08-03: four facades produced identical diffs on
    every hourly cycle (54 727 contracts, ``removed_count`` and
    ``relevant_count`` matching to the digit), each paying a full read → walk →
    write, spanning ~11 s per hour of largely GIL-held work.

    Only the *global* half of the cycle lives here. ``relevant_count`` stays
    per-facade because each facade subscribes to its own ~74-symbol shard, and
    that half is a set intersection over 74 codes — the cheap part. ``added`` /
    ``removed`` are kept whole (not the truncated ``[:200]`` log lists) for the
    same reason ``_compute_diff_payload`` computes relevance pre-truncation.

    Nothing large is retained: adds/removes are single digits in a normal cycle,
    so this holds a few dozen strings, not the 54 727-contract list.
    """

    mono: float
    contracts_before: int
    contracts_after: int
    added: tuple[str, ...]
    removed: tuple[str, ...]


#: Guards ``_shared_rebuild``. Distinct from ``ShioajiClient._contract_refresh_lock``,
#: which is *per client* and therefore serialises nothing across pooled facades.
_shared_rebuild_lock = threading.Lock()
_shared_rebuild: _SharedRebuild | None = None


def _publish_shared_rebuild(rebuild: _SharedRebuild) -> None:
    global _shared_rebuild
    with _shared_rebuild_lock:
        _shared_rebuild = rebuild


def _take_shared_rebuild(max_age_s: float) -> _SharedRebuild | None:
    """Return this cycle's rebuild if another facade already did the work.

    ``max_age_s`` must stay well under the refresh interval so a facade whose
    thread has drifted does its own rebuild rather than silently inheriting a
    stale one. The pooled facades arrive within ~11 s of each other in practice.
    """
    with _shared_rebuild_lock:
        shared = _shared_rebuild
    if shared is None:
        return None
    if (time.monotonic() - shared.mono) > max_age_s:
        return None
    return shared


def _reset_shared_rebuild() -> None:
    """Test hook — the module global would otherwise leak between tests."""
    global _shared_rebuild
    with _shared_rebuild_lock:
        _shared_rebuild = None


def _classify_contract_event_field(value: Any, allowed: frozenset[str]) -> str:
    """Map an SDK enum (or anything else) onto a bounded lowercase label.

    The enums stringify as ``ContractAction.FORCE`` / ``ContractUpdateSecurityType.FUT``
    depending on SDK version, so take the trailing dotted segment and only accept
    it if it is a known variant.
    """
    if value is None:
        return "unknown"
    raw = getattr(value, "name", None) or str(value)
    token = str(raw).rsplit(".", 1)[-1].strip().lower()
    return token if token in allowed else "other"


def _compute_diff_payload(
    *,
    version: int,
    codes_before: set[str],
    codes_after: set[str],
    subscribed: set[str],
) -> dict[str, Any]:
    """Build the ``_contract_refresh_last_diff`` payload for one refresh cycle.

    ``relevant_count`` / ``relevant_codes`` capture the intersection of
    ``(added | removed)`` with currently-subscribed codes and are computed
    on the **full** add/remove sets — not on the truncated ``[:200]`` log
    lists. The 2026-05-12 resubscribe storm hit because a 680-removed-code
    cleanup used the truncated lists for the relevance decision, missing
    the overlap that sorted past position 200.

    The parameter is named ``subscribed`` (not ``subscribed_codes``) so the
    rebind-guard regex in ``test_resubscribe_concurrent.py`` doesn't flag
    callers that pass the live ``c.subscribed_codes`` set by keyword.
    """
    return _diff_payload_from_delta(
        version=version,
        contracts_before=len(codes_before),
        contracts_after=len(codes_after),
        added=sorted(codes_after - codes_before),
        removed=sorted(codes_before - codes_after),
        subscribed=subscribed,
    )


def _diff_payload_from_delta(
    *,
    version: int,
    contracts_before: int,
    contracts_after: int,
    added: Sequence[str],
    removed: Sequence[str],
    subscribed: set[str],
) -> dict[str, Any]:
    """Build the diff payload from an already-computed add/remove delta.

    Split out of ``_compute_diff_payload`` so a pooled facade reusing another
    facade's rebuild pays only for its own ``relevant_*`` fields — the add/remove
    delta is identical across facades, the subscription shard is not.
    """
    relevant = (set(added) | set(removed)) & set(subscribed or ())
    return {
        "version": int(version),
        "contracts_before": int(contracts_before),
        "contracts_after": int(contracts_after),
        "added_count": len(added),
        "removed_count": len(removed),
        "added_codes": list(added[:200]),
        "removed_codes": list(removed[:200]),
        "relevant_count": len(relevant),
        "relevant_codes": sorted(relevant)[:50],
    }


def _diff_should_resubscribe(diff: dict[str, Any]) -> bool:
    """Gate the ``policy='diff'`` resubscribe branch on diff relevance.

    Prefers ``relevant_count`` (computed pre-truncation by
    ``_compute_diff_payload``). Falls back to the legacy "any add/remove
    is relevant" check when the diff dict lacks ``relevant_count`` — keeps
    backwards-compat with diff dicts produced before this helper landed
    (older crash-recovery state files, test fixtures, etc.).
    """
    if "relevant_count" in diff:
        return int(diff.get("relevant_count") or 0) > 0
    return bool(diff.get("added_codes") or diff.get("removed_codes"))


def _contract_type_counts(contracts: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"stock": 0, "future": 0, "option": 0, "index": 0, "other": 0}
    for contract in contracts:
        kind = str(contract.get("type", "") or "").strip().lower()
        if kind in counts:
            counts[kind] += 1
        else:
            counts["other"] += 1
    return counts


def _symbols_require_derivative_contracts(symbols: list[dict[str, Any]]) -> bool:
    for sym in symbols:
        exchange = str(sym.get("exchange", "") or "").strip().upper()
        if exchange in {"TAIFEX", "FUT", "OPT"}:
            return True
    return False


def _read_contract_status(api: Any) -> str:
    """Return ``api.Contracts.status`` as text, or ``"missing"`` when absent.

    Reported when the refresh re-reads the SDK's contract store, so the log says
    which load it is serialising rather than implying a broker round-trip.
    """
    contracts = getattr(api, "Contracts", None)
    if contracts is None:
        return "missing"
    return str(getattr(contracts, "status", None))


class StaleInstrumentError(Exception):
    """Raised when a contract's delivery_date is strictly before today.

    Same-day expiry (rollover day) is permitted: the front month must remain
    tradeable on the day the previous month rolls off. Only contracts whose
    delivery_date is in the past trigger this error.
    """

    __slots__ = ("code", "delivery_date")

    def __init__(self, *, code: str, delivery_date: dt.date) -> None:
        self.code = code
        self.delivery_date = delivery_date
        super().__init__(
            f"stale_instrument_subscription_blocked: code={code!r} delivery_date={delivery_date.isoformat()}"
        )


def _coerce_delivery_date(value: Any) -> dt.date:
    """Accept a date object or a YYYYMMDD/YYYY-MM-DD/YYYY/MM/DD string."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).replace("/", "").replace("-", "")
    if len(s) >= 8:
        return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    raise ValueError(f"unparseable delivery_date: {value!r}")


def assert_not_expired(contract: Any, *, today: dt.date) -> None:
    """Refuse to subscribe to expired contracts (rollover-day-safe).

    Strict less-than: ``delivery_date < today`` raises ``StaleInstrumentError``.
    Same-day expiry (``delivery_date == today``) is permitted so the front
    month remains tradeable through its rollover day.

    ``today`` is keyword-only and required so callers commit to a specific
    date (improves testability and freezes time at the boundary).
    """
    raw = getattr(contract, "delivery_date", None)
    if raw is None:
        return  # contract has no delivery_date → not a dated contract
    delivery_date = _coerce_delivery_date(raw)
    if delivery_date < today:
        code = str(getattr(contract, "code", "<unknown>"))
        raise StaleInstrumentError(code=code, delivery_date=delivery_date)


def assert_no_stale_subscriptions(
    symbols: Any,
    lookup: Any,
    *,
    today: dt.date,
    log: Any = logger,
) -> None:
    """Iterate subscribed symbols, refuse startup if any contract is expired.

    ``symbols`` is the broker client's ``symbols`` list (each entry a mapping
    with ``code`` / ``exchange`` keys plus a product-type key). ``lookup`` is
    a broker-supplied callable ``(exchange, code, product_type) -> contract |
    None`` — typically a thin wrapper around ``md_client._get_contract``.

    On stale contract: emits a structlog ``stale_instrument_subscription_blocked``
    error event with the offending code + delivery_date, then re-raises so
    bootstrap fails closed.
    """
    for sym in symbols:
        if not isinstance(sym, dict):
            continue
        code = sym.get("code")
        exchange = sym.get("exchange")
        if not code or not exchange:
            continue
        product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
        contract = lookup(str(exchange), str(code), product_type)
        if contract is None:
            continue
        try:
            assert_not_expired(contract, today=today)
        except StaleInstrumentError as exc:
            log.error(
                "stale_instrument_subscription_blocked",
                code=exc.code,
                delivery_date=exc.delivery_date.isoformat(),
                today=today.isoformat(),
            )
            raise


def derive_callback_code(contract: Any, config_code: str) -> str:
    """Derive the actual callback code from a Shioaji contract object.

    For R1/R2/C0/C1 continuous contracts, contract.code equals the alias
    (e.g. "TMFR1") but quote callbacks arrive with the resolved month code
    (e.g. "TMFE6"). This function reconstructs the month code from the
    contract's delivery_month or delivery_date attribute.

    Returns config_code unchanged if derivation fails or isn't needed.
    """
    # Only attempt derivation for alias-style codes
    suffix = config_code[-2:] if len(config_code) >= 4 else ""
    is_alias = suffix in ("R1", "R2", "C0", "C1")
    if not is_alias:
        # Regular month code — check contract.code directly
        actual = getattr(contract, "code", None)
        return actual if actual and actual != config_code else config_code

    # Extract root symbol (e.g. "TMF" from "TMFR1")
    root = config_code[:-2]

    # Try delivery_month first (format: "YYYY/MM" or "YYYYMM")
    dm = getattr(contract, "delivery_month", None)
    if dm:
        dm_str = str(dm).replace("/", "")
        if len(dm_str) >= 6:
            try:
                month = int(dm_str[4:6])
                year_digit = int(dm_str[3])  # last digit of year
                if 1 <= month <= 12:
                    letter = _MONTH_LETTERS[month - 1]
                    return f"{root}{letter}{year_digit}"
            except (ValueError, IndexError):
                pass

    # Fallback: try delivery_date (format: "YYYY/MM/DD" or "YYYYMMDD")
    dd = getattr(contract, "delivery_date", None)
    if dd:
        dd_str = str(dd).replace("/", "").replace("-", "")
        if len(dd_str) >= 8:
            try:
                month = int(dd_str[4:6])
                year_digit = int(dd_str[3])
                if 1 <= month <= 12:
                    letter = _MONTH_LETTERS[month - 1]
                    return f"{root}{letter}{year_digit}"
            except (ValueError, IndexError):
                pass

    # Could not derive — fall back to contract.code
    actual = getattr(contract, "code", None)
    return actual if actual else config_code


class ContractsRuntime:
    """Contracts cache/preflight/refresh runtime."""

    __slots__ = ("_client",)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client

    def _get_contract(
        self,
        exchange: str,
        code: str,
        product_type: str | None = None,
        allow_synthetic: bool = False,
    ) -> Any | None:
        if not self._client.api:
            return None
        if not hasattr(self._client.api, "Contracts"):
            ensure_contracts = getattr(self._client, "_ensure_contracts", None)
            if callable(ensure_contracts):
                ensure_contracts()
        if not hasattr(self._client.api, "Contracts"):
            return None

        exch = str(exchange or "").upper()
        prod = str(product_type or "").strip().lower()
        raw_code = str(code or "").strip().upper()

        if prod in {"index", "idx"} or exch in {"IDX", "INDEX"}:
            idx_exch = exch if exch in {"TSE", "OTC"} else self._client.index_exchange
            idx_group = getattr(self._client.api.Contracts.Indexs, idx_exch, None)
            return self._lookup_contract(
                idx_group, code, allow_symbol_fallback=self._client.allow_symbol_fallback, label="index"
            )

        if prod in {"stock", "stk"} or exch in {"TSE", "OTC", "OES"}:
            stocks = getattr(self._client.api.Contracts, "Stocks", None)
            tse_group = getattr(stocks, "TSE", None) if stocks is not None else None
            otc_group = getattr(stocks, "OTC", None) if stocks is not None else None
            oes_group = getattr(stocks, "OES", None) if stocks is not None else None
            if isinstance(stocks, dict):
                tse_group = stocks.get("TSE", tse_group)
                otc_group = stocks.get("OTC", otc_group)
                oes_group = stocks.get("OES", oes_group)

            if exch == "TSE" and tse_group is not None:
                return self._lookup_contract(
                    tse_group,
                    code,
                    allow_symbol_fallback=self._client.allow_symbol_fallback,
                    label="stock",
                )
            if exch == "OTC" and otc_group is not None:
                return self._lookup_contract(
                    otc_group,
                    code,
                    allow_symbol_fallback=self._client.allow_symbol_fallback,
                    label="stock",
                )
            if exch == "OES" and oes_group is not None:
                return self._lookup_contract(
                    oes_group,
                    code,
                    allow_symbol_fallback=self._client.allow_symbol_fallback,
                    label="stock",
                )

            for group in (tse_group, otc_group, oes_group):
                if group is None:
                    continue
                contract = self._lookup_contract(
                    group,
                    code,
                    allow_symbol_fallback=self._client.allow_symbol_fallback,
                    label="stock",
                )
                if contract:
                    return contract

            if stocks is not None:
                return self._lookup_contract(
                    stocks,
                    code,
                    allow_symbol_fallback=self._client.allow_symbol_fallback,
                    label="stock",
                )

        if prod in {"future", "futures"} or exch in {"FUT", "FUTURES", "TAIFEX"}:
            # R1/R2 continuous contract alias (e.g. TXFR1 → Contracts.Futures.TXF.TXFR1)
            if len(raw_code) >= 4 and raw_code[-2:] in ("R1", "R2"):
                root = raw_code[:-2]
                root_group = getattr(self._client.api.Contracts.Futures, root, None)
                if root_group is not None:
                    r_contract = getattr(root_group, raw_code, None)
                    if r_contract is not None:
                        return r_contract

            # Direct product-group lookup for month codes (e.g. TMFE6 → Futures.TMF.TMFE6)
            # Shioaji organises contracts under product groups; top-level iteration
            # may miss them if the container isn't dict-like at the root.
            if len(raw_code) >= 5 and raw_code[-1].isdigit() and raw_code[-2].isalpha():
                root = raw_code[:-2]
                root_group = getattr(self._client.api.Contracts.Futures, root, None)
                if root_group is not None:
                    direct = getattr(root_group, raw_code, None)
                    if direct is not None:
                        return direct

            for candidate in self._expand_future_codes(raw_code):
                contract = self._lookup_contract(
                    self._client.api.Contracts.Futures,
                    candidate,
                    allow_symbol_fallback=self._client.allow_symbol_fallback,
                    label="future",
                )
                if contract:
                    return contract

        if prod in {"option", "options"} or exch in {"OPT", "OPTIONS"}:
            contract = self._lookup_contract(
                self._client.api.Contracts.Options,
                raw_code,
                allow_symbol_fallback=self._client.allow_symbol_fallback,
                label="option",
            )
            if contract:
                return contract

        if allow_synthetic and sj:
            return self._build_synthetic_contract(exch, raw_code)

        return None

    def _expand_future_codes(self, code: str) -> list[str]:
        """Expand legacy futures month codes (e.g., TXFD6) to YYYYMM form (TXF202604)."""
        code = str(code or "").strip().upper()
        if not code:
            return []
        candidates = [code]
        if len(code) >= 5:
            month_code = code[-2]
            year_digit = code[-1]
            month_map = {
                "A": "01",
                "B": "02",
                "C": "03",
                "D": "04",
                "E": "05",
                "F": "06",
                "G": "07",
                "H": "08",
                "I": "09",
                "J": "10",
                "K": "11",
                "L": "12",
            }
            if year_digit.isdigit() and month_code in month_map:
                root = code[:-2]
                year = self._resolve_year_from_digit(int(year_digit))
                alt = f"{root}{year}{month_map[month_code]}"
                if alt not in candidates:
                    candidates.append(alt)
        return candidates

    def _resolve_year_from_digit(self, digit: int) -> int:
        now_year = dt.datetime.fromtimestamp(timebase.now_s(), tz=timebase.TZINFO).year
        base = (now_year // 10) * 10 + digit
        if base < now_year - 1:
            base += 10
        return base

    def _lookup_contract(self, container: Any, code: str, allow_symbol_fallback: bool, label: str) -> Any | None:
        if not container:
            return None

        try:
            return container[code]
        except Exception as exc:
            logger.debug("Direct contract lookup failed", code=code, label=label, error=str(exc))

        def iter_contracts(value: Any):
            iterable = value.values() if isinstance(value, dict) else value
            for item in iterable:
                yield item
                try:
                    if hasattr(item, "__iter__") and not hasattr(item, "code"):
                        for sub in item:
                            yield sub
                except Exception as exc:
                    logger.debug("Error iterating contract sub-items", error=str(exc))
                    continue

        try:
            for contract in iter_contracts(container):
                if getattr(contract, "code", None) == code:
                    return contract
        except Exception as exc:
            logger.warning("Error searching contracts by code", code=code, label=label, error=str(exc))
            return None

        if not allow_symbol_fallback:
            return None

        try:
            for contract in iter_contracts(container):
                if getattr(contract, "symbol", None) == code:
                    logger.warning("Symbol fallback used for contract", code=code, type=label)
                    return contract
        except Exception as exc:
            logger.warning("Error searching contracts by symbol fallback", code=code, label=label, error=str(exc))
            return None
        return None

    def _build_synthetic_contract(self, exchange: str, code: str) -> Any | None:
        try:
            exch_obj = (
                sj.constant.Exchange.TAIFEX if exchange in {"FUT", "FUTURES", "TAIFEX"} else sj.constant.Exchange.TSE
            )
            sec_type = (
                sj.constant.SecurityType.Future
                if exchange in {"FUT", "FUTURES", "TAIFEX"}
                else sj.constant.SecurityType.Stock
            )
            cat = code[:3] if len(code) >= 3 else code

            contract = sj.contracts.Contract(
                code=code,
                symbol=code,
                name=code,
                category=cat,
                exchange=exch_obj,
                security_type=sec_type,
            )
            logger.info("Constructed synthetic contract", code=code, exchange=exchange)
            return contract
        except Exception as exc:
            logger.error("Failed to construct synthetic contract", error=str(exc))
            return None

    def get_exchange(self, code: str) -> str | None:
        if code in self._client.code_exchange_map:
            return self._client.code_exchange_map[code]
        return None

    def validate_symbols(self) -> list[str]:
        if not self._client.api or not self._client.logged_in:
            return []
        invalid: list[str] = []
        for sym in self._client.symbols:
            code = sym.get("code")
            exchange = sym.get("exchange")
            product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
            if not code or not exchange:
                continue
            if not self._client._get_contract(exchange, code, product_type=product_type, allow_synthetic=False):
                invalid.append(code)
        if invalid:
            logger.warning("Unsubscribable symbols detected", count=len(invalid), symbols=invalid[:10])
        return invalid

    def reload_symbols(self) -> None:
        old_map: dict[str, dict[str, Any]] = {}
        for sym in self._client.symbols:
            code = sym.get("code")
            if code:
                old_map[str(code)] = sym
        self._client._load_config()
        self._client.code_exchange_map = {
            s["code"]: s["exchange"] for s in self._client.symbols if s.get("code") and s.get("exchange")
        }

        new_map: dict[str, dict[str, Any]] = {}
        for sym in self._client.symbols:
            code = sym.get("code")
            if code:
                new_map[str(code)] = sym
        removed = set(old_map) - set(new_map)
        added = set(new_map) - set(old_map)

        if not self._client.api or not self._client.logged_in or not self._client.tick_callback:
            # D2: in-place reset preserves object identity for peer readers.
            self._client.subscribed_codes.clear()
            self._client.subscribed_codes.update(new_map.keys())
            self._client.subscribed_count = len(self._client.subscribed_codes)
            self._client._refresh_quote_routes()
            return

        for code in removed:
            self._client._unsubscribe_symbol(old_map[code])
            self._client.subscribed_codes.discard(code)
        for code in added:
            if self._client.subscribed_count >= self._client.MAX_SUBSCRIPTIONS:
                raise ValueError("Subscription limit reached during reload")
            sym = new_map[code]
            if self._client._subscribe_symbol(sym, self._client.tick_callback):
                self._client.subscribed_codes.add(code)
        self._client.subscribed_count = len(self._client.subscribed_codes)
        self._client._refresh_quote_routes()
        # Rebuild alias map after symbol reload
        self._client.alias_to_actual.clear()
        self.resolve_symbol_aliases()

    def is_contract_cache_stale(self) -> bool:
        import datetime

        path = Path(self._client._contract_cache_path)
        if not path.exists():
            return True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            updated_at = data.get("updated_at")
            if not updated_at:
                return True
            dt = datetime.datetime.fromisoformat(updated_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            age_s = (datetime.datetime.fromtimestamp(timebase.now_s(), tz=datetime.timezone.utc) - dt).total_seconds()
            return age_s > self._client._contract_refresh_s
        except Exception as exc:
            logger.warning("Cannot parse contract cache for staleness check", error=str(exc))
            return True

    def write_refresh_status(self, *, result: str, error: str | None = None) -> None:
        refresh_thread = getattr(self._client, "_contract_refresh_thread", None)
        refresh_lock = getattr(self._client, "_contract_refresh_lock", None)
        payload = {
            "updated_at_ns": time.time_ns(),
            "result": str(result),
            "error": (str(error) if error else None),
            "version": int(getattr(self._client, "_contract_refresh_version", 0) or 0),
            "policy": str(getattr(self._client, "_contract_refresh_resubscribe_policy", "none") or "none"),
            "thread_running": bool(getattr(self._client, "_contract_refresh_running", False)),
            "thread_alive": bool(refresh_thread.is_alive()) if refresh_thread is not None else False,
            "lock_busy": bool(refresh_lock.locked()) if refresh_lock is not None else False,
            "cache_path": str(getattr(self._client, "_contract_cache_path", "")),
            "refresh_interval_s": float(getattr(self._client, "_contract_refresh_s", 0.0) or 0.0),
            "last_diff": dict(getattr(self._client, "_contract_refresh_last_diff", {}) or {}),
        }
        self._client._contract_refresh_last_status = payload
        path = str(getattr(self._client, "_contract_refresh_status_path", "") or "").strip()
        if not path:
            return
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            return

    def refresh_status(self) -> dict[str, object]:
        return {
            "status": dict(self._client._contract_refresh_last_status or {}),
            "version": int(self._client._contract_refresh_version),
            "last_diff": dict(self._client._contract_refresh_last_diff or {}),
            "policy": str(self._client._contract_refresh_resubscribe_policy or "none"),
            "cache_path": str(self._client._contract_cache_path),
            "status_path": str(self._client._contract_refresh_status_path or ""),
            "thread_running": bool(self._client._contract_refresh_running),
            "thread_alive": bool(self._client._contract_refresh_thread.is_alive())
            if self._client._contract_refresh_thread
            else False,
            "lock_busy": bool(self._client._contract_refresh_lock.locked()),
        }

    def refresh_contracts_and_symbols(self) -> None:
        if not self._client.api:
            return
        if not self._client._contract_refresh_lock.acquire(blocking=False):
            logger.info("contract_refresh_skipped_locked")
            self.write_refresh_status(result="skipped_locked")
            try:
                if self._client.metrics and hasattr(self._client.metrics, "contract_refresh_total"):
                    self._client.metrics.contract_refresh_total.labels(result="skipped_locked").inc()
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                pass
            return

        # Pool mode: ``config_path`` points at a QuoteConnectionPool shard, which
        # is also the only mode where the rebuild is safely shareable — see
        # ``_reuse_shared_rebuild``. Computed up front because the reuse decision
        # happens before any of the expensive work.
        is_pool_shard = "/hft_quote_pool_" in str(self._client.config_path)
        if is_pool_shard:
            shared = _take_shared_rebuild(
                max_age_s=client_float(self._client, "_contract_refresh_share_window_s", 300.0),
            )
            if shared is not None:
                self._reuse_shared_rebuild(shared)
                return

        codes_before: set[str] = set()
        contracts_before: list[dict[str, Any]] = []
        _t_phase = time.monotonic()
        try:
            cache_path = Path(self._client._contract_cache_path)
            if cache_path.exists():
                old_cache = json.loads(cache_path.read_text(encoding="utf-8"))
                contracts_before = [c for c in old_cache.get("contracts", []) if isinstance(c, dict)]
                codes_before = {str(c.get("code", "")) for c in contracts_before if c.get("code")}
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            pass
        _ms_cache_read = (time.monotonic() - _t_phase) * 1000.0

        # There is deliberately no broker fetch here any more.
        #
        # ``fetch_contracts`` needs sole ownership of shioaji 1.5.6's Rust
        # ``_core`` client, and this facade's ~74 live subscriptions hold
        # references for the entire session, so the call cannot succeed while we
        # are subscribed. Measured on THESHOW over 24 h: 96 attempts, 96
        # ``fetch_failed``, zero ``ok`` — the poll was pure noise, and it emitted
        # an ERROR line per facade per hour for a call that is unimplementable by
        # construction. Contracts are loaded by *login*, which on this platform
        # happens roughly every 1.6 h per facade, i.e. more often than this hourly
        # poll could have refreshed them even if it worked. Broker-announced
        # changes now arrive as ``SYS/CONTRACT`` pushes via
        # ``register_contract_event_callback`` instead of being polled for.
        #
        # What remains is still worth doing: re-read what the SDK currently
        # holds, rebuild the on-disk cache from it, and diff so a rollover that
        # login picked up still drives resubscription.
        logger.info(
            "contract_refresh_reading_sdk_contracts",
            status=_read_contract_status(self._client.api),
        )

        try:
            from hft_platform.config.symbols import (
                DEFAULT_LIST_PATH,
                ContractIndex,
                build_symbols,
                write_contract_cache,
                write_symbols_yaml,
            )

            raw_contracts: list[dict] = []

            def _normalize(c: Any, exchange: str, kind: str) -> dict:
                right = getattr(c, "option_right", None) or getattr(c, "right", None)
                if right is not None:
                    right = getattr(right, "value", right)
                payload = {
                    "code": getattr(c, "code", None),
                    "symbol": getattr(c, "symbol", None),
                    "name": getattr(c, "name", None),
                    "exchange": exchange,
                    "type": kind,
                    "root": getattr(c, "category", None) or getattr(c, "symbol", None),
                    "tick_size": getattr(c, "tick_size", None),
                    "price_scale": getattr(c, "price_scale", None),
                    "delivery_date": getattr(c, "delivery_date", None),
                    "strike": getattr(c, "strike_price", None) or getattr(c, "strike", None),
                    "right": right,
                }
                return {k: v for k, v in payload.items() if v is not None}

            # Serialise the pooled facades. Walking the SDK's contract store is a
            # read, not a fetch, but four facades entering it on the same hourly
            # timer is the same process-wide contention that made concurrent
            # fetches abort. Reuse the login slot rather than add a second
            # primitive. ``False`` means the slot timed out: proceed
            # unserialised (a never-rebuilt cache is worse) and do NOT release a
            # slot we do not hold.
            slot_held = acquire_login_slot(
                min_gap_s=client_float(self._client, "_contract_fetch_stagger_gap_s", 2.0),
                timeout_s=client_float(self._client, "_contract_fetch_stagger_timeout_s", 300.0),
                metrics=getattr(self._client, "metrics", None),
            )
            try:
                try:
                    for c in self._client.api.Contracts.Stocks.TSE:
                        raw_contracts.append(_normalize(c, "TSE", "stock"))
                    for c in self._client.api.Contracts.Stocks.OTC:
                        raw_contracts.append(_normalize(c, "OTC", "stock"))
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass
                try:
                    for c in iter_contract_category(self._client.api.Contracts.Futures):
                        raw_contracts.append(_normalize(c, "TAIFEX", "future"))
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass
                try:
                    for c in iter_contract_category(self._client.api.Contracts.Options):
                        raw_contracts.append(_normalize(c, "TAIFEX", "option"))
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass
            finally:
                if slot_held:
                    release_login_slot()
            _ms_sdk_walk = (time.monotonic() - _t_phase) * 1000.0 - _ms_cache_read

            counts_before = _contract_type_counts(contracts_before)
            counts_after = _contract_type_counts(raw_contracts)
            derivatives_before = counts_before["future"] + counts_before["option"]
            derivatives_after = counts_after["future"] + counts_after["option"]
            configured_derivatives = _symbols_require_derivative_contracts(
                [sym for sym in getattr(self._client, "symbols", []) if isinstance(sym, dict)]
            )
            logger.info(
                "contract_refresh_contract_counts",
                before=counts_before,
                after=counts_after,
                configured_derivatives=configured_derivatives,
            )
            if derivatives_after == 0 and (derivatives_before > 0 or configured_derivatives):
                error = (
                    "broker returned no derivative contracts while previous cache or configured symbols "
                    "require derivatives"
                )
                try:
                    logger.error(
                        "contract_refresh_integrity_failed",
                        error=error,
                        before=counts_before,
                        after=counts_after,
                        configured_derivatives=configured_derivatives,
                    )
                    self.write_refresh_status(result="error", error=error)
                    if self._client.metrics and hasattr(self._client.metrics, "contract_refresh_total"):
                        self._client.metrics.contract_refresh_total.labels(result="error").inc()
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                finally:
                    self._client._contract_refresh_lock.release()
                return

            _t_write = time.monotonic()
            write_contract_cache(raw_contracts, self._client._contract_cache_path)
            _ms_cache_write = (time.monotonic() - _t_write) * 1000.0
            # ``contract_cache_last_success_ts`` is deliberately NOT advanced
            # here. It means "when did we last load contracts *from the broker*",
            # and this routine no longer talks to the broker — it re-serialises
            # what login already loaded. Stamping it here would make
            # ``ContractsStaleVsBrokerAnnouncement`` clear itself within an hour
            # of every announcement without anything having been re-read, which
            # is exactly the kind of self-satisfying metric this alert exists to
            # avoid. Login stamps it (``session_runtime``); nothing else should.

            codes_after = {str(c.get("code", "")) for c in raw_contracts if c.get("code")}
            full_added = sorted(codes_after - codes_before)
            full_removed = sorted(codes_before - codes_after)
            self._client._contract_refresh_version += 1
            self._client._contract_refresh_last_diff = _diff_payload_from_delta(
                version=self._client._contract_refresh_version,
                contracts_before=len(codes_before),
                contracts_after=len(codes_after),
                added=full_added,
                removed=full_removed,
                subscribed=set(getattr(self._client, "subscribed_codes", None) or ()),
            )
            # Publish before the symbol rebuild below, not after: the sibling
            # facades are already waiting on the login slot, and everything after
            # this point is per-facade work they must not inherit.
            if is_pool_shard:
                _publish_shared_rebuild(
                    _SharedRebuild(
                        mono=time.monotonic(),
                        contracts_before=len(codes_before),
                        contracts_after=len(codes_after),
                        added=tuple(full_added),
                        removed=tuple(full_removed),
                    )
                )
            added = self._client._contract_refresh_last_diff["added_codes"]
            removed = self._client._contract_refresh_last_diff["removed_codes"]
            logger.info(
                "contract_refresh_diff",
                version=self._client._contract_refresh_last_diff["version"],
                contracts_before=self._client._contract_refresh_last_diff["contracts_before"],
                contracts_after=self._client._contract_refresh_last_diff["contracts_after"],
                added_count=self._client._contract_refresh_last_diff["added_count"],
                removed_count=self._client._contract_refresh_last_diff["removed_count"],
                relevant_count=self._client._contract_refresh_last_diff["relevant_count"],
                shared="owner",
            )
            # Phase timings decide whether narrowing the walk is worth a second
            # change: if ``sdk_walk_ms`` dominates, the cost is crossing into the
            # SDK's Rust contract store at all and fewer ``getattr``s per contract
            # will not help; if ``cache_read_ms``/``cache_write_ms`` dominate, the
            # 13 MB JSON round-trip is the target instead.
            logger.info(
                "contract_refresh_phase_timings",
                cache_read_ms=round(_ms_cache_read, 1),
                sdk_walk_ms=round(_ms_sdk_walk, 1),
                cache_write_ms=round(_ms_cache_write, 1),
                contracts=len(raw_contracts),
            )
            try:
                if self._client.metrics and hasattr(self._client.metrics, "contract_refresh_symbols_changed_total"):
                    if not added and not removed:
                        self._client.metrics.contract_refresh_symbols_changed_total.labels(change="same").inc()
                    if added:
                        self._client.metrics.contract_refresh_symbols_changed_total.labels(change="added").inc()
                    if removed:
                        self._client.metrics.contract_refresh_symbols_changed_total.labels(change="removed").inc()
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                pass

            _t_build = time.monotonic()
            contract_index = ContractIndex(contracts=raw_contracts)
            list_path = Path(Path(self._client.config_path).parent / "symbols.list")
            if not list_path.exists():
                list_path = Path(DEFAULT_LIST_PATH)
            build_result = build_symbols(str(list_path), contract_index)
            logger.info(
                "contract_refresh_symbol_build_ms",
                symbol_build_ms=round((time.monotonic() - _t_build) * 1000.0, 1),
                rebuilt_count=len(build_result.symbols),
            )
            # Pool mode: ``config_path`` points at a QuoteConnectionPool shard
            # (``/tmp/hft_quote_pool_*/symbols_group_<id>.yaml``) which holds
            # only this facade's partition (~universe/num_conns). Writing the
            # full rebuilt universe back here corrupts the partition — the
            # 2026-05-23 root cause where ``symbols_group_0.yaml`` was being
            # promoted to 478 symbols every hour. The pool owns shard
            # composition; refresh only updates the broker contract index here.
            if build_result.symbols and not is_pool_shard:
                write_symbols_yaml(build_result.symbols, self._client.config_path)
                logger.info(
                    "Symbols rebuilt from fresh contracts",
                    count=len(build_result.symbols),
                    errors=len(build_result.errors),
                )
            elif build_result.symbols and is_pool_shard:
                logger.debug(
                    "skip_symbol_yaml_write_in_pool_mode",
                    config_path=str(self._client.config_path),
                    rebuilt_count=len(build_result.symbols),
                )
            if build_result.errors:
                logger.warning("Symbol rebuild had errors", errors=build_result.errors[:5])
        except Exception as exc:
            logger.warning("Symbol rebuild failed, keeping existing symbols", error=str(exc))

        try:
            self._client._load_config()
            logger.info("Symbol config reloaded after contract refresh", symbol_count=len(self._client.symbols))
        except Exception as exc:
            # Q3-fix (2026-04-27): contract-refresh reload-failure used to be
            # invisible. ``_load_config`` itself now bumps the metric Counter;
            # we additionally raise this from WARNING to ERROR with a
            # ``severity="critical"`` tag and best-effort fan out to the
            # optional dispatcher attached to the client.
            logger.error(
                "Symbol config reload failed after contract refresh",
                error=str(exc),
                severity="critical",
            )
            dispatcher = getattr(self._client, "_notification_dispatcher", None)
            if dispatcher is not None and hasattr(dispatcher, "notify_symbol_reload_failed"):
                try:
                    import asyncio as _asyncio  # local import — non hot-path

                    symbols = getattr(self._client, "symbols", None) or []
                    limit = int(
                        getattr(self._client, "MAX_SUBSCRIPTIONS_PER_CLIENT", 0)
                        or getattr(self._client, "MAX_SUBSCRIPTIONS", 0)
                    )
                    reason = "exceeds_limit" if "exceeds limit" in str(exc).lower() else "other"
                    coro = dispatcher.notify_symbol_reload_failed(
                        reason=reason,
                        count=len(symbols),
                        limit=limit,
                    )
                    # contracts_runtime can be called from a worker thread
                    # (contract_refresh_worker); schedule onto the running
                    # loop if available, otherwise drop after logging.
                    try:
                        loop = _asyncio.get_running_loop()
                        loop.create_task(coro)
                    except RuntimeError:
                        try:
                            _asyncio.run(coro)
                        except Exception as run_exc:  # noqa: BLE001
                            logger.warning(
                                "symbol_reload_alert_run_failed",
                                error=str(run_exc),
                            )
                except Exception as notify_exc:  # noqa: BLE001
                    logger.warning(
                        "symbol_reload_alert_dispatch_failed",
                        error=str(notify_exc),
                    )
        finally:
            try:
                if self._client.metrics and hasattr(self._client.metrics, "contract_refresh_total"):
                    self._client.metrics.contract_refresh_total.labels(result="ok").inc()
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                pass
            policy = self._client._contract_refresh_resubscribe_policy
            should_resub = policy == "all"
            if policy == "diff":
                diff = self._client._contract_refresh_last_diff or {}
                should_resub = _diff_should_resubscribe(diff)
            if should_resub and self._client.logged_in:
                try:
                    logger.info(
                        "contract_refresh_resubscribe",
                        policy=policy,
                        relevant_count=(self._client._contract_refresh_last_diff or {}).get("relevant_count"),
                    )
                    self._client._resubscribe_all()
                except Exception as exc:
                    logger.warning("contract_refresh_resubscribe_failed", error=str(exc))
            self._client._contract_refresh_lock.release()
            self.write_refresh_status(result="ok")

    def _reuse_shared_rebuild(self, shared: _SharedRebuild) -> None:
        """Complete this facade's refresh cycle from a sibling facade's rebuild.

        Skips the three expensive steps — reading the 13 MB contract cache,
        walking the SDK's ~54 700 contracts, and writing the cache back — because
        a sibling facade has just produced exactly that result from the same file
        and the same broker account. Everything that is genuinely per-facade
        still runs: the relevance half of the diff (this facade's own ~74-symbol
        subscription shard), the resubscribe decision, the config reload, and the
        status file.

        Only reached in pool-shard mode. In single-facade or non-pool
        deployments each client owns its ``config_path`` and must run its own
        ``write_symbols_yaml``, which needs the full contract list — so those
        keep the original path untouched.

        Caller holds ``_contract_refresh_lock``; this method releases it.
        """
        try:
            self._client._contract_refresh_version += 1
            self._client._contract_refresh_last_diff = _diff_payload_from_delta(
                version=self._client._contract_refresh_version,
                contracts_before=shared.contracts_before,
                contracts_after=shared.contracts_after,
                added=shared.added,
                removed=shared.removed,
                subscribed=set(getattr(self._client, "subscribed_codes", None) or ()),
            )
            diff = self._client._contract_refresh_last_diff
            logger.info(
                "contract_refresh_diff",
                version=diff["version"],
                contracts_before=diff["contracts_before"],
                contracts_after=diff["contracts_after"],
                added_count=diff["added_count"],
                removed_count=diff["removed_count"],
                relevant_count=diff["relevant_count"],
                shared="reused",
                shared_age_s=round(time.monotonic() - shared.mono, 3),
            )
            try:
                if self._client.metrics and hasattr(self._client.metrics, "contract_refresh_symbols_changed_total"):
                    if not shared.added and not shared.removed:
                        self._client.metrics.contract_refresh_symbols_changed_total.labels(change="same").inc()
                    if shared.added:
                        self._client.metrics.contract_refresh_symbols_changed_total.labels(change="added").inc()
                    if shared.removed:
                        self._client.metrics.contract_refresh_symbols_changed_total.labels(change="removed").inc()
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))

            try:
                self._client._load_config()
            except Exception as exc:
                logger.error(
                    "Symbol config reload failed after contract refresh",
                    error=str(exc),
                    severity="critical",
                )
        finally:
            try:
                if self._client.metrics and hasattr(self._client.metrics, "contract_refresh_total"):
                    self._client.metrics.contract_refresh_total.labels(result="ok_shared").inc()
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
            policy = self._client._contract_refresh_resubscribe_policy
            should_resub = policy == "all"
            if policy == "diff":
                should_resub = _diff_should_resubscribe(self._client._contract_refresh_last_diff or {})
            if should_resub and self._client.logged_in:
                try:
                    logger.info(
                        "contract_refresh_resubscribe",
                        policy=policy,
                        relevant_count=(self._client._contract_refresh_last_diff or {}).get("relevant_count"),
                        shared="reused",
                    )
                    self._client._resubscribe_all()
                except Exception as exc:
                    logger.warning("contract_refresh_resubscribe_failed", error=str(exc))
            self._client._contract_refresh_lock.release()
            self.write_refresh_status(result="ok_shared")

    def preflight_contracts(self) -> None:
        errors: list[str] = []
        if self.is_contract_cache_stale():
            logger.warning("preflight_contract_cache_stale", path=self._client._contract_cache_path)
            errors.append("contract_cache_stale")
        missing_codes: list[str] = []
        for sym in self._client.symbols:
            code = sym.get("code")
            exchange = sym.get("exchange")
            product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
            if not code or not exchange:
                continue
            if not self._client._get_contract(exchange, code, product_type=product_type, allow_synthetic=False):
                missing_codes.append(str(code))
        if missing_codes:
            logger.warning(
                "preflight_missing_contracts",
                missing_count=len(missing_codes),
                missing_sample=missing_codes[:10],
            )
            errors.append(f"missing_contracts:{len(missing_codes)}")
        # RC-1 (2026-04-27): preflight bounds the universe against the
        # per-client ceiling (default 600). The per-conn cap (120) is enforced
        # by QuoteConnectionPool sharding, not at preflight.
        preflight_ceiling = int(
            getattr(self._client, "MAX_SUBSCRIPTIONS_PER_CLIENT", 0) or self._client.MAX_SUBSCRIPTIONS
        )
        if len(self._client.symbols) > preflight_ceiling:
            logger.warning(
                "preflight_subscription_count_exceeded",
                symbol_count=len(self._client.symbols),
                limit=preflight_ceiling,
            )
            errors.append("subscription_count_exceeded")
        logger.info("preflight_complete", passed_all=(len(errors) == 0), errors=errors)

    def resolve_symbol_aliases(self, codes: list[str] | None = None) -> dict[str, str]:
        """Resolve C0/C1/R1/R2 aliases to actual month codes via broker contracts.

        Args:
            codes: List of symbol codes to resolve. If None, resolves all
                   symbols from client.symbols config.

        Returns:
            Mapping of config_code → actual_code for aliases that differ.
            Identity mappings (code == actual) are omitted.
        """
        if codes is None:
            codes = [str(sym.get("code", "")) for sym in self._client.symbols if sym.get("code")]

        alias_map: dict[str, str] = {}
        for code in codes:
            code = str(code).strip()
            if not code:
                continue
            # Find matching symbol config for exchange/product_type
            sym_cfg = next(
                (s for s in self._client.symbols if s.get("code") == code),
                None,
            )
            exchange = (sym_cfg or {}).get("exchange", "FUT")
            product_type = (sym_cfg or {}).get("product_type") or (sym_cfg or {}).get("security_type")
            contract = self._client._get_contract(
                exchange,
                code,
                product_type=product_type,
                allow_synthetic=False,
            )
            if contract:
                actual = derive_callback_code(contract, code)
                if actual != code:
                    alias_map[code] = actual
                    logger.info(
                        "alias_resolved",
                        config_code=code,
                        actual_code=actual,
                    )
        # Merge into client-level map
        self._client.alias_to_actual.update(alias_map)
        return alias_map

    def _on_contract_update_event(self, event: Any) -> None:
        """Broker-thread handler for ``SYS/CONTRACT`` announcements.

        Runs on a shioaji callback thread, so it must stay cheap and must never
        raise back into the SDK — an exception crossing the PyO3 boundary is how
        callback threads die silently. It deliberately does **not** trigger a
        fetch: ``fetch_contracts`` cannot succeed on a subscribed facade, and
        re-entering the SDK from its own callback thread is precisely the
        ``Already borrowed`` re-entry that caused the 04:12 CST cascade. Record
        only; acting on the event is a separate decision.
        """
        try:
            action = _classify_contract_event_field(getattr(event, "action", None), _CONTRACT_EVENT_ACTIONS)
            security_type = _classify_contract_event_field(
                getattr(event, "security_type", None), _CONTRACT_EVENT_SECURITY_TYPES
            )
            check_file_ts = getattr(event, "check_file_ts", None)
            now_s = timebase.now_s()
            self._client._contract_update_last_event_s = now_s
            logger.info(
                "contract_update_event",
                action=action,
                security_type=security_type,
                check_file_ts=(float(check_file_ts) if isinstance(check_file_ts, (int, float)) else None),
            )
            metrics = getattr(self._client, "metrics", None)
            if metrics is not None:
                counter = getattr(metrics, "contract_update_events_total", None)
                if counter is not None:
                    counter.labels(action=action, security_type=security_type).inc()
                gauge = getattr(metrics, "contract_update_last_event_ts", None)
                if gauge is not None:
                    gauge.set(now_s)
        except Exception as exc:  # never let a callback kill the SDK's thread
            logger.warning("contract_update_event_handler_failed", error=str(exc))

    def register_contract_event_callback(self) -> bool:
        """Subscribe to the broker's contract-change announcements.

        shioaji 1.5.x pushes ``SYS/CONTRACT`` events (solace topic
        ``APISUB/V1/SYS/CONTRACT``) carrying ``action`` (FORCE|CHECK),
        ``security_type`` and ``check_file_ts``. This platform had never used the
        API, and instead polled ``fetch_contracts`` hourly — a call that needs
        sole ownership of the SDK's inner client and therefore fails 100% of the
        time on a facade holding 74 subscriptions. The push signal is the one
        that actually works, so register for it even while the reaction is still
        being decided: without it there is no evidence of *when* contracts change.

        Registration failure is logged and reported, never fatal — contracts
        still load at login.
        """
        api = self._client.api
        if api is None:
            logger.warning("Contract event callback deferred; api unavailable")
            self._set_contract_callback_registered(False)
            return False
        setter = getattr(api, "set_contract_event_callback", None)
        if setter is None:
            # Older/other SDK builds simply do not expose it.
            logger.info("contract_event_callback_unsupported")
            self._set_contract_callback_registered(False)
            return False
        try:
            setter(self._on_contract_update_event)
        except Exception as exc:
            logger.warning("Failed contract event callback registration", error=str(exc))
            self._set_contract_callback_registered(False)
            return False
        logger.info("Contract event callback registered")
        self._set_contract_callback_registered(True)
        return True

    def _set_contract_callback_registered(self, registered: bool) -> None:
        """Publish whether the push callback is live.

        Registration success was log-only, which means "registered but the
        broker has been silent" and "never registered at all" looked identical
        on the metrics plane — both are ``contract_update_last_event_ts == 0``.
        An alert cannot be written against a distinction that only exists in the
        logs, so publish it.
        """
        try:
            metrics = getattr(self._client, "metrics", None)
            gauge = getattr(metrics, "contract_event_callback_registered", None) if metrics else None
            if gauge is not None:
                conn_id = str(getattr(self._client, "conn_id", "-"))
                gauge.labels(conn_id=conn_id).set(1 if registered else 0)
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))

    def start_contract_refresh_thread(self) -> None:
        if self._client._contract_refresh_running:
            return
        # Register before the poll loop starts. The poll is known-broken on a
        # subscribed facade; this callback is the only working freshness signal.
        self.register_contract_event_callback()
        self._client._contract_refresh_running = True
        self._client._set_thread_alive_metric("contract_refresh", True)
        self.write_refresh_status(result="thread_started")

        def _refresh_loop() -> None:
            if self.is_contract_cache_stale():
                logger.info("Contract cache stale at startup; triggering immediate refresh")
                self.refresh_contracts_and_symbols()
            next_refresh = time.monotonic() + self._client._contract_refresh_s
            while self._client._contract_refresh_running:
                time.sleep(60.0)
                if not self._client._contract_refresh_running:
                    break
                if time.monotonic() >= next_refresh:
                    logger.info("Scheduled contract refresh starting")
                    self.refresh_contracts_and_symbols()
                    next_refresh = time.monotonic() + self._client._contract_refresh_s
            self._client._contract_refresh_running = False
            self._client._set_thread_alive_metric("contract_refresh", False)

        self._client._contract_refresh_thread = threading.Thread(
            target=_refresh_loop,
            name="shioaji-contract-refresh",
            daemon=True,
        )
        self._client._contract_refresh_thread.start()
