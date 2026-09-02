from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, Side
from hft_platform.core import timebase
from hft_platform.core.order_ids import OrderIdResolver
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.execution.field_map import BrokerExecFieldMap, ShioajiExecFieldMap
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("exec_normalizer")


@dataclass(slots=True)
class RawExecEvent:
    """
    Lightweight capture of Shioaji callback args.
    """

    topic: str  # "order" or "deal"
    data: Dict[str, Any]
    ingest_ts_ns: int


class ExecutionNormalizer:
    def __init__(
        self,
        raw_queue: Any = None,
        order_id_map: Optional[Dict[str, str]] = None,
        strategy_id_resolvers: Optional[list[Callable[[RawExecEvent], Optional[str]]]] = None,
        fee_calculator: Any = None,
        default_account_id: str = "",
        field_map: BrokerExecFieldMap | None = None,
        default_account_id_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self.raw_queue = raw_queue
        self._default_account_id = default_account_id
        # Resolved lazily: BrokerProtocol.get_default_account_id returns "" until
        # the session authenticates, and the normalizer is constructed before
        # login. Cached on first non-empty answer so the fill path stays O(1)
        # and never re-enters the SDK.
        self._default_account_id_provider = default_account_id_provider
        # Which "which candidate ids were present" shapes have already been
        # logged. The counter below carries the volume; the log only has to
        # carry the payload shape once per distinct shape, so a burst of
        # unattributable orders cannot turn into a log storm. Bounded at 17
        # entries (the subsets of four candidate fields, plus "none").
        self._unattributed_shapes_logged: set[str] = set()
        self.metrics = MetricsRegistry.get()
        self.metadata = SymbolMetadata()
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.metadata))
        # Maps broker order IDs -> order_key ("strategy_id:intent_id") or strategy_id.
        self.order_id_map = order_id_map if order_id_map is not None else {}
        self.order_id_resolver = OrderIdResolver(self.order_id_map)
        self.strategy_id_resolvers = strategy_id_resolvers or [
            self._resolve_from_injected,
            self._resolve_from_order_id_map,
            self._resolve_from_custom_field,
        ]
        self._fee_calculator = fee_calculator
        self.field_map: BrokerExecFieldMap = field_map or ShioajiExecFieldMap()

    def _resolve_default_account_id(self) -> str:
        """Return the broker session's account id, resolving it at most once.

        ``BrokerProtocol.get_default_account_id`` documents that its return
        value "must match the ``account_id`` value that ``ExecutionNormalizer``
        resolves from fill callbacks, so that recovery keys align with live
        fill keys in ``PositionStore``".  Nothing wired the two together, so
        this stayed ``""`` and step 3 of the resolution chain was dead: a fill
        arriving without its own account field fell straight through to the
        reject branch and never reached ``PositionStore``.
        """
        if self._default_account_id:
            return self._default_account_id
        provider = self._default_account_id_provider
        if provider is None:
            return ""
        try:
            resolved = str(provider() or "")
        except Exception:  # broker session may be mid-reconnect
            logger.warning("default_account_id_provider_failed", exc_info=True)
            return ""
        if resolved:
            self._default_account_id = resolved
            logger.info("execution_default_account_resolved", account_id_len=len(resolved))
        return resolved

    def _first_nonempty(self, payload: Any, keys: tuple[str, ...], default: Any = None) -> Any:
        """Walk *keys* on *payload* and return the first truthy value, else *default*."""
        for key in keys:
            value = self._payload_get(payload, key)
            if value not in (None, "", 0):
                return value
        return default

    def _first_str(self, payload: Any, keys: tuple[str, ...]) -> str:
        value = self._first_nonempty(payload, keys, default="")
        return str(value) if value else ""

    def _first_int(self, payload: Any, keys: tuple[str, ...]) -> int:
        value = self._first_nonempty(payload, keys, default=0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _unwrap_data(self, raw: RawExecEvent) -> tuple[Any, Any | None]:
        d = raw.data
        if isinstance(d, dict) and "payload" in d:
            return d.get("payload"), d.get("state") or d.get("status")
        return d, None

    @staticmethod
    def _payload_get(payload: Any, key: str, default: Any = None) -> Any:
        if payload is None:
            return default
        if isinstance(payload, dict):
            return payload.get(key, default)
        return getattr(payload, key, default)

    def _resolve_from_injected(self, raw: RawExecEvent) -> Optional[str]:
        """Highest-priority resolver: reads strategy_id injected by _on_exec
        from the pending fill index (bypasses order_id_map entirely)."""
        d = raw.data
        if isinstance(d, dict):
            val = d.get("_resolved_strategy_id")
            if val:
                return str(val)
        return None

    def _resolve_order_key_from_injected(self, raw: RawExecEvent) -> Optional[str]:
        """Order key injected by ``_on_exec`` from the pending-fill index."""
        d = raw.data
        if isinstance(d, dict):
            val = d.get("_resolved_order_key")
            if val:
                return str(val)
        return None

    def _resolve_from_custom_field(self, raw: RawExecEvent) -> Optional[str]:
        d, _ = self._unwrap_data(raw)
        order = self._payload_get(d, "order")
        fm = self.field_map
        custom_field = self._first_str(order, fm.custom_field_keys()) or self._first_str(d, fm.custom_field_keys())
        if custom_field:
            return custom_field
        return None

    def _resolve_from_order_id_map(self, raw: RawExecEvent) -> Optional[str]:
        d, _ = self._unwrap_data(raw)
        order = self._payload_get(d, "order")
        fm = self.field_map
        ord_no = self._first_str(order, fm.order_id_keys()) or self._first_str(d, fm.order_id_keys())
        seq_no = self._first_str(order, fm.sequence_id_keys()) or self._first_str(d, fm.sequence_id_keys())
        other_id = self._first_str(order, fm.other_id_keys()) or self._first_str(d, fm.other_id_keys())
        custom_field = self._first_str(order, fm.custom_field_keys()) or self._first_str(d, fm.custom_field_keys())
        resolved = self.order_id_resolver.resolve_strategy_id_from_candidates([ord_no, seq_no, other_id, custom_field])
        return resolved if resolved != "UNKNOWN" else None

    def _resolve_strategy_id(self, raw: RawExecEvent) -> str:
        for resolver in self.strategy_id_resolvers:
            try:
                candidate = resolver(raw)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                logger.debug("operation_fallback", error=str(exc))
                candidate = None
            if candidate:
                return candidate
        return "UNKNOWN"

    def _normalize_ts_ns(self, value: Any) -> int:
        if value is None:
            return timebase.now_ns()
        try:
            # Use float arithmetic to avoid Decimal allocation on exec path.
            # Detect magnitude then multiply, rounding at the end.
            fv = float(value)
        except (TypeError, ValueError, OverflowError):
            return timebase.now_ns()
        if fv <= 0.0:
            return timebase.now_ns()
        if fv > 1e17:
            return int(fv)  # already nanoseconds
        if fv > 1e14:
            return int(fv * 1_000)  # microseconds -> ns
        if fv > 1e11:
            return int(fv * 1_000_000)  # milliseconds -> ns
        return int(fv * 1_000_000_000)  # seconds -> ns

    def normalize_order(self, raw: RawExecEvent) -> Optional[OrderEvent]:
        self.metrics.execution_events_total.labels(type="order").inc()
        d, state = self._unwrap_data(raw)
        # Mapping logic (Mock implementation based on typical fields)
        # Shioaji fields: ord_no, seq_no, id (order_id), action, price, qty, status

        try:
            if not isinstance(d, dict):
                # Silent until 2026-08-12: both orders of 2026-08-10 died here
                # without a log line, so the visible symptom was only the fill
                # warning and nobody could see that the order path was dead.
                # A broker payload the platform cannot read is a defect in the
                # adapter's conversion, never a routine skip.
                logger.warning("normalize_order_non_mapping_payload", payload_type=str(type(d)))
                self.metrics.order_normalization_failed_total.labels(reason="non_mapping_payload").inc()
                return None

            status_payload = d.get("status")
            status_text = None
            exchange_ts = None
            if isinstance(status_payload, dict):
                status_text = status_payload.get("status")
                exchange_ts = status_payload.get("exchange_ts") or status_payload.get("ts")
            elif status_payload is not None:
                status_text = str(status_payload)

            fm = self.field_map
            op = d.get("operation", {}) if isinstance(d.get("operation"), dict) else {}
            op_type = self._first_nonempty(op, fm.operation_type_keys()) or self._first_nonempty(
                d, fm.operation_type_keys()
            )
            op_code = self._first_nonempty(op, fm.operation_code_keys()) or self._first_nonempty(
                d, fm.operation_code_keys()
            )

            status = self._map_status(status_text, op_type=op_type, op_code=op_code, state=state)

            if "order" in d and not isinstance(d.get("order"), dict):
                logger.warning("normalize_order_non_mapping_order_block", order_type=str(type(d.get("order"))))
                self.metrics.order_normalization_failed_total.labels(reason="non_mapping_order_block").inc()
                return None
            order = d.get("order", {}) if isinstance(d.get("order"), dict) else {}
            ord_no = self._first_str(order, fm.order_id_keys()) or self._first_str(d, fm.order_id_keys())
            seq_no = self._first_str(order, fm.sequence_id_keys()) or self._first_str(d, fm.sequence_id_keys())
            other_id = self._first_str(order, fm.other_id_keys()) or self._first_str(d, fm.other_id_keys())
            custom_field = self._first_str(order, fm.custom_field_keys()) or self._first_str(d, fm.custom_field_keys())
            oid = ord_no or seq_no or other_id
            strategy_id = self._resolve_strategy_id(raw)
            client_order_id = self.order_id_resolver.resolve_order_key_from_candidates(
                [ord_no, seq_no, other_id, custom_field]
            )
            if not client_order_id:
                # The order-ACK callback arrives ~4.3 ms before place_order()
                # returns the Trade carrying the broker ids, so the resolver
                # above CANNOT succeed on a first ACK -- it was empty on 414 of
                # 414 rows. _on_exec peeks the pending-fill index (registered
                # before dispatch) and injects the order key here.
                client_order_id = self._resolve_order_key_from_injected(raw) or ""
            if not client_order_id:
                self._record_unattributed_order(
                    ord_no=ord_no, seq_no=seq_no, other_id=other_id, custom_field=custom_field
                )

            contract = d.get("contract", {}) if isinstance(d.get("contract"), dict) else {}
            symbol = self._first_str(contract, fm.symbol_keys()) or self._first_str(d, fm.symbol_keys()) or "UNKNOWN"
            price_val = self._payload_get(order, "price") or 0
            price = self.price_codec.scale(symbol, price_val)

            submitted = self._first_int(order, fm.submitted_qty_keys())
            filled = self._first_int(order, fm.filled_qty_keys())
            return OrderEvent(
                order_id=oid,
                strategy_id=strategy_id,
                symbol=symbol,
                status=status,
                submitted_qty=submitted,
                filled_qty=filled,
                remaining_qty=max(0, submitted - filled),
                price=price,
                side=Side.BUY if "buy" in str(order.get("action", "")).lower() else Side.SELL,
                ingest_ts_ns=raw.ingest_ts_ns,
                broker_ts_ns=self._normalize_ts_ns(exchange_ts),
                client_order_id=client_order_id or "",
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Order normalization failed", error=str(e), data=d)
            return None

    def _record_unattributed_order(self, *, ord_no: str, seq_no: str, other_id: str, custom_field: str) -> None:
        """Count (and once per shape, describe) an order we cannot attribute.

        An order callback that normalizes cleanly but resolves to no order_key
        is written to ``hft.orders`` with ``client_order_id=''`` and
        ``strategy_id='UNKNOWN'``. Nothing rejects it and nothing counts it, so
        the row looks like every other row -- on 2026-08-21 production held 56
        such rows, 100% of the table, with an empty join key to ``hft.fills``,
        and no metric had ever moved.

        Only the *names* of the candidate fields are recorded, never their
        values: broker ids are account-linked identifiers and this runs on the
        logging path.
        """
        present = ",".join(
            name
            for name, value in (
                ("ordno", ord_no),
                ("seqno", seq_no),
                ("other", other_id),
                ("custom_field", custom_field),
            )
            if value
        )
        label = present or "none"
        self.metrics.order_attribution_unresolved_total.labels(present=label).inc()
        if label not in self._unattributed_shapes_logged:
            self._unattributed_shapes_logged.add(label)
            logger.warning(
                "order_attribution_unresolved",
                present=label,
                map_size=len(self.order_id_map),
                hint=(
                    "no candidate id matched order_id_map; the row will record "
                    "client_order_id='' and strategy_id='UNKNOWN'"
                ),
            )

    def normalize_fill(self, raw: RawExecEvent) -> Optional[FillEvent]:  # noqa: C901
        self.metrics.execution_events_total.labels(type="fill").inc()
        d, _ = self._unwrap_data(raw)
        fm = self.field_map

        def get(key: str, default: Any = None) -> Any:
            if isinstance(d, dict):
                return d.get(key, default)
            return getattr(d, key, default)

        try:
            qty = self._first_int(d, fm.submitted_qty_keys())
            if qty <= 0:
                logger.warning(
                    "normalize_fill_zero_qty", raw_keys=list(d.keys()) if isinstance(d, dict) else str(type(d))
                )
                return None
            price_value = get("price") or 0

            # Map action/side
            action = get("action")
            side = Side.BUY
            if action is not None:
                s = str(action).lower()
                if "sell" in s or action == -1:  # Broker may use Int or String
                    side = Side.SELL

            # Resolve symbol via broker field map; prefer full_code aliases.
            sym = self._first_str(d, fm.symbol_keys())
            if not sym:
                c = get("contract")
                if c:
                    sym = self._first_str(c, fm.symbol_keys())

            strategy_id = self._resolve_strategy_id(raw)
            order_id = self._first_str(d, fm.order_id_keys())
            other_id = self._first_str(d, fm.other_id_keys())
            custom_field = self._first_str(d, fm.custom_field_keys())
            client_order_id = self.order_id_resolver.resolve_order_key_from_candidates(
                [order_id, other_id, custom_field]
            )
            # scale() handles float/int/Decimal inputs with precision
            scale_price = self.price_codec.scale(sym, price_value)

            # Compute fees if calculator is available
            fee = 0
            tax = 0
            if self._fee_calculator is not None:
                side_str = "BUY" if side == Side.BUY else "SELL"
                breakdown = self._fee_calculator.compute(sym, side_str, qty, scale_price)
                fee = breakdown.total
                tax = breakdown.tax

            fill_id = self._first_str(d, fm.sequence_id_keys())
            if not fill_id:
                # Synthesize a fill_id when broker omits seqno (e.g. reconnect replays).
                # This enables downstream dedup to catch duplicate fills.
                #
                # CONTENT-ADDRESSED, deliberately. This used to append
                # ``self._synth_counter``, which increments per call, so the
                # same replayed deal produced a DIFFERENT id every time and
                # dedup could never match it -- defeating the one purpose the
                # docstring above claims. Worse, the id was always truthy, so
                # the router's content-keyed fallback
                # (``fill.fill_id or _synthesize_dedup_key(fill)``, four call
                # sites) was unreachable dead code. A reconnect replay
                # therefore double-counted position and realized PnL.
                #
                # Fields match ``execution.router._synthesize_dedup_key``
                # exactly, so the synthesized id and the fallback key agree on
                # what "the same fill" means. ``order_id`` is included for the
                # same reason it is there: without it, two different orders
                # filling the same qty at the same price in the same
                # nanosecond collapse into one.
                _exch_ts = self._normalize_ts_ns(get("ts"))
                fill_id = f"synth_{sym}|{order_id}|{side.name}|{scale_price}|{qty}|{_exch_ts}"
                self.metrics.synthetic_fill_id_total.inc()
                logger.info(
                    "synthetic_fill_id_generated",
                    fill_id=fill_id,
                    symbol=sym,
                    side=side.name,
                )

            # Account ID resolution chain:
            # 1. Explicit account_id field (Fubon or enriched payloads)
            # 2. Shioaji Account object (.account_id or str())
            # 3. default_account_id from broker session
            # 4. Reject — unknown account poisons PositionStore keys
            raw_account_id = get("account_id")
            if not raw_account_id:
                acct_obj = get("account")
                if acct_obj is not None:
                    raw_account_id = getattr(acct_obj, "account_id", None) or str(acct_obj)
            if not raw_account_id:
                raw_account_id = self._resolve_default_account_id()
            if not raw_account_id:
                logger.critical(
                    "fill_rejected_missing_account_id",
                    fill_id=fill_id,
                    symbol=sym,
                    side=side.name,
                    qty=qty,
                )
                self.metrics.execution_events_total.labels(type="fill_rejected_no_account").inc()
                return None
            account_id = str(raw_account_id)

            return FillEvent(
                fill_id=fill_id,
                account_id=account_id,
                order_id=order_id,
                strategy_id=strategy_id,
                symbol=sym,
                side=side,
                qty=qty,
                price=scale_price,
                fee=fee,
                tax=tax,
                ingest_ts_ns=raw.ingest_ts_ns,
                match_ts_ns=self._normalize_ts_ns(get("ts")),
                client_order_id=client_order_id or "",
            )
        except (KeyError, TypeError, ValueError, AttributeError) as _exc:
            logger.error("Fill normalization failed", error=str(_exc), data=d)
            return None

    def _map_status(
        self, s: str | None, op_type: str | None = None, op_code: str | None = None, state: Any = None
    ) -> OrderStatus:
        text = str(s).upper() if s is not None else ""
        if "PENDING" in text:
            return OrderStatus.PENDING_SUBMIT
        if "SUBMITTED" in text or "PRESUBMITTED" in text:
            return OrderStatus.SUBMITTED
        if "PART" in text and "FILL" in text:
            return OrderStatus.PARTIALLY_FILLED
        if "FILLED" in text:
            return OrderStatus.FILLED
        if "CANCELLED" in text or "CANCELED" in text:
            return OrderStatus.CANCELLED
        if "FAILED" in text:
            return OrderStatus.FAILED
        if op_type:
            op_upper = str(op_type).upper()
            if op_code and str(op_code) != "00":
                return OrderStatus.FAILED
            if "CANCEL" in op_upper:
                return OrderStatus.CANCELLED
            if "UPDATE" in op_upper or "NEW" in op_upper:
                return OrderStatus.SUBMITTED
        return OrderStatus.SUBMITTED
