---
skill: shioaji-contracts
version: 1
description: Use when working with Shioaji (永豐金) broker — session lifecycle, quote callbacks, contract resolution, or diagnosing connection issues. Shioaji is managed via ShioajiClientFacade and registered through the broker factory pattern.
runtime_plane: Market Data
hft_laws: [Async, Allocator, Precision]
---

# Skill: shioaji-contracts

## When to Use

Use this skill when:
- Diagnosing Shioaji login failures, session refresh issues, or reconnect loops.
- Working with quote schema validation, watchdog timeouts, or callback registration.
- Resolving contract/symbol lookup problems (missing contract, stale cache).
- Extending or debugging the factory registration path for Shioaji.
- Adding or modifying broker-protocol implementations under the multi-broker framework.

## Architecture Overview

`ShioajiClientFacade` composes sub-runtime modules under `feed_adapter/shioaji/`:

| Submodule | File | Responsibility |
|-----------|------|----------------|
| `SessionRuntime` | `feed_adapter/shioaji/session_runtime.py` | Login/retry, session refresh, reconnect |
| `QuoteRuntime` | `feed_adapter/shioaji/quote_runtime.py` | Quote schema validation, callbacks, watchdog |
| `ContractsRuntime` | `feed_adapter/shioaji/contracts_runtime.py` | Contract loading and caching |
| `ShioajiClientFacade` | `feed_adapter/shioaji/facade.py` | Composes all sub-runtimes into unified interface |
| `ShioajiBrokerFactory` | `feed_adapter/shioaji/factory.py` | `BrokerFactory` protocol impl, auto-registers |

`ShioajiClient` (`feed_adapter/shioaji_client.py`) remains the underlying state owner; the facade and runtime modules hold references to it and delegate state reads/writes through it to maintain a single source of truth.

## Multi-Broker Context

Shioaji is the default broker, selected via `HFT_BROKER=shioaji` (or unset).

- `ShioajiBrokerFactory` implements the `BrokerFactory` protocol from `feed_adapter/broker_registry.py`
- Auto-registers on import via `feed_adapter/shioaji/__init__.py`
- Factory handles Shioaji-specific config: `HFT_ORDER_MODE`, `HFT_ORDER_SIMULATION`, `HFT_ORDER_NO_CA`
- Crash detector (`detect_crash_signature` from `feed_adapter/shioaji/signatures.py`) is injected into `MarketDataService` by the factory
- All 4 protocols implemented: `MarketDataProvider`, `OrderExecutor`, `AccountProvider`, `BrokerSession`

## SessionRuntime — Key Methods

Located in `feed_adapter/shioaji/session_runtime.py`.

| Method | What it Does |
|--------|--------------|
| `login(*args, **kwargs)` | Public entrypoint; calls `login_with_retry` |
| `login_with_retry(api_key, secret_key, person_id, ca_passwd, contracts_cb)` | Full login sequence with retry, CA activation, contract fetch fallback |
| `start_session_refresh_thread()` | Starts background thread for periodic token refresh |
| `do_session_refresh()` | Executes one refresh cycle; called by the refresh thread |
| `request_reconnect(reason, force)` | Reconnect request respecting backoff/cooldown/lock; returns `False` if gated out (never raises) |
| `is_logged_in()` | Returns `bool` — reads `client.logged_in` |

Credentials are resolved from env vars in order: `SHIOAJI_API_KEY`, `SHIOAJI_SECRET_KEY`, `SHIOAJI_PERSON_ID`, `SHIOAJI_CA_PASSWORD` / `CA_PASSWORD`.

`SessionRuntime` implements `SessionPolicy` — quote-side code (watchdog, event handler) must talk to session-side code exclusively through this protocol, never importing `ShioajiClient` internals directly.

## QuoteRuntime — Key Methods

Located in `feed_adapter/shioaji/quote_runtime.py`.

| Method | What it Does |
|--------|--------------|
| `validate_quote_schema(*args, **kwargs)` | Schema guard for quote callbacks; returns `(bool, reason_str)` — `False` on v0-shape or malformed payload |
| `register_quote_callbacks()` | Registers tick/bidask callbacks with the Shioaji API object |
| `start_quote_watchdog()` | Starts watchdog thread monitoring for stale or missing quote events |
| `mark_pending(reason)` | Transitions quote feed to pending/degraded state; returns `QuotePendingState` delta |
| `clear_pending()` | Clears pending state; returns cleared `QuotePendingState` delta |
| `resubscribe()` | Requests re-subscription to all symbols after a session recovery |

`QuoteEventHandler` within `QuoteRuntime` owns the pending-state FSM. `ShioajiClient` applies returned `QuotePendingState` deltas atomically.

## ContractsRuntime — Key Methods

Located in `feed_adapter/shioaji/contracts_runtime.py`.

| Method | What it Does |
|--------|--------------|
| `reload_symbols()` | Clears and rebuilds the in-memory contract cache |
| `validate_symbols()` | Returns list of symbols that failed contract resolution |
| `refresh_status()` | Returns dict with refresh status, version counter, last diff, policy, cache/status paths, and thread liveness |

## Symbol Sync — Behaviour

- Contracts are fetched at login (`fetch_contract=True` by default).
- If contract fetch fails, the system falls back to `fetch_contract=False` and retries login (controlled by `HFT_LOGIN_FETCH_CONTRACT_FALLBACK`, default `"1"`).
- Symbol validation is gated before quote subscription — unknown symbols log a warning and are skipped, not raised.
- `ContractsRuntime.reload_symbols()` triggers a full re-fetch; call via `ShioajiClientFacade.reload_symbols()`.

## Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SHIOAJI_API_KEY` | — | Broker API key |
| `SHIOAJI_SECRET_KEY` | — | Broker secret key |
| `SHIOAJI_PERSON_ID` | — | National ID for CA activation |
| `SHIOAJI_CA_PASSWORD` / `CA_PASSWORD` | — | CA certificate password |
| `HFT_QUOTE_VERSION` | `auto` | Quote protocol version (`auto`, `v1`, `v2`) |
| `HFT_QUOTE_VERSION_STRICT` | `0` | `1` = reject mismatched quote schema |
| `HFT_ORDER_MODE` | `""` | Order routing mode (injected by factory) |
| `HFT_ORDER_SIMULATION` | — | Enables order simulation when set |
| `HFT_ORDER_NO_CA` | `0` | `1` = skip CA certificate requirement |
| `HFT_LOGIN_FETCH_CONTRACT_FALLBACK` | `1` | `0` = disable no-contract-fetch fallback |
| `HFT_BROKER` | `shioaji` | Selects broker via registry; `shioaji` is default |

## Common Failures

### Login hangs or times out
- Check `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY` are set.
- Increase `contracts_timeout` in `config/base/main.yaml` if contract fetch is slow.
- Set `HFT_LOGIN_FETCH_CONTRACT_FALLBACK=0` to disable the two-phase login if contracts cause hangs.
- Check `SessionRuntime.login_with_retry` logs for the `_last_login_error` field.

### Quote watchdog fires repeatedly
- Indicates stale or missing tick events from the broker feed.
- Check `QuoteRuntime.mark_pending` logs for the `reason` field.
- Verify `register_quote_callbacks` succeeded (look for `"callbacks registered"` log).
- In sim mode, the Shioaji SDK may silently drop subscriptions — call `resubscribe()` manually.

### Symbol not found / contract missing
- Run `facade.validate_symbols()` to list failing symbols.
- Call `facade.reload_symbols()` to force a refresh.
- Check `ContractsRuntime.refresh_status()` for cache age; stale cache (> 1h) is suspect.

### Crash signature detected
- `detect_crash_signature` in `feed_adapter/shioaji/signatures.py` matches known Shioaji SDK error strings.
- When a crash signature fires, `MarketDataService` triggers a reconnect via `SessionPolicy.request_reconnect`.
- Add new patterns to `signatures.py` (not to `shioaji_client.py`) to preserve separation.

### Session lock contention (`SHIOAJI_ACCOUNT` collision)
- The session lock key is derived from `SHIOAJI_ACCOUNT` or `SHIOAJI_PERSON_ID` or `SHIOAJI_API_KEY`.
- Multiple processes using the same credentials will contend on the same lock. Use separate credentials or set distinct `SHIOAJI_ACCOUNT`.

## HFT Law Checklist (for this skill)

- [ ] No `await` or blocking IO inside quote callbacks (Async Law — callbacks run in SDK thread)
- [ ] Quote payload must not be stored as `dict` on hot path — use pre-allocated slots (Allocator Law)
- [ ] Prices from `ContractsRuntime` are always scaled int (x10000) before crossing into platform code (Precision Law)
- [ ] `detect_crash_signature` must never raise — it is called in the SDK error callback path
- [ ] `SessionPolicy.request_reconnect` must never raise — it is called from the watchdog thread

## Cross-References

- Architecture diagram: `.agent/library/c4-model-current.md` (Market Data Plane)
- Runtime pipeline: `CLAUDE.md` → Runtime Pipeline section
- Broker protocol specs: `feed_adapter/broker_registry.py` (BrokerFactory, 4 runtime protocols)
- Latency baseline: `docs/architecture/latency-baseline-shioaji-sim-vs-system.md`
