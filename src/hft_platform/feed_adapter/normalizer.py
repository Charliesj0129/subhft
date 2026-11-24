import datetime as dt
import os
import re
import sys
import threading
from collections.abc import Iterable
from dataclasses import replace
from typing import Any, cast

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.events import BidAskEvent, MetaData, TickEvent
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.trade_classifier import TradeClassifier

# Validated Imports

# Use existing implementation of SymbolMetadata.

logger = get_logger("feed_adapter.normalizer")

_TICK_EVENT_SUPPORTS_CONTRACT = "contract" in getattr(TickEvent, "__dataclass_fields__", {})
_BIDASK_EVENT_SUPPORTS_CONTRACT = "contract" in getattr(BidAskEvent, "__dataclass_fields__", {})

_EVENT_MODE = os.getenv("HFT_EVENT_MODE", "event").lower()
if "pytest" in sys.modules:
    _EVENT_MODE = "event"
_RETURN_TUPLE = _EVENT_MODE in {"tuple", "raw"}
_SYNTHETIC_SIDE = os.getenv("HFT_MD_SYNTHETIC_SIDE", "0").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_SYNTHETIC_TICKS = max(1, int(os.getenv("HFT_MD_SYNTHETIC_TICKS", "1")))
try:
    _TS_MAX_LAG_NS = int(float(os.getenv("HFT_TS_MAX_LAG_S", "5")) * 1e9)
except Exception as exc:
    logger.warning("Failed to parse HFT_TS_MAX_LAG_S, using 0", error=str(exc))
    _TS_MAX_LAG_NS = 0
try:
    _TS_MAX_FUTURE_NS = int(float(os.getenv("HFT_TS_MAX_FUTURE_S", "5")) * 1e9)
except Exception as exc:
    logger.warning("Failed to parse HFT_TS_MAX_FUTURE_S, using 0", error=str(exc))
    _TS_MAX_FUTURE_NS = 0
try:
    _TS_SKEW_LOG_COOLDOWN_NS = int(float(os.getenv("HFT_TS_SKEW_LOG_COOLDOWN_S", "60")) * 1e9)
except Exception as exc:
    logger.warning("Failed to parse HFT_TS_SKEW_LOG_COOLDOWN_S, using 0", error=str(exc))
    _TS_SKEW_LOG_COOLDOWN_NS = 0

# TAIFEX monthly contract codes: ROOT + month letter (A=Jan … L=Dec) + year
# digit, e.g. TMFH6 = August 2026. Continuous-family codes (TMFR1) and weekly
# option codes do not match and are deliberately left without a derived date.
_MONTHLY_CONTRACT_RE = re.compile(r"^([A-Z]{2,4})([A-L])([0-9])$")


def _third_wednesday(year: int, month: int) -> dt.date:
    """Settlement date for a TAIFEX monthly futures/options contract.

    Cross-checked against the broker-supplied option expiries in
    ``config/symbols.yaml``: June 2026 ships as ``2026/06/17``, which is the
    third Wednesday of that month.
    """
    first = dt.date(year, month, 1)
    # weekday(): Monday=0 … Wednesday=2
    first_wednesday = 1 + (2 - first.weekday()) % 7
    return dt.date(year, month, first_wednesday + 14)


def _parse_instrument_expiry(raw: Any, code: str) -> dt.date | None:
    """Parse a configured expiry, accepting the formats the config actually uses.

    ``config/symbols.yaml`` writes ``"2026/06/17"``. ``date.fromisoformat``
    rejects slash separators, and the resulting ``ValueError`` used to be
    swallowed by a bare ``pass`` — the value was present, parsed, rejected and
    discarded without a trace, for every one of the 106 option entries.
    """
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    logger.warning("instrument_expiry_unparseable", symbol=code, value=text)
    return None


def _derive_monthly_expiry(code: str) -> dt.date | None:
    """Derive expiry from a TAIFEX monthly contract code.

    ``config/symbols.yaml`` carries no ``expiry`` on any futures entry, so
    without this every futures row records the schema default. The single-digit
    year is anchored to the current decade and rolled forward when that would
    place the contract more than five years in the past — the same rule
    ``contracts/ref.py:237`` uses for parsing display strings.
    """
    match = _MONTHLY_CONTRACT_RE.match(code)
    if match is None:
        return None
    _root, letter, year_digit = match.groups()
    month = ord(letter) - ord("A") + 1
    base_year = dt.date.today().year
    year = (base_year // 10) * 10 + int(year_digit)
    if year < base_year - 5:
        year += 10
    return _third_wednesday(year, month)


def _normalize_tags(tags_raw: Any) -> set[str]:
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in re.split(r"[|,]", tags_raw) if t.strip()]
    elif isinstance(tags_raw, (list, tuple, set)):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    else:
        tags = []
    return {t.lower() for t in tags}


class SymbolMetadata:
    """
    Loads per-symbol configuration.
    """

    DEFAULT_SCALE = 10_000

    def __init__(self, config_path: str | None = None):
        # simplified for this context, assuming existing logic was ok, just need it here
        if config_path is None:
            from hft_platform.config.symbols_path import resolve_symbols_config_path

            config_path = resolve_symbols_config_path()

        self.config_path = config_path
        self.meta: dict[str, dict[str, Any]] = {}
        self.tags_by_symbol: dict[str, set[str]] = {}
        self.symbols_by_tag: dict[str, set[str]] = {}
        self._price_scale_cache: dict[str, int] = {}
        self._exchange_cache: dict[str, str] = {}
        self._product_type_cache: dict[str, str] = {}
        self._mtime: float | None = None
        # A universe roll replaces contracts while the engine runs (see
        # ``QuoteConnectionPool.roll_universe``). The canonical file on disk is
        # the operator's, so the rolled universe arrives here instead and is
        # re-applied after every load — otherwise the next ``reload_if_changed``
        # would price a freshly subscribed month off a file that predates it.
        self._runtime_overlay: list[dict[str, Any]] = []
        self.alias_to_actual: dict[str, str] = {}  # config alias → callback code (e.g. TXFR1 → TXFE6)
        # Serialises whole reloads so two loads (roll worker vs loop-thread
        # ``reload_if_changed``) cannot publish out of order. The event loop
        # never waits on it: ``reload_if_changed`` skips when it is held.
        self._reload_lock = threading.Lock()
        from hft_platform.core.instrument_registry import InstrumentRegistry

        self.registry = InstrumentRegistry()
        self._load()
        self._populate_registry()

    def _load(self) -> None:
        """Re-read the symbol file and fold the runtime overlay over it.

        ``apply_runtime_overlay`` runs this on the universe-roll worker thread
        while the event loop keeps reading ``exchange()``/``product_type()``.
        Everything is therefore built in locals and published at the end, the
        new ``meta`` before the emptied caches, so a reader never sees a
        half-filled table. Clearing ``meta`` first let a loop-thread lookup
        cache ``""`` for TMFK6, and every later order went out untyped
        (2026-11-09 incident).
        """
        import yaml

        meta: dict[str, dict[str, Any]] = {}
        tags_by_symbol: dict[str, set[str]] = {}
        symbols_by_tag: dict[str, set[str]] = {}
        try:
            mtime: float | None = os.path.getmtime(self.config_path)
        except OSError:
            mtime = None
        try:
            with open(self.config_path) as f:
                data = yaml.safe_load(f) or {}
                for item in data.get("symbols", []):
                    code = item.get("code")
                    if not code:
                        continue
                    meta[code] = item
                    normalized = _normalize_tags(item.get("tags", []))
                    if normalized:
                        tags_by_symbol[code] = normalized
                        for tag in normalized:
                            symbols_by_tag.setdefault(tag, set()).add(code)
        except Exception as exc:
            logger.warning("symbol_metadata_load_failed", error=str(exc), config_path=self.config_path)
        self._apply_overlay(meta, tags_by_symbol, symbols_by_tag)
        # An actual month code that exists only through ``set_alias_map`` is
        # not in the file, so a reload would otherwise drop it and its lookups
        # would resolve to "".
        self._copy_alias_entries(meta)

        # Publish: tables first, then fresh caches. A reader holding an old
        # cache may still fill it, but that dict is discarded; a reader that
        # sees a new cache reads it after ``meta`` was replaced.
        self.meta = meta
        self.tags_by_symbol = tags_by_symbol
        self.symbols_by_tag = symbols_by_tag
        self._mtime = mtime
        self._price_scale_cache = {}
        self._exchange_cache = {}
        self._product_type_cache = {}
        # ``set_alias_map`` on another thread may have recorded an alias after
        # the copy above and written its entry into the old table. It updates
        # ``alias_to_actual`` before touching ``meta``, so a second pass over
        # the published table catches it.
        self._copy_alias_entries(self.meta)

    def _copy_alias_entries(self, meta: dict[str, dict[str, Any]]) -> None:
        for config_code, actual_code in list(self.alias_to_actual.items()):
            if actual_code != config_code and actual_code not in meta:
                config_entry = meta.get(config_code)
                if config_entry is not None:
                    meta[actual_code] = config_entry

    def _apply_overlay(
        self,
        meta: dict[str, dict[str, Any]],
        tags_by_symbol: dict[str, set[str]],
        symbols_by_tag: dict[str, set[str]],
    ) -> None:
        """Fold the rolled universe (if any) over the tables being built."""
        for item in getattr(self, "_runtime_overlay", ()):
            code = item.get("code")
            if not code:
                continue
            meta[code] = item
            normalized = _normalize_tags(item.get("tags", []))
            previous = tags_by_symbol.pop(code, set())
            for tag in previous - normalized:
                holders = symbols_by_tag.get(tag)
                if holders:
                    holders.discard(code)
            if normalized:
                tags_by_symbol[code] = normalized
                for tag in normalized:
                    symbols_by_tag.setdefault(tag, set()).add(code)

    def apply_runtime_overlay(self, symbols: list[dict[str, Any]]) -> None:
        """Adopt a rolled universe as the metadata of record, over the file.

        Called by the pool's universe roll before the new codes are subscribed,
        so the first tick of a new contract is scaled by its own metadata rather
        than by ``DEFAULT_SCALE``.
        """
        self._runtime_overlay = [dict(sym) for sym in symbols if sym.get("code")]
        self.reload()

    def reload(self) -> None:
        with self._reload_lock:
            self._load()
            self._populate_registry()

    def reload_if_changed(self) -> bool:
        try:
            mtime = os.path.getmtime(self.config_path)
        except OSError:
            return False
        if self._mtime is None or mtime > self._mtime:
            # Runs on the event loop: never wait for a roll's reload. If one
            # is in flight, skip; ``_mtime`` is still the old one unless that
            # reload saw the new file, so the next call retries.
            if not self._reload_lock.acquire(blocking=False):
                return False
            try:
                self._load()
            finally:
                self._reload_lock.release()
            return True
        return False

    def symbols_for_tags(self, tags: Iterable[str]) -> set[str]:
        resolved = set()
        for tag in tags:
            key = str(tag).strip().lower()
            if not key:
                continue
            resolved.update(self.symbols_by_tag.get(key, set()))
        return resolved

    def contract_ref(self, symbol: str) -> Any:
        """Return a cached ``ContractRef`` for *symbol*, or ``None`` if the
        symbol cannot be parsed.

        Gate 2b of the Option-3 migration. Lazy-populated: events passing
        through the hot path incur a single dict lookup after the first parse
        per symbol. Failures are cached as ``None`` so we do not re-parse a
        malformed symbol on every tick.
        """
        cache = getattr(self, "_contract_ref_cache", None)
        if cache is None:
            cache = {}
            self._contract_ref_cache = cache
        if symbol in cache:
            return cache[symbol]

        import datetime as dt

        try:
            from hft_platform.contracts.ref import (
                ContractFamily,
                FutureRef,
                parse_display,
            )
        except ModuleNotFoundError:
            cache[symbol] = None
            return None

        base_year = dt.datetime.fromtimestamp(timebase.now_s(), dt.UTC).year
        parsed: Any
        try:
            parsed = parse_display(symbol, base_year=base_year)
        except ValueError:
            parsed = None
        # A family string (e.g. "TMFR1") is not a concrete contract; events
        # carry a concrete expiry. We keep the ``contract`` field as None for
        # family-form symbols and rely on the resolver to fill it at dispatch.
        if isinstance(parsed, ContractFamily):
            parsed = None
        # Defensive: future single-digit year parser may not uniquely
        # identify the exact expiry day. Downstream (ContractResolver) is the
        # authority for concrete expiry dates; parse_display is a best-effort
        # fallback for dual-write convenience.
        if isinstance(parsed, FutureRef):
            parsed = parsed  # type intentionally narrowed; keep as parsed.
        cache[symbol] = parsed
        return parsed

    def price_scale(self, symbol: str) -> int:
        # Bind the cache before reading ``meta``: a concurrent ``_load`` swaps
        # ``meta`` before the caches, so a value computed here lands either in
        # the dict it was read against or in one that is already discarded.
        # Only symbols with an entry are cached; a miss stays uncached so it
        # cannot outlive the reload that adds the symbol.
        cache = self._price_scale_cache
        cached = cache.get(symbol)
        if cached is not None:
            return cached
        entry = self.meta.get(symbol)
        if not entry:
            return self.DEFAULT_SCALE
        if "price_scale" in entry:
            scale = int(entry.get("price_scale", self.DEFAULT_SCALE))
            cache[symbol] = scale
            return scale
        tick_size = entry.get("tick_size")
        if tick_size:
            try:
                scale = int(round(1 / float(tick_size)))
                if scale > 0:
                    cache[symbol] = scale
                    return scale
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        cache[symbol] = self.DEFAULT_SCALE
        return self.DEFAULT_SCALE

    def tick_size_scaled(self, symbol: str) -> int:
        """Return tick_size * price_scale for *symbol*, or 0 if unknown.

        Used by strategy intent validation to reject under-scaled prices (e.g.
        a strategy passing raw 505 instead of 5_050_000 for a x10000-scaled
        symbol). A zero return disables the guard for symbols without metadata.
        """
        entry = self.meta.get(symbol)
        if not entry:
            return 0
        return self._safe_tick_size_scaled(entry, symbol)

    def contract_multiplier(self, symbol: str) -> int:
        """Return contract point-value multiplier for PnL calculation.

        Stocks return 1 (1 share × price diff = PnL in NTD).
        Futures return point_value from config (e.g. TMF=10, MXF=50, TXF=200).
        """
        entry = self.meta.get(symbol)
        if entry:
            pv = entry.get("point_value")
            if pv is not None:
                return int(pv)
        return 1

    def exchange(self, symbol: str) -> str:
        # Same cache discipline as ``price_scale``: bind first, cache hits only.
        cache = self._exchange_cache
        cached = cache.get(symbol)
        if cached is not None:
            return cached
        entry = self.meta.get(symbol)
        if entry is None:
            return ""
        value = str(entry.get("exchange", ""))
        cache[symbol] = value
        return value

    def product_type(self, symbol: str) -> str:
        cache = self._product_type_cache
        cached = cache.get(symbol)
        if cached is not None:
            return cached
        entry = self.meta.get(symbol)
        if entry is None:
            # Not cached: a "" here sent TMFK6 orders down the untyped
            # stock path until restart (2026-11-09).
            return ""
        raw = (
            entry.get("product_type")
            or entry.get("security_type")
            or entry.get("type")
            or entry.get("asset_type")
            or ""
        )
        raw = str(raw).strip().lower()
        if not raw:
            exchange = str(entry.get("exchange", "")).upper()
            if exchange in {"TSE", "OTC", "OES"}:
                raw = "stock"
            elif exchange in {"FUT", "FUTURES", "TAIFEX"}:
                raw = "future"
            elif exchange in {"OPT", "OPTIONS"}:
                raw = "option"
            elif exchange in {"IDX", "INDEX"}:
                raw = "index"
        cache[symbol] = raw
        return raw

    def order_params(self, symbol: str) -> dict[str, Any]:
        entry = self.meta.get(symbol) or {}
        params: dict[str, Any] = {}
        for key in ("order_cond", "order_lot", "oc_type", "account"):
            if key in entry and entry[key] is not None:
                params[key] = entry[key]
        return params

    def set_alias_map(self, alias_map: dict[str, str]) -> None:
        """Set alias→actual mapping from broker contract resolution.

        Called by bootstrap after broker login to propagate alias mappings
        (e.g. TXFR1 → TXFE6) resolved by ContractsRuntime.

        Also copies config metadata entries from alias codes to actual codes
        so that price_scale(), exchange(), product_type() etc. resolve correctly
        for the actual callback codes used at runtime.
        """
        self.alias_to_actual.update(alias_map)
        for config_code, actual_code in alias_map.items():
            if actual_code != config_code and actual_code not in self.meta:
                config_entry = self.meta.get(config_code)
                if config_entry is not None:
                    self.meta[actual_code] = config_entry

    def resolve_symbol(self, code: str) -> str:
        """Resolve a config symbol code to the actual callback code.

        Returns the actual month code if an alias mapping exists,
        otherwise returns the input code unchanged.
        """
        return self.alias_to_actual.get(code, code)

    def resolve_symbols(self, codes: set[str] | list[str]) -> set[str]:
        """Resolve a set of config symbol codes to actual callback codes."""
        return {self.alias_to_actual.get(c, c) for c in codes}

    def _safe_tick_size_scaled(self, entry: dict[str, Any], code: str) -> int:
        """Compute tick_size_scaled with fallback for invalid tick_size values."""
        raw = entry.get("tick_size", 1.0)
        try:
            val = float(raw)
            if val <= 0:
                val = 1.0
        except (TypeError, ValueError):
            val = 1.0
        return int(round(val * self.price_scale(code)))

    def _populate_registry(self) -> None:
        """Build InstrumentProfile entries from symbols.yaml metadata."""
        from hft_platform.core.instrument_registry import (
            FeeStructure,
            InstrumentProfile,
            InstrumentType,
            OptionRight,
            TradingHours,
        )

        profiles = []
        for code, entry in self.meta.items():
            ptype_str = self.product_type(code)
            itype = {
                "future": InstrumentType.FUTURE,
                "option": InstrumentType.OPTION,
                "stock": InstrumentType.EQUITY,
                "equity": InstrumentType.EQUITY,
                "index": InstrumentType.INDEX,
            }.get(ptype_str, InstrumentType.EQUITY)

            fee = FeeStructure(
                tax_rate_bps=int(entry.get("tax_rate_bps", 20)),
                commission_per_lot=int(entry.get("commission_per_lot", 130000)),
            )
            hours = TradingHours(
                day_open=str(entry.get("day_open", "08:45")),
                day_close=str(entry.get("day_close", "13:45")),
                night_open=entry.get("night_open"),
                night_close=entry.get("night_close"),
            )

            strike_scaled = None
            option_right = None
            if itype == InstrumentType.OPTION:
                raw_strike = entry.get("strike") or entry.get("strike_price")
                if raw_strike is not None:
                    strike_scaled = int(round(float(raw_strike) * self.price_scale(code)))
                raw_right = str(entry.get("right") or entry.get("option_right", ""))
                if raw_right.upper() in ("C", "CALL"):
                    option_right = OptionRight.CALL
                elif raw_right.upper() in ("P", "PUT"):
                    option_right = OptionRight.PUT
            # Expiry applies to every derivative, not just options. Gating it
            # with strike/right left every futures row in ``hft.market_data``
            # on the schema default 1970-01-01, and left ``InstrumentRegistry.
            # evict_expired`` — the registry's only capacity relief valve —
            # with nothing it could ever evict.
            expiry = _parse_instrument_expiry(entry.get("expiry"), code)
            if expiry is None and itype in (InstrumentType.FUTURE, InstrumentType.OPTION):
                expiry = _derive_monthly_expiry(code)

            profile = InstrumentProfile(
                symbol=code,
                instrument_type=itype,
                underlying=str(entry.get("underlying", "")),
                exchange=self.exchange(code),
                multiplier=self.contract_multiplier(code),
                tick_size_scaled=self._safe_tick_size_scaled(entry, code),
                price_scale=self.price_scale(code),
                fee_structure=fee,
                trading_hours=hours,
                lot_size=int(entry.get("lot_size", 1)),
                strike_scaled=strike_scaled,
                option_right=option_right,
                expiry=expiry,
            )
            profiles.append(profile)

        self.registry.reload_static(profiles)


def _extract_ts_ns(ts_val: Any) -> int:
    return timebase.coerce_ns(ts_val)


def _scale_book_levels(prices, volumes, scale):
    levels = []
    for raw_price, volume in zip(prices, volumes):
        price = float(raw_price)
        if price > 0:
            levels.append([int(round(price * scale)), int(volume)])
    return levels


def _event_contract_kw(event_cls: type, metadata: "SymbolMetadata | None", symbol: str) -> dict[str, Any]:
    if event_cls is TickEvent:
        supports_contract = _TICK_EVENT_SUPPORTS_CONTRACT
    elif event_cls is BidAskEvent:
        supports_contract = _BIDASK_EVENT_SUPPORTS_CONTRACT
    else:
        supports_contract = False
    if not supports_contract:
        return {}
    return {"contract": metadata.contract_ref(symbol) if metadata is not None else None}


def _local_tz_offset_ns(now_ns: int) -> int:
    try:
        now_dt = dt.datetime.fromtimestamp(now_ns / 1e9, tz=timebase.TZINFO)
        offset = timebase.TZINFO.utcoffset(now_dt)
    except Exception:
        return 0
    if offset is None:
        return 0
    return int(offset.total_seconds() * 1e9)


def _correct_local_tz_future_ts(exch_ts: int, now_ns: int) -> int | None:
    offset_ns = _local_tz_offset_ns(now_ns)
    if not offset_ns:
        return None
    corrected = exch_ts - offset_ns
    if abs(corrected - now_ns) >= abs(exch_ts - now_ns):
        return None
    if corrected - now_ns > _TS_MAX_FUTURE_NS:
        return None
    return corrected


def _clamp_future_ts(exch_ts: int, now_ns: int, topic: str, symbol: str) -> int:
    if not exch_ts or not _TS_MAX_FUTURE_NS:
        return exch_ts
    delta_ns = exch_ts - now_ns
    if delta_ns > _TS_MAX_FUTURE_NS:
        corrected = _correct_local_tz_future_ts(exch_ts, now_ns)
        if corrected is not None:
            logger.debug(
                "Exchange timestamp timezone shift corrected",
                topic=topic,
                symbol=symbol,
                delta_ns=delta_ns,
                corrected_delta_ns=corrected - now_ns,
            )
            return corrected
        logger.warning(
            "Exchange timestamp in future",
            topic=topic,
            symbol=symbol,
            delta_ns=delta_ns,
            max_future_ns=_TS_MAX_FUTURE_NS,
        )
        return now_ns
    return exch_ts


class MarketDataNormalizer:
    __slots__ = (
        "_seq_gen",
        "metadata",
        "price_codec",
        "metrics",
        "_scale_cache",
        "_last_local_ts_tick",
        "_last_local_ts_bidask",
        "_last_local_ts_snapshot",
        "_last_skew_log_ns",
        "_trade_classifier",
        "_latency_metrics_counter",
        "_latency_metrics_sample_every",
        "_skip_tick_missing_symbol",
        "_skip_tick_negative_price",
        "_skip_bidask_missing_symbol",
        "_skip_snapshot_missing_symbol",
    )

    def __init__(self, config_path: str | None = None, metadata: SymbolMetadata | None = None):
        import itertools

        self._seq_gen = itertools.count(1)
        # self._lock = Lock() # Removed
        self.metadata = metadata or SymbolMetadata(config_path)
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.metadata))
        self.metrics = MetricsRegistry.get()
        _skip = self.metrics.normalization_skip_total if self.metrics else None
        self._skip_tick_missing_symbol = _skip.labels(type="tick", reason="missing_symbol") if _skip else None
        self._skip_tick_negative_price = _skip.labels(type="tick", reason="negative_price") if _skip else None
        self._skip_bidask_missing_symbol = _skip.labels(type="bidask", reason="missing_symbol") if _skip else None
        self._skip_snapshot_missing_symbol = _skip.labels(type="snapshot", reason="missing_symbol") if _skip else None
        # (symbol, scale) in ONE reference: two separate fields could be read torn by a concurrent
        # worker (symbol of one write, scale of another). The tuple is replaced whole, never mutated.
        self._scale_cache: tuple[str | None, int] = (None, SymbolMetadata.DEFAULT_SCALE)
        self._last_local_ts_tick: int = 0
        self._last_local_ts_bidask: int = 0
        self._last_local_ts_snapshot: int = 0
        self._latency_metrics_counter: int = 0
        self._latency_metrics_sample_every: int = max(1, int(os.getenv("HFT_NORMALIZER_METRICS_SAMPLE_EVERY", "4")))
        self._last_skew_log_ns = 0
        self._trade_classifier = TradeClassifier()

    def _next_seq(self) -> int:
        return next(self._seq_gen)

    def _validate_and_sync_timestamp(self, exch_ts: int, local_ts: int, topic: str, symbol: str) -> tuple[int, int]:
        """Clamp future exchange timestamps and sync/cap local_ts against exch_ts.

        Returns ``(exch_ts, local_ts)`` after all adjustments.

        P2-b: The gauge ``feed_time_skew_ns`` previously only updated inside
        the over-threshold branch, so it stuck at the worst raw delta ever
        seen (a 7,997s value from a stale ts_epoch was observed in the
        live signal). It now reflects the *current* delta on every event,
        and a separate counter
        ``feed_time_skew_over_threshold_total{topic, severity}`` records
        how often we actually exceeded 1s / 10s / 60s.

        The gauge reports the delta **before** the clamp. Reporting it after
        made the gauge saturate: the clamp pins ``local_ts`` to
        ``exch_ts + _TS_MAX_LAG_NS``, so every clamped event read back as
        exactly the ceiling. Production on 2026-08-20 showed
        ``feed_time_skew_ns{topic="snapshot"} == 5e9`` — the ceiling to the
        nanosecond — while
        ``feed_time_skew_over_threshold_total{severity="critical_60s"}`` had
        counted 362 skews above *60* seconds on the same topic. The gauge
        said 5s, the counter said >60s, and only the counter was right.
        """
        raw_delta: int = 0
        if exch_ts:
            exch_ts = _clamp_future_ts(exch_ts, local_ts, topic, symbol)
            if local_ts < exch_ts:
                local_ts = exch_ts
            else:
                raw_delta = local_ts - exch_ts
                if _TS_MAX_LAG_NS and raw_delta > _TS_MAX_LAG_NS:
                    if _TS_SKEW_LOG_COOLDOWN_NS and (local_ts - self._last_skew_log_ns > _TS_SKEW_LOG_COOLDOWN_NS):
                        # Live signal showed 3 distinct mis-epoched
                        # contracts (EXFF6 ~3.95 days, TXFG6 ~33 min,
                        # MXFF6 ~7.5s) all collapsing to the clamp ceiling
                        # in the gauge, with no way to tell them apart.
                        # Always include the broker code + raw / clamped
                        # delta so the warning carries enough context to
                        # triage the underlying ts_epoch problem.
                        logger.warning(
                            "feed_time_skew",
                            topic=topic,
                            symbol=symbol,
                            raw_delta_ns=raw_delta,
                            clamped_delta_ns=_TS_MAX_LAG_NS,
                            max_ns=_TS_MAX_LAG_NS,
                        )
                        self._last_skew_log_ns = local_ts
                    if self.metrics:
                        # Sample over-threshold events at three severities
                        # so dashboards can alert on "any skew >1s" without
                        # being drowned by routine sub-second jitter.
                        if raw_delta > 60_000_000_000:
                            self.metrics.feed_time_skew_over_threshold_total.labels(
                                topic=topic, severity="critical_60s"
                            ).inc()
                        elif raw_delta > 10_000_000_000:
                            self.metrics.feed_time_skew_over_threshold_total.labels(
                                topic=topic, severity="high_10s"
                            ).inc()
                        else:
                            self.metrics.feed_time_skew_over_threshold_total.labels(
                                topic=topic, severity="warn_1s"
                            ).inc()
                    local_ts = exch_ts + _TS_MAX_LAG_NS
            # The gauge answers "right now, how skewed is this topic?", so it
            # must carry the raw delta. ``local_ts - exch_ts`` here is the
            # post-clamp delta, which is capped at _TS_MAX_LAG_NS and therefore
            # cannot express the magnitude that actually matters. ``raw_delta``
            # is 0 when the exchange timestamp was in the future (nothing to
            # report) and equals the post-clamp delta whenever no clamp fired,
            # so this is strictly more information for the same one ``set()``.
            if self.metrics:
                self.metrics.feed_time_skew_ns.labels(topic=topic).set(raw_delta)
        return exch_ts, local_ts

    def _record_latency_metrics(self, exch_ts: int, local_ts: int, last_ts_attr: str) -> None:
        """Record feed_latency_ns and feed_interarrival_ns metrics and update the
        named ``_last_local_ts_*`` attribute.

        Metrics are sampled every ``_latency_metrics_sample_every`` calls to reduce
        per-tick Prometheus overhead.  The ``last_ts_attr`` timestamp is always
        updated so that interarrival deltas remain accurate on sampled events.
        """
        if self.metrics:
            self._latency_metrics_counter += 1
            sample = self._latency_metrics_counter % self._latency_metrics_sample_every == 0
            if exch_ts and sample:
                lag_ns = local_ts - exch_ts
                if lag_ns >= 0:
                    self.metrics.feed_latency_ns.observe(lag_ns)
            last = getattr(self, last_ts_attr)
            if last and sample:
                delta = local_ts - last
                if delta >= 0:
                    self.metrics.feed_interarrival_ns.observe(delta)
            setattr(self, last_ts_attr, local_ts)

    @property
    def _last_symbol(self) -> str | None:
        return self._scale_cache[0]

    @property
    def _last_scale(self) -> int:
        return self._scale_cache[1]

    def _get_scale(self, symbol: str) -> int:
        cached = self._scale_cache
        if symbol == cached[0]:
            return cached[1]
        scale = int(self.metadata.price_scale(symbol))
        if scale <= 0:
            scale = 1
        self._scale_cache = (symbol, scale)
        return scale

    def _maybe_synthesize_side(
        self,
        symbol: str,
        bids: list | Any,
        asks: list | Any,
        scale: int,
    ) -> tuple[list | Any, list | Any, bool]:
        if not _SYNTHETIC_SIDE:
            return bids, asks, False

        def _has_levels(levels: list | Any) -> bool:
            if levels is None:
                return False
            if hasattr(levels, "size"):
                return getattr(levels, "size", 0) > 0
            return bool(levels)

        has_bids = _has_levels(bids)
        has_asks = _has_levels(asks)
        if has_bids and has_asks:
            return bids, asks, False

        tick_size = None
        entry = self.metadata.meta.get(symbol) if self.metadata else None
        raw_tick = entry.get("tick_size") if entry else None
        if raw_tick is not None:
            try:
                tick_size = float(raw_tick)
            except (TypeError, ValueError):
                tick_size = None
        if not tick_size and scale > 0:
            tick_size = 1.0 / float(scale)
        if not tick_size:
            tick_size = 1.0
        tick_int = max(1, int(round(tick_size * scale)))

        def _best_price(levels: list | Any) -> int:
            if hasattr(levels, "size"):
                levels_any = cast(Any, levels)
                return int(levels_any[0, 0]) if getattr(levels_any, "size", 0) > 0 else 0
            return int(levels[0][0]) if levels else 0

        synthesized = False
        if not has_bids and has_asks:
            best_ask = _best_price(asks)
            bid_price = max(best_ask - (tick_int * _SYNTHETIC_TICKS), 1)
            bids = [[bid_price, 1]]
            synthesized = True
        elif not has_asks and has_bids:
            best_bid = _best_price(bids)
            ask_price = max(best_bid + (tick_int * _SYNTHETIC_TICKS), 1)
            asks = [[ask_price, 1]]
            synthesized = True

        return bids, asks, synthesized

    def normalize_tick(self, payload: Any) -> TickEvent | tuple | None:
        # Fast path lookup without _coalesce
        # Assuming Shioaji standard keys: 'code', 'close', 'volume', 'ts'
        try:
            if isinstance(payload, dict):
                symbol = payload.get("code") or payload.get("Code")
                ts_val = payload.get("ts") if payload.get("ts") is not None else payload.get("datetime")
                close_val = payload.get("close") if payload.get("close") is not None else payload.get("Close")
                vol_val = payload.get("volume") if payload.get("volume") is not None else payload.get("Volume")
                total_volume = int(payload.get("total_volume") or 0)
                is_simtrade = bool(payload.get("simtrade") or 0)
                is_odd_lot = bool(payload.get("intraday_odd") or 0)
            else:
                symbol = getattr(payload, "code", None) or getattr(payload, "Code", None)
                _ts = getattr(payload, "ts", None)
                ts_val = _ts if _ts is not None else getattr(payload, "datetime", None)
                _close = getattr(payload, "close", None)
                close_val = _close if _close is not None else getattr(payload, "Close", None)
                _vol = getattr(payload, "volume", None)
                vol_val = _vol if _vol is not None else getattr(payload, "Volume", None)
                total_volume = int(getattr(payload, "total_volume", None) or 0)
                is_simtrade = bool(getattr(payload, "simtrade", None) or 0)
                is_odd_lot = bool(getattr(payload, "intraday_odd", None) or 0)

            if not symbol:
                if self._skip_tick_missing_symbol:
                    self._skip_tick_missing_symbol.inc()
                return None

            exch_ts = _extract_ts_ns(ts_val)

            if close_val is not None:
                scale = self._get_scale(symbol)
                price = int(round(float(close_val) * scale))
            else:
                price = 0

            if price <= 0:
                if self._skip_tick_negative_price:
                    self._skip_tick_negative_price.inc()
                return None

            volume = int(vol_val) if vol_val is not None else 0

            if _RETURN_TUPLE:
                _td, _tc = self._trade_classifier.classify(symbol, price)
                return (
                    "tick",
                    symbol,
                    price,
                    volume,
                    total_volume,
                    is_simtrade,
                    is_odd_lot,
                    exch_ts,
                    _td,
                    _tc,
                )

            local_ts = timebase.now_ns()
            exch_ts, local_ts = self._validate_and_sync_timestamp(exch_ts, local_ts, "tick", symbol)
            self._record_latency_metrics(exch_ts, local_ts, "_last_local_ts_tick")
            meta = MetaData(seq=self._next_seq(), topic="tick", source_ts=exch_ts, local_ts=local_ts)

            _td, _tc = self._trade_classifier.classify(symbol, price)
            return TickEvent(
                meta=meta,
                symbol=symbol,
                price=price,
                volume=volume,
                total_volume=total_volume,
                bid_side_total_vol=0,  # Optimization: skip less used fields unless needed
                ask_side_total_vol=0,
                is_simtrade=is_simtrade,
                is_odd_lot=is_odd_lot,
                trade_direction=_td,
                trade_confidence=_tc,
                **_event_contract_kw(TickEvent, self.metadata, symbol),
            )
        except Exception as e:
            logger.error("Normalize Tick Error", error=str(e), payload_type=str(type(payload)))
            if self.metrics:
                self.metrics.normalization_errors_total.labels(type="Tick").inc()
            return None

    def normalize_bidask(self, payload: Any) -> BidAskEvent | tuple | None:
        try:
            if isinstance(payload, dict):
                symbol = payload.get("code") or payload.get("Code")
                ts_val = payload.get("ts") if payload.get("ts") is not None else payload.get("datetime")
                bp = payload.get("bid_price") or []
                bv = payload.get("bid_volume") or []
                ap = payload.get("ask_price") or []
                av = payload.get("ask_volume") or []
            else:
                symbol = getattr(payload, "code", None) or getattr(payload, "Code", None)
                _ba_ts = getattr(payload, "ts", None)
                ts_val = _ba_ts if _ba_ts is not None else getattr(payload, "datetime", None)
                bp = getattr(payload, "bid_price", None) or []
                bv = getattr(payload, "bid_volume", None) or []
                ap = getattr(payload, "ask_price", None) or []
                av = getattr(payload, "ask_volume", None) or []
            if not symbol:
                if self._skip_bidask_missing_symbol:
                    self._skip_bidask_missing_symbol.inc()
                return None

            exch_ts = _extract_ts_ns(ts_val)

            scale = self._get_scale(symbol)

            bids = _scale_book_levels(bp, bv, scale)
            asks = _scale_book_levels(ap, av, scale)
            bids, asks, _ = self._maybe_synthesize_side(symbol, bids, asks, scale)

            if bids and asks:
                best_bid, best_ask = bids[0][0], asks[0][0]
                if best_bid > 0 and best_ask > 0:
                    self._trade_classifier.update_quotes(symbol, best_bid, best_ask)

            if _RETURN_TUPLE:
                return ("bidask", symbol, bids, asks, exch_ts, False)

            local_ts = timebase.now_ns()
            exch_ts, local_ts = self._validate_and_sync_timestamp(exch_ts, local_ts, "bidask", symbol)
            self._record_latency_metrics(exch_ts, local_ts, "_last_local_ts_bidask")
            meta = MetaData(seq=self._next_seq(), topic="bidask", source_ts=exch_ts, local_ts=local_ts)
            return BidAskEvent(
                meta=meta,
                symbol=symbol,
                bids=bids,
                asks=asks,
                **_event_contract_kw(BidAskEvent, self.metadata, symbol),
            )
        except Exception as e:
            logger.error("Normalize BidAsk Error", error=str(e), payload_type=str(type(payload)))
            if self.metrics:
                self.metrics.normalization_errors_total.labels(type="BidAsk").inc()
            return None

    def normalize_snapshot(self, payload: dict[str, Any]) -> BidAskEvent | tuple | None:
        if isinstance(payload, dict):
            symbol = payload.get("code") or payload.get("Code")
            ts_val = payload.get("ts") or payload.get("datetime")
            buy_price = payload.get("buy_price")
            buy_volume = payload.get("buy_volume")
            sell_price = payload.get("sell_price")
            sell_volume = payload.get("sell_volume")
        else:
            symbol = getattr(payload, "code", None) or getattr(payload, "Code", None)
            ts_val = getattr(payload, "ts", None) or getattr(payload, "datetime", None)
            buy_price = getattr(payload, "buy_price", None)
            buy_volume = getattr(payload, "buy_volume", None)
            sell_price = getattr(payload, "sell_price", None)
            sell_volume = getattr(payload, "sell_volume", None)

        if not symbol:
            if self._skip_snapshot_missing_symbol:
                self._skip_snapshot_missing_symbol.inc()
            return None

        # Guard: broker SDK may return list-typed fields for multi-level snapshots
        if isinstance(buy_price, (list, tuple)):
            buy_price = buy_price[0] if buy_price else 0
        if isinstance(sell_price, (list, tuple)):
            sell_price = sell_price[0] if sell_price else 0
        if isinstance(buy_volume, (list, tuple)):
            buy_volume = buy_volume[0] if buy_volume else 0
        if isinstance(sell_volume, (list, tuple)):
            sell_volume = sell_volume[0] if sell_volume else 0

        exch_ts = _extract_ts_ns(ts_val)

        scale = self._get_scale(symbol)

        if buy_price is not None or sell_price is not None:
            bids = []
            asks = []
            if buy_price:
                bids.append([int(round(float(buy_price) * scale)), int(buy_volume or 0)])
            if sell_price:
                asks.append([int(round(float(sell_price) * scale)), int(sell_volume or 0)])
            bids, asks, _ = self._maybe_synthesize_side(symbol, bids, asks, scale)

            if _RETURN_TUPLE:
                return ("bidask", symbol, bids, asks, exch_ts, True)

            local_ts = timebase.now_ns()
            exch_ts, local_ts = self._validate_and_sync_timestamp(exch_ts, local_ts, "snapshot", symbol)
            self._record_latency_metrics(exch_ts, local_ts, "_last_local_ts_snapshot")
            meta = MetaData(seq=self._next_seq(), topic="snapshot", source_ts=exch_ts, local_ts=local_ts)
            return BidAskEvent(
                meta=meta,
                symbol=symbol,
                bids=bids,
                asks=asks,
                is_snapshot=True,
                **_event_contract_kw(BidAskEvent, self.metadata, symbol),
            )

        event = self.normalize_bidask(payload)
        if isinstance(event, tuple):
            if len(event) > 6:
                return (event[0], event[1], event[2], event[3], event[4], True, *event[6:])
            return (event[0], event[1], event[2], event[3], event[4], True)
        if event:
            event = replace(event, is_snapshot=True)
        return event
