# QuoteConnectionPool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand Shioaji quote subscription capacity from 200 (1 connection) to 1000 (5 connections) by pooling multiple sessions with static symbol group allocation.

**Architecture:** A new `QuoteConnectionPool` class owns N `ShioajiClientFacade` instances, each reading from a per-group YAML shard file. The Pool duck-types as a single facade so `MarketDataService` requires zero changes. Bootstrap branches on `HFT_QUOTE_CONNECTIONS` env var (default 1 = original behavior).

**Tech Stack:** Python 3.12, structlog, prometheus_client, PyYAML, pytest

**Spec:** `docs/superpowers/specs/2026-03-30-quote-connection-pool-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py` | CREATE | QuoteConnectionPool class — owns N facades, shard generation, duck-type interface |
| `src/hft_platform/feed_adapter/shioaji/client.py` | MODIFY (1 line) | Append `session_lock_suffix` to lock path |
| `src/hft_platform/config/_symbols_parsing.py` | MODIFY (~5 lines) | Support `group` key in `parse_kv_tokens` |
| `src/hft_platform/services/bootstrap.py` | MODIFY (~15 lines) | Branch on `HFT_QUOTE_CONNECTIONS`, route `client=` to `order_client` |
| `tests/unit/test_quote_connection_pool.py` | CREATE | Pool unit tests |
| `tests/unit/test_symbols_parsing_group.py` | CREATE | `group=N` parsing tests |

---

### Task 1: Support `group` key in symbol parsing

**Files:**
- Test: `tests/unit/test_symbols_parsing_group.py`
- Modify: `src/hft_platform/config/_symbols_parsing.py:231-273`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_symbols_parsing_group.py
"""Tests for group attribute support in symbol parsing."""

from hft_platform.config._symbols_parsing import parse_kv_tokens


def test_parse_kv_tokens_group_integer():
    result = parse_kv_tokens(["group=2"])
    assert result["group"] == 2


def test_parse_kv_tokens_group_zero():
    result = parse_kv_tokens(["group=0"])
    assert result["group"] == 0


def test_parse_kv_tokens_group_invalid_ignored():
    result = parse_kv_tokens(["group=abc"])
    assert "group" not in result
    assert "_invalid" in result


def test_parse_kv_tokens_group_with_other_attrs():
    result = parse_kv_tokens(["exchange=TSE", "group=1", "price_scale=10000"])
    assert result["exchange"] == "TSE"
    assert result["group"] == 1
    assert result["price_scale"] == 10000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_symbols_parsing_group.py -v`
Expected: FAIL — `group` key not present in result dict.

- [ ] **Step 3: Add `group` handling to `parse_kv_tokens`**

In `src/hft_platform/config/_symbols_parsing.py`, add this block after the `contract_size` handling (around line 272, before the `return attrs`):

```python
        elif key in {"group"}:
            try:
                attrs["group"] = int(value)
            except ValueError:
                attrs.setdefault("_invalid", []).append(f"group={value}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_symbols_parsing_group.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/test_symbols_parsing_group.py src/hft_platform/config/_symbols_parsing.py
git commit -m "feat(config): support group=N attribute in symbol parsing"
```

---

### Task 2: Session lock suffix support in ShioajiClient

**Files:**
- Test: `tests/unit/test_quote_connection_pool.py` (start the test file)
- Modify: `src/hft_platform/feed_adapter/shioaji/client.py:390`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_quote_connection_pool.py
"""Tests for QuoteConnectionPool and related changes."""

import os
import unittest.mock as mock

import pytest


class TestSessionLockSuffix:
    """Verify session_lock_suffix is appended to lock path."""

    def test_lock_path_includes_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHIOAJI_API_KEY", "TESTKEY123")
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "SECRET")
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_SHIOAJI_SESSION_LOCK_DIR", str(tmp_path))

        # Write minimal symbols YAML
        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text("symbols: []")

        with mock.patch("hft_platform.feed_adapter.shioaji.client._sdk", return_value=None):
            from hft_platform.feed_adapter.shioaji.client import ShioajiClient

            client = ShioajiClient(
                config_path=str(sym_path),
                shioaji_config={"session_lock_suffix": "_conn1"},
            )
            assert "_conn1.lock" in client._session_lock_path

    def test_lock_path_no_suffix_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHIOAJI_API_KEY", "TESTKEY123")
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "SECRET")
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_SHIOAJI_SESSION_LOCK_DIR", str(tmp_path))

        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text("symbols: []")

        with mock.patch("hft_platform.feed_adapter.shioaji.client._sdk", return_value=None):
            from hft_platform.feed_adapter.shioaji.client import ShioajiClient

            client = ShioajiClient(config_path=str(sym_path))
            assert "_conn" not in client._session_lock_path
            assert client._session_lock_path.endswith(".lock")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py::TestSessionLockSuffix::test_lock_path_includes_suffix -v`
Expected: FAIL — `_conn1` not in lock path.

- [ ] **Step 3: Apply the 1-line change to client.py**

In `src/hft_platform/feed_adapter/shioaji/client.py`, change line 390 from:

```python
        self._session_lock_path = str(Path(lock_dir) / f"shioaji_session_{lock_id}.lock")
```

to:

```python
        _lock_suffix = self.shioaji_config.get("session_lock_suffix", "")
        self._session_lock_path = str(Path(lock_dir) / f"shioaji_session_{lock_id}{_lock_suffix}.lock")
```

- [ ] **Step 4: Run tests to verify both pass**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py::TestSessionLockSuffix -v`
Expected: Both tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/feed_adapter/shioaji/client.py tests/unit/test_quote_connection_pool.py
git commit -m "feat(shioaji): support session_lock_suffix for multi-connection lock isolation"
```

---

### Task 3: QuoteConnectionPool — init, validation, shard generation

**Files:**
- Create: `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py`
- Test: `tests/unit/test_quote_connection_pool.py` (extend)

- [ ] **Step 1: Write failing tests for validation and init**

Append to `tests/unit/test_quote_connection_pool.py`:

```python
import tempfile
import yaml


class TestQuoteConnectionPoolValidation:
    """Test fail-fast validation in Pool constructor."""

    def _make_symbols_yaml(self, symbols: list[dict], tmp_path) -> str:
        path = tmp_path / "symbols.yaml"
        path.write_text(yaml.safe_dump({"symbols": symbols}))
        return str(path)

    def test_rejects_too_many_connections(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
            QuoteConnectionPool,
        )

        sym_path = self._make_symbols_yaml([], tmp_path)
        with pytest.raises(ValueError, match="exceeds Shioaji limit of 5"):
            QuoteConnectionPool(sym_path, {}, num_conns=5)

    def test_rejects_group_exceeding_200(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
            QuoteConnectionPool,
        )

        symbols = [{"code": f"SYM{i}", "exchange": "TSE", "group": 0} for i in range(201)]
        sym_path = self._make_symbols_yaml(symbols, tmp_path)
        with pytest.raises(ValueError, match="Group 0 has 201 symbols"):
            QuoteConnectionPool(sym_path, {}, num_conns=1)

    def test_rejects_group_out_of_range(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
            QuoteConnectionPool,
        )

        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 3}]
        sym_path = self._make_symbols_yaml(symbols, tmp_path)
        with pytest.raises(ValueError, match="group=3 but only 2 connections"):
            QuoteConnectionPool(sym_path, {}, num_conns=2)

    def test_default_group_zero_when_omitted(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
            QuoteConnectionPool,
        )

        symbols = [{"code": "TXFC0", "exchange": "TAIFEX"}]
        sym_path = self._make_symbols_yaml(symbols, tmp_path)
        # Should not raise — defaults to group 0
        pool = QuoteConnectionPool(sym_path, {}, num_conns=1)
        assert pool.num_conns == 1

    def test_shard_files_created(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
            QuoteConnectionPool,
        )

        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        sym_path = self._make_symbols_yaml(symbols, tmp_path)
        pool = QuoteConnectionPool(sym_path, {}, num_conns=2)
        assert len(pool._shard_paths) == 2
        for p in pool._shard_paths:
            assert os.path.exists(p)
            with open(p) as f:
                data = yaml.safe_load(f)
                assert "symbols" in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py::TestQuoteConnectionPoolValidation -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement QuoteConnectionPool init and validation**

Create `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py`:

```python
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

logger = get_logger("feed_adapter.quote_connection_pool")

# Maximum Shioaji connections per person ID.
_SHIOAJI_MAX_CONNECTIONS = 5
# 1 reserved for order_client.
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

        # Load all symbols from the master config.
        with open(symbols_path, "r") as f:
            data = yaml.safe_load(f) or {}
        self._all_symbols: list[dict[str, Any]] = data.get("symbols", [])

        # Validate group assignments.
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

        # Generate per-group YAML shard files.
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
        """Remove temporary shard directory."""
        if self._shard_dir and os.path.isdir(self._shard_dir):
            shutil.rmtree(self._shard_dir, ignore_errors=True)
            self._shard_dir = ""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py::TestQuoteConnectionPoolValidation -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py tests/unit/test_quote_connection_pool.py
git commit -m "feat(shioaji): add QuoteConnectionPool with validation and shard generation"
```

---

### Task 4: QuoteConnectionPool — facade creation, login, subscribe, logout

**Files:**
- Modify: `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py`
- Test: `tests/unit/test_quote_connection_pool.py` (extend)

- [ ] **Step 1: Write failing tests for facade lifecycle**

Append to `tests/unit/test_quote_connection_pool.py`:

```python
class TestQuoteConnectionPoolLifecycle:
    """Test login/subscribe/logout orchestration via mocked facades."""

    def _make_pool_with_symbols(self, tmp_path, symbols, num_conns):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
            QuoteConnectionPool,
        )

        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
        return QuoteConnectionPool(str(sym_path), {}, num_conns=num_conns)

    def test_create_facades_builds_correct_count(self, tmp_path):
        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 2)

        with mock.patch(
            "hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade"
        ) as MockFacade:
            MockFacade.return_value = mock.MagicMock()
            pool.create_facades()
            assert MockFacade.call_count == 2
            assert len(pool._clients) == 2

    def test_create_facades_injects_lock_suffix(self, tmp_path):
        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 1)

        with mock.patch(
            "hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade"
        ) as MockFacade:
            MockFacade.return_value = mock.MagicMock()
            pool.create_facades()
            call_kwargs = MockFacade.call_args_list[0][1]
            assert call_kwargs["shioaji_config"]["session_lock_suffix"] == "_conn0"

    def test_login_all_calls_each_facade(self, tmp_path):
        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 2)
        facade0 = mock.MagicMock()
        facade0.login.return_value = True
        facade0.logged_in = True
        facade1 = mock.MagicMock()
        facade1.login.return_value = True
        facade1.logged_in = True
        pool._clients = [facade0, facade1]
        pool._login_interval_s = 0  # no delay in tests

        pool.login_all()
        facade0.login.assert_called_once()
        facade1.login.assert_called_once()

    def test_login_all_partial_failure(self, tmp_path):
        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 2)
        facade0 = mock.MagicMock()
        facade0.login.return_value = True
        facade0.logged_in = True
        facade1 = mock.MagicMock()
        facade1.login.return_value = False
        facade1.logged_in = False
        pool._clients = [facade0, facade1]
        pool._login_interval_s = 0

        pool.login_all()
        assert pool.partial_login is True
        assert pool.logged_in is False

    def test_subscribe_basket_calls_each_logged_in_facade(self, tmp_path):
        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 2)
        facade0 = mock.MagicMock()
        facade0.logged_in = True
        facade0.subscribed_count = 1
        facade1 = mock.MagicMock()
        facade1.logged_in = False
        facade1.subscribed_count = 0
        pool._clients = [facade0, facade1]

        cb = mock.MagicMock()
        pool.subscribe_basket(cb)
        facade0.subscribe_basket.assert_called_once_with(cb)
        facade1.subscribe_basket.assert_not_called()

    def test_logout_calls_all_facades(self, tmp_path):
        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 1)
        facade0 = mock.MagicMock()
        pool._clients = [facade0]

        pool.logout()
        facade0.close.assert_called_once_with(logout=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py::TestQuoteConnectionPoolLifecycle -v`
Expected: FAIL — `create_facades`, `login_all`, etc. not implemented.

- [ ] **Step 3: Implement lifecycle methods**

Add to `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py`, below `cleanup_shards`:

```python
    def create_facades(self) -> None:
        """Create a ShioajiClientFacade for each connection group."""
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

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

    # Duck-type alias for MarketDataService compatibility.
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
        """Duck-type alias."""
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
        """Duck-type alias."""
        self.close(logout=logout)

    def get_client(self, group: int) -> Any:
        """Return the facade for a specific group (diagnostics)."""
        if 0 <= group < len(self._clients):
            return self._clients[group]
        raise ValueError(f"Invalid group {group}, valid: 0..{len(self._clients) - 1}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py::TestQuoteConnectionPoolLifecycle -v`
Expected: All 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py tests/unit/test_quote_connection_pool.py
git commit -m "feat(shioaji): add QuoteConnectionPool lifecycle — login, subscribe, logout"
```

---

### Task 5: QuoteConnectionPool — duck-type properties and health

**Files:**
- Modify: `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py`
- Test: `tests/unit/test_quote_connection_pool.py` (extend)

- [ ] **Step 1: Write failing tests for properties**

Append to `tests/unit/test_quote_connection_pool.py`:

```python
class TestQuoteConnectionPoolProperties:
    """Test duck-type properties."""

    def _make_pool(self, tmp_path, num_conns=2):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
            QuoteConnectionPool,
        )

        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
        return QuoteConnectionPool(str(sym_path), {}, num_conns=num_conns)

    def test_logged_in_all_true(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.logged_in = True
        f1 = mock.MagicMock()
        f1.logged_in = True
        pool._clients = [f0, f1]
        assert pool.logged_in is True

    def test_logged_in_one_false(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.logged_in = True
        f1 = mock.MagicMock()
        f1.logged_in = False
        pool._clients = [f0, f1]
        assert pool.logged_in is False

    def test_partial_login(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.logged_in = True
        f1 = mock.MagicMock()
        f1.logged_in = False
        pool._clients = [f0, f1]
        assert pool.partial_login is True

    def test_subscribed_count_sum(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.subscribed_count = 20
        f1 = mock.MagicMock()
        f1.subscribed_count = 150
        pool._clients = [f0, f1]
        assert pool.subscribed_count == 170

    def test_mode_from_first_client(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0._client.mode = "simulation"
        pool._clients = [f0]
        assert pool.mode == "simulation"

    def test_symbols_concatenation(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0._client.symbols = [{"code": "TXFC0"}]
        f1 = mock.MagicMock()
        f1._client.symbols = [{"code": "2330"}]
        pool._clients = [f0, f1]
        syms = pool.symbols
        codes = [s["code"] for s in syms]
        assert codes == ["TXFC0", "2330"]

    def test_health(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.logged_in = True
        f0.subscribed_count = 20
        f0._client._last_quote_data_ts = 1000.0
        pool._clients = [f0]
        h = pool.health()
        assert 0 in h
        assert h[0]["logged_in"] is True
        assert h[0]["subscribed_count"] == 20
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py::TestQuoteConnectionPoolProperties -v`
Expected: FAIL — properties not implemented.

- [ ] **Step 3: Implement properties**

Add to `QuoteConnectionPool` class in `quote_connection_pool.py`:

```python
    @property
    def logged_in(self) -> bool:
        """True only if ALL clients are logged in."""
        return bool(self._clients) and all(c.logged_in for c in self._clients)

    @property
    def partial_login(self) -> bool:
        """True if at least one client is logged in."""
        return any(c.logged_in for c in self._clients)

    @property
    def subscribed_count(self) -> int:
        """Sum of all clients' subscribed counts."""
        return sum(getattr(c, "subscribed_count", 0) for c in self._clients)

    @property
    def mode(self) -> str:
        """Proxy from first client."""
        if self._clients:
            return self._clients[0]._client.mode
        return "unknown"

    @property
    def symbols(self) -> list[dict[str, Any]]:
        """Concatenation of all clients' symbol lists."""
        result: list[dict[str, Any]] = []
        for c in self._clients:
            result.extend(c._client.symbols)
        return result

    def health(self) -> dict[int, dict[str, Any]]:
        """Return per-connection health status."""
        return {
            i: {
                "logged_in": c.logged_in,
                "subscribed_count": getattr(c, "subscribed_count", 0),
                "last_quote_ts": getattr(c._client, "_last_quote_data_ts", 0.0),
            }
            for i, c in enumerate(self._clients)
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py::TestQuoteConnectionPoolProperties -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py tests/unit/test_quote_connection_pool.py
git commit -m "feat(shioaji): add QuoteConnectionPool duck-type properties and health"
```

---

### Task 6: Bootstrap integration

**Files:**
- Modify: `src/hft_platform/services/bootstrap.py:556-585` and `:793-797` and `:1113-1115`

- [ ] **Step 1: Modify `_build_broker_clients` to support pool creation**

In `src/hft_platform/services/bootstrap.py`, change the `_build_broker_clients` method. Replace lines 584-585:

```python
        # Default: shioaji
        return ShioajiClientFacade(symbols_path, base_shioaji_cfg), ShioajiClientFacade(symbols_path, order_cfg)
```

with:

```python
        # Default: shioaji
        num_conns = int(os.getenv("HFT_QUOTE_CONNECTIONS", "1"))
        if num_conns > 1:
            from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

            pool = QuoteConnectionPool(symbols_path, base_shioaji_cfg, num_conns)
            pool.create_facades()
            return pool, ShioajiClientFacade(symbols_path, order_cfg)
        return ShioajiClientFacade(symbols_path, base_shioaji_cfg), ShioajiClientFacade(symbols_path, order_cfg)
```

- [ ] **Step 2: Route StartupPositionVerifier to order_client**

In `src/hft_platform/services/bootstrap.py`, change line 793-794 from:

```python
        startup_verifier = StartupPositionVerifier(
            client=md_client,
```

to:

```python
        startup_verifier = StartupPositionVerifier(
            client=order_client,
```

- [ ] **Step 3: Route service registry `client=` to order_client**

In `src/hft_platform/services/bootstrap.py`, change line 1115 from:

```python
            client=md_client,
```

to:

```python
            client=order_client,
```

- [ ] **Step 4: Run existing tests to verify no regression**

Run: `uv run pytest tests/unit/test_bootstrap.py -v --timeout=30 2>&1 | tail -20`
Expected: All existing bootstrap tests PASS.

Run: `uv run pytest tests/unit/test_quote_connection_pool.py -v`
Expected: All pool tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/services/bootstrap.py
git commit -m "feat(bootstrap): integrate QuoteConnectionPool with HFT_QUOTE_CONNECTIONS env var"
```

---

### Task 7: Prometheus metrics for pool connections

**Files:**
- Modify: `src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py`
- Test: `tests/unit/test_quote_connection_pool.py` (extend)

- [ ] **Step 1: Write failing test for metrics**

Append to `tests/unit/test_quote_connection_pool.py`:

```python
class TestQuoteConnectionPoolMetrics:
    """Test Prometheus metrics reporting."""

    def test_update_metrics_sets_gauges(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
            QuoteConnectionPool,
        )

        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
        pool = QuoteConnectionPool(str(sym_path), {}, num_conns=1)

        facade = mock.MagicMock()
        facade.logged_in = True
        facade.subscribed_count = 15
        facade._client._last_quote_data_ts = 1000.0
        pool._clients = [facade]

        # Should not raise
        pool.update_metrics()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py::TestQuoteConnectionPoolMetrics -v`
Expected: FAIL — `update_metrics` not defined.

- [ ] **Step 3: Implement metrics**

Add imports at top of `quote_connection_pool.py`:

```python
try:
    from prometheus_client import Gauge
except ImportError:
    Gauge = None
```

Add module-level metric definitions after the logger:

```python
_METRIC_SUBSCRIBED = None
_METRIC_LOGGED_IN = None
_METRIC_LAST_DATA_AGE = None


def _ensure_metrics() -> None:
    global _METRIC_SUBSCRIBED, _METRIC_LOGGED_IN, _METRIC_LAST_DATA_AGE
    if Gauge is None or _METRIC_SUBSCRIBED is not None:
        return
    _METRIC_SUBSCRIBED = Gauge(
        "hft_quote_conn_subscribed_count",
        "Subscribed symbol count per quote connection",
        ["conn_id"],
    )
    _METRIC_LOGGED_IN = Gauge(
        "hft_quote_conn_logged_in",
        "Login state per quote connection",
        ["conn_id"],
    )
    _METRIC_LAST_DATA_AGE = Gauge(
        "hft_quote_conn_last_data_age_s",
        "Seconds since last quote data per connection",
        ["conn_id"],
    )
```

Add method to `QuoteConnectionPool`:

```python
    def update_metrics(self) -> None:
        """Push per-connection metrics to Prometheus gauges."""
        _ensure_metrics()
        if _METRIC_SUBSCRIBED is None:
            return
        from hft_platform.core import timebase

        now_s = timebase.now_s()
        for i, c in enumerate(self._clients):
            label = str(i)
            _METRIC_SUBSCRIBED.labels(conn_id=label).set(getattr(c, "subscribed_count", 0))
            _METRIC_LOGGED_IN.labels(conn_id=label).set(1 if c.logged_in else 0)
            last_ts = getattr(c._client, "_last_quote_data_ts", 0.0)
            age = now_s - last_ts if last_ts > 0 else -1
            _METRIC_LAST_DATA_AGE.labels(conn_id=label).set(age)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py::TestQuoteConnectionPoolMetrics -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py tests/unit/test_quote_connection_pool.py
git commit -m "feat(shioaji): add Prometheus metrics for QuoteConnectionPool connections"
```

---

### Task 8: Full regression + lint + typecheck

- [ ] **Step 1: Run ruff lint**

Run: `uv run ruff check src/hft_platform/feed_adapter/shioaji/quote_connection_pool.py src/hft_platform/config/_symbols_parsing.py src/hft_platform/services/bootstrap.py src/hft_platform/feed_adapter/shioaji/client.py`
Expected: No errors (fix any that appear).

- [ ] **Step 2: Run full pool test suite**

Run: `uv run pytest tests/unit/test_quote_connection_pool.py tests/unit/test_symbols_parsing_group.py -v`
Expected: All tests PASS.

- [ ] **Step 3: Run broader regression**

Run: `uv run pytest tests/unit/ -x --timeout=60 -q 2>&1 | tail -20`
Expected: No new failures.

- [ ] **Step 4: Verify backward compat (HFT_QUOTE_CONNECTIONS unset)**

Run: `HFT_QUOTE_CONNECTIONS=1 uv run pytest tests/unit/test_bootstrap.py -v --timeout=30 2>&1 | tail -10`
Expected: Same as before — single connection path, all tests pass.

- [ ] **Step 5: Final commit if any lint/fix changes**

```bash
git add -u
git commit -m "chore: lint fixes for QuoteConnectionPool"
```
