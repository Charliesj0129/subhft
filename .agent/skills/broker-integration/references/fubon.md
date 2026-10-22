# Fubon adapter notes

Read when touching `feed_adapter/fubon/`. SDK: `fubon-neo` (import `fubon_neo`),
not on PyPI; optional extra in `pyproject.toml`, guarded import
(`try: import fubon_neo except ImportError`). The facade is `FubonClientFacade`
(`facade.py`) over `session_runtime.py`, `quote_runtime.py`, `order_gateway.py`,
`account_gateway.py`, `contracts_runtime.py`, `subscription_manager.py`,
`order_codec.py`, `execution_callbacks.py`.

- Auth: API key plus password (`HFT_FUBON_API_KEY`, `HFT_FUBON_PASSWORD`);
  config in `config/base/brokers/fubon.yaml` (rate limits soft 100 / hard 150 per
  10 s; latency numbers there are estimates until measured).
- Callbacks run on SDK threads: enter the event loop only via `call_soon_threadsafe`.
- Prices arrive as strings or floats. Convert at the boundary with `Decimal`
  from the string (never `int(float(x) * 10000)`); outgoing price strings come
  from `Decimal(price) / Decimal(10000)`.
- Books are arrays of objects; flatten into preallocated arrays (depth up to 5).
- Subscription cooldown is longer than Shioaji's; check `subscription_manager.py`.
- SDK responses are wrapped; unwrap with the module's helpers before use.
- Certificate errors raise immediately (bad path or password); network errors
  retry with exponential backoff.
- Read the file before relying on any of this: field names and limits drift.
