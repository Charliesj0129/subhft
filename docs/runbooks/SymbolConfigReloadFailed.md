# SymbolConfigReloadFailed

Alert `SymbolConfigReloadFailed` (`config/monitoring/alerts/rules.yaml`, severity critical):

```
increase(feed_symbol_config_reload_total{result!="ok"}[5m]) > 0   for 1m
```

`ShioajiClient._load_config()` (`feed_adapter/shioaji/client.py:917`) bumps the counter once per call, so the
`result` label says which branch it took. It runs at client start (`client.py:312`), when `symbols.yaml` changes
(`reload_symbols`, `contracts_runtime.py:758`), after a contract refresh (`contracts_runtime.py:1171`, `:1304`), and
on the quote pool's reload and universe-roll paths (`quote_connection_pool.py:1378`, `:1642`), so the alert can fire
from any of them.

## What each `result` means

| `result` | Branch | Load outcome | Log event (`docker logs hft-engine`) |
|---|---|---|---|
| `parse_error` | the YAML at the client's `config_path` could not be read or parsed | raises; the previous `symbols` stay in memory | `symbol_config_parse_error` (fields `path`, `error`) |
| `other` | any other exception while reading it | raises; the previous `symbols` stay | `symbol_config_load_other_error` |
| `exceeds_limit` | universe is larger than `MAX_SUBSCRIPTIONS_PER_CLIENT` (default 600, set by `HFT_MAX_SUBSCRIPTIONS`) | **default: truncates to the ceiling and continues**; raises `ValueError` only when `HFT_STRICT_SUBSCRIPTION_LIMIT=1` or `HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS=0` | `symbol_list_exceeds_limit_truncated` (warning) or `symbol_list_exceeds_limit` (error, strict) |
| `exceeds_pool_capacity` | universe is within the ceiling but larger than 120 x `HFT_QUOTE_CONNECTIONS` (code default 1 connection) | loads the full list; the subscribe step still truncates per connection | `symbol_list_exceeds_pool_capacity` (error) |

`exceeds_limit` counts in the truncating case too, so this alert can fire while the load itself succeeded and the
tail of the universe is silently not subscribed. Subscribe-time truncation is counted separately in
`feed_subscription_truncate_total`.

A Telegram notice (`notify_symbol_reload_failed`) goes out from the market-data service and the contract refresh when
the reload raised.

## Read-only checks

```bash
# Which result fired, and how often
curl -s http://localhost:9090/metrics | grep feed_symbol_config_reload_total

# The matching log lines
docker logs hft-engine --since 30m 2>&1 | grep -E 'symbol_config_|symbol_list_exceeds'

# Universe size in the repo copy (compare with the ceiling and with 120 x connections)
uv run python -c "import yaml; print(len(yaml.safe_load(open('config/symbols.yaml'))['symbols']))"
```

On the production host these are reads only. In pool mode `path` in the log is a per-connection shard under
`/tmp/hft_quote_pool_*/`, not `config/symbols.yaml`; see `SymbolsYamlRegeneration.md` for why.

## What to do

- `parse_error` / `other`: read `error` in the log line. A shard or `config/symbols.yaml` that is unreadable or not
  valid YAML needs the file fixed; regenerate it offline as in `SymbolsYamlRegeneration.md`.
- `exceeds_limit` / `exceeds_pool_capacity`: the universe is larger than the configured capacity. Either shrink the
  universe (offline, `make rebuild-symbols-yaml`) or raise `HFT_MAX_SUBSCRIPTIONS` / `HFT_QUOTE_CONNECTIONS`. Mind the
  broker's session and topic budget before adding connections (`feed_adapter/shioaji/limits.py`).
- Any change to the container's environment or to `config/symbols.yaml` on the host is a deploy, not a hot fix: follow
  `deployment.md` and `.agent/rules/41-deployment.md` (D1-D10, stop, wait 60 s, start). Do not edit the host from this
  page.

## Related

- `SymbolsYamlRegeneration.md` - offline regeneration after a contract roll
- `shioaji-contract-refresh-operations.md` - the hourly contract refresh
- `FeedSubscriptionPermanentlyFailed` (same rules file) - per-symbol subscribe failures after a roll
