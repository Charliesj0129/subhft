from __future__ import annotations

import datetime as dt
import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("feed_adapter.contracts_runtime")

try:
    import shioaji as sj
except Exception:  # pragma: no cover
    sj = None

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient


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
        now_year = dt.datetime.now(timebase.TZINFO).year
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
            self._client.subscribed_codes = set(new_map)
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
            age_s = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
            return age_s > self._client._contract_refresh_s
        except Exception as exc:
            logger.warning("Cannot parse contract cache for staleness check", error=str(exc))
            return True

    def write_refresh_status(self, *, result: str, error: str | None = None) -> None:
        payload = {
            "updated_at_ns": time.time_ns(),
            "result": str(result),
            "error": (str(error) if error else None),
            "version": int(getattr(self._client, "_contract_refresh_version", 0) or 0),
            "policy": str(getattr(self._client, "_contract_refresh_resubscribe_policy", "none") or "none"),
            "thread_running": bool(getattr(self._client, "_contract_refresh_running", False)),
            "thread_alive": bool(getattr(self._client, "_contract_refresh_thread", None).is_alive())
            if getattr(self._client, "_contract_refresh_thread", None)
            else False,
            "lock_busy": bool(getattr(self._client, "_contract_refresh_lock", None).locked())
            if getattr(self._client, "_contract_refresh_lock", None)
            else False,
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
        except Exception:
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
            except Exception:
                pass
            return

        codes_before: set[str] = set()
        try:
            cache_path = Path(self._client._contract_cache_path)
            if cache_path.exists():
                old_cache = json.loads(cache_path.read_text(encoding="utf-8"))
                codes_before = {str(c.get("code", "")) for c in old_cache.get("contracts", []) if c.get("code")}
        except Exception:
            pass

        try:
            self._client._ensure_contracts()
            logger.info("Contract data refreshed from broker")
        except Exception as exc:
            logger.warning("Contract refresh fetch failed", error=str(exc))
            self.write_refresh_status(result="error", error=str(exc))
            try:
                if self._client.metrics and hasattr(self._client.metrics, "contract_refresh_total"):
                    self._client.metrics.contract_refresh_total.labels(result="error").inc()
            except Exception:
                pass
            self._client._contract_refresh_lock.release()
            return

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

            try:
                for c in self._client.api.Contracts.Stocks.TSE:
                    raw_contracts.append(_normalize(c, "TSE", "stock"))
                for c in self._client.api.Contracts.Stocks.OTC:
                    raw_contracts.append(_normalize(c, "OTC", "stock"))
            except Exception:
                pass
            try:
                for root in self._client.api.Contracts.Futures.keys():
                    for c in self._client.api.Contracts.Futures[root]:
                        raw_contracts.append(_normalize(c, "TAIFEX", "future"))
            except Exception:
                pass
            try:
                for root in self._client.api.Contracts.Options.keys():
                    for c in self._client.api.Contracts.Options[root]:
                        raw_contracts.append(_normalize(c, "TAIFEX", "option"))
            except Exception:
                pass

            write_contract_cache(raw_contracts, self._client._contract_cache_path)

            codes_after = {str(c.get("code", "")) for c in raw_contracts if c.get("code")}
            added = sorted(codes_after - codes_before)
            removed = sorted(codes_before - codes_after)
            self._client._contract_refresh_version += 1
            self._client._contract_refresh_last_diff = {
                "version": int(self._client._contract_refresh_version),
                "contracts_before": len(codes_before),
                "contracts_after": len(codes_after),
                "added_count": len(added),
                "removed_count": len(removed),
                "added_codes": added[:200],
                "removed_codes": removed[:200],
            }
            logger.info(
                "contract_refresh_diff",
                version=self._client._contract_refresh_last_diff["version"],
                contracts_before=self._client._contract_refresh_last_diff["contracts_before"],
                contracts_after=self._client._contract_refresh_last_diff["contracts_after"],
                added_count=self._client._contract_refresh_last_diff["added_count"],
                removed_count=self._client._contract_refresh_last_diff["removed_count"],
            )
            try:
                if self._client.metrics and hasattr(self._client.metrics, "contract_refresh_symbols_changed_total"):
                    if not added and not removed:
                        self._client.metrics.contract_refresh_symbols_changed_total.labels(change="same").inc()
                    if added:
                        self._client.metrics.contract_refresh_symbols_changed_total.labels(change="added").inc()
                    if removed:
                        self._client.metrics.contract_refresh_symbols_changed_total.labels(change="removed").inc()
            except Exception:
                pass

            contract_index = ContractIndex(contracts=raw_contracts)
            list_path = Path(Path(self._client.config_path).parent / "symbols.list")
            if not list_path.exists():
                list_path = Path(DEFAULT_LIST_PATH)
            build_result = build_symbols(str(list_path), contract_index)
            if build_result.symbols:
                write_symbols_yaml(build_result.symbols, self._client.config_path)
                logger.info(
                    "Symbols rebuilt from fresh contracts",
                    count=len(build_result.symbols),
                    errors=len(build_result.errors),
                )
            if build_result.errors:
                logger.warning("Symbol rebuild had errors", errors=build_result.errors[:5])
        except Exception as exc:
            logger.warning("Symbol rebuild failed, keeping existing symbols", error=str(exc))

        try:
            self._client._load_config()
            logger.info("Symbol config reloaded after contract refresh", symbol_count=len(self._client.symbols))
        except Exception as exc:
            logger.warning("Symbol config reload failed after contract refresh", error=str(exc))
        finally:
            try:
                if self._client.metrics and hasattr(self._client.metrics, "contract_refresh_total"):
                    self._client.metrics.contract_refresh_total.labels(result="ok").inc()
            except Exception:
                pass
            policy = self._client._contract_refresh_resubscribe_policy
            should_resub = policy == "all"
            if policy == "diff":
                diff = self._client._contract_refresh_last_diff or {}
                should_resub = bool(diff.get("added_codes") or diff.get("removed_codes"))
            if should_resub and self._client.logged_in:
                try:
                    logger.info("contract_refresh_resubscribe", policy=policy)
                    self._client._resubscribe_all()
                except Exception as exc:
                    logger.warning("contract_refresh_resubscribe_failed", error=str(exc))
            self._client._contract_refresh_lock.release()
            self.write_refresh_status(result="ok")

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
        if len(self._client.symbols) > self._client.MAX_SUBSCRIPTIONS:
            logger.warning(
                "preflight_subscription_count_exceeded",
                symbol_count=len(self._client.symbols),
                limit=self._client.MAX_SUBSCRIPTIONS,
            )
            errors.append("subscription_count_exceeded")
        logger.info("preflight_complete", passed_all=(len(errors) == 0), errors=errors)

    def start_contract_refresh_thread(self) -> None:
        if self._client._contract_refresh_running:
            return
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
