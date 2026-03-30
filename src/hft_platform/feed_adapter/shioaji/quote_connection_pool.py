"""QuoteConnectionPool — manages multiple ShioajiClient sessions for quote subscriptions.

Each client owns an independent sj.Shioaji() session with its own watchdog,
reconnect orchestrator, and subscription tracking. All clients share the same
callback function, funneling data into a single raw_queue.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from typing import Any, Callable

import yaml
from structlog import get_logger

try:
    from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade
except ImportError:  # pragma: no cover
    ShioajiClientFacade = None  # type: ignore[assignment,misc]

logger = get_logger("feed_adapter.quote_connection_pool")

_SHIOAJI_MAX_CONNECTIONS = 5
_MAX_QUOTE_CONNECTIONS = _SHIOAJI_MAX_CONNECTIONS - 1
_MAX_SUBSCRIPTIONS_PER_CONN = 200


class QuoteConnectionPool:
    """Manages multiple ShioajiClientFacade instances for quote subscriptions.

    Duck-types as a single ShioajiClientFacade for MarketDataService compatibility.
    """

    __slots__ = (
        "_clients",
        "_shard_dir",
        "_shard_paths",
        "_num_conns",
        "_config",
        "_all_symbols",
        "_login_interval_s",
    )

    def __init__(self, symbols_path: str, shioaji_cfg: dict[str, Any], num_conns: int) -> None:
        if num_conns + 1 > _SHIOAJI_MAX_CONNECTIONS:
            raise ValueError(
                f"Total connections {num_conns + 1} (quote={num_conns} + order=1) "
                f"exceeds Shioaji limit of {_SHIOAJI_MAX_CONNECTIONS}"
            )
        if num_conns > _MAX_QUOTE_CONNECTIONS:
            raise ValueError(
                f"num_conns={num_conns} exceeds max quote connections {_MAX_QUOTE_CONNECTIONS}"
            )

        self._num_conns = num_conns
        self._config = shioaji_cfg
        self._login_interval_s = float(os.getenv("HFT_QUOTE_LOGIN_INTERVAL_S", "2"))

        with open(symbols_path, "r") as f:
            data = yaml.safe_load(f) or {}
        self._all_symbols: list[dict[str, Any]] = data.get("symbols", [])

        groups: dict[int, list[dict[str, Any]]] = {i: [] for i in range(num_conns)}
        for sym in self._all_symbols:
            g = sym.get("group", 0)
            if not isinstance(g, int) or g < 0 or g >= num_conns:
                raise ValueError(
                    f"Symbol {sym.get('code', '?')} has group={g} "
                    f"but only {num_conns} connections configured (valid: 0..{num_conns - 1})"
                )
            groups[g].append(sym)

        for g, syms in groups.items():
            if len(syms) > _MAX_SUBSCRIPTIONS_PER_CONN:
                raise ValueError(
                    f"Group {g} has {len(syms)} symbols, exceeds {_MAX_SUBSCRIPTIONS_PER_CONN} limit"
                )
            if not syms:
                logger.warning("Empty symbol group", group=g, num_conns=num_conns)

        self._shard_dir = tempfile.mkdtemp(prefix="hft_quote_pool_")
        self._shard_paths: list[str] = []
        self._clients: list[Any] = []

        for group_id in range(num_conns):
            shard_path = os.path.join(self._shard_dir, f"symbols_group_{group_id}.yaml")
            with open(shard_path, "w") as f:
                yaml.safe_dump({"symbols": groups[group_id]}, f, sort_keys=False)
            self._shard_paths.append(shard_path)

        logger.info(
            "QuoteConnectionPool initialized",
            num_conns=num_conns,
            groups={g: len(s) for g, s in groups.items()},
            shard_dir=self._shard_dir,
        )

    @property
    def num_conns(self) -> int:
        return self._num_conns

    def cleanup_shards(self) -> None:
        if self._shard_dir and os.path.isdir(self._shard_dir):
            shutil.rmtree(self._shard_dir, ignore_errors=True)
            self._shard_dir = ""

    def create_facades(self) -> None:
        """Create a ShioajiClientFacade for each connection group."""
        self._clients = []
        for group_id in range(self._num_conns):
            per_conn_cfg = dict(self._config)
            per_conn_cfg["session_lock_suffix"] = f"_conn{group_id}"
            facade = ShioajiClientFacade(
                config_path=self._shard_paths[group_id],
                shioaji_config=per_conn_cfg,
            )
            self._clients.append(facade)
            logger.info("Created facade for group", conn_id=group_id)

    def login_all(self) -> None:
        """Sequentially login each connection with a configurable interval."""
        for i, facade in enumerate(self._clients):
            log = logger.bind(conn_id=i)
            try:
                ok = facade.login()
                if ok:
                    log.info("Connection logged in")
                else:
                    log.error("Connection login failed")
            except Exception as exc:
                log.error("Connection login exception", error=str(exc))
            if i < len(self._clients) - 1 and self._login_interval_s > 0:
                time.sleep(self._login_interval_s)

    def login(self, *args: Any, **kwargs: Any) -> bool:
        self.login_all()
        return self.partial_login

    def subscribe_all(self, cb: Callable[..., Any]) -> None:
        """Subscribe each logged-in connection's symbol basket."""
        for i, facade in enumerate(self._clients):
            log = logger.bind(conn_id=i)
            if not facade.logged_in:
                log.warning("Skipping subscribe for unconnected facade")
                continue
            try:
                facade.subscribe_basket(cb)
                log.info("Subscribed", count=facade.subscribed_count)
            except Exception as exc:
                log.error("Subscribe failed", error=str(exc))

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        """Duck-type alias for MarketDataService compatibility."""
        self.subscribe_all(cb)

    def logout(self) -> None:
        """Logout and close all connections."""
        for i, facade in enumerate(self._clients):
            try:
                facade.close(logout=True)
                logger.bind(conn_id=i).info("Connection closed")
            except Exception as exc:
                logger.bind(conn_id=i).error("Close failed", error=str(exc))
        self.cleanup_shards()

    def close(self, logout: bool = False) -> None:
        if logout:
            self.logout()
        else:
            for facade in self._clients:
                try:
                    facade.close(logout=False)
                except Exception:
                    pass
            self.cleanup_shards()

    def shutdown(self, logout: bool = False) -> None:
        self.close(logout=logout)

    def get_client(self, group: int) -> Any:
        if 0 <= group < len(self._clients):
            return self._clients[group]
        raise ValueError(f"Invalid group {group}, valid: 0..{len(self._clients) - 1}")

    @property
    def logged_in(self) -> bool:
        return bool(self._clients) and all(c.logged_in for c in self._clients)

    @property
    def partial_login(self) -> bool:
        return any(c.logged_in for c in self._clients)

    @property
    def subscribed_count(self) -> int:
        return sum(getattr(c, "subscribed_count", 0) for c in self._clients)

    @property
    def mode(self) -> str:
        if self._clients:
            return self._clients[0]._client.mode
        return "unknown"

    @property
    def symbols(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for c in self._clients:
            result.extend(c._client.symbols)
        return result

    def health(self) -> dict[int, dict[str, Any]]:
        return {
            i: {
                "logged_in": c.logged_in,
                "subscribed_count": getattr(c, "subscribed_count", 0),
                "last_quote_ts": getattr(c._client, "_last_quote_data_ts", 0.0),
            }
            for i, c in enumerate(self._clients)
        }
