# utils — Shared Utilities

> **Package**: `src/hft_platform/utils/`
> **Files**: 2

## Overview

Shared utilities for structured logging and serialization.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `logging.py` | `setup_logging()` | structlog configuration |
| `serialization.py` | `orjson_dumps()`, `orjson_loads()` | JSON/orjson helpers |

## Logging

All platform code uses `structlog` (never `print()`):

```python
from structlog import get_logger
logger = get_logger("module_name")
logger.info("event", symbol="TXFD6", price=195000000)
```

## Serialization

Prefers `orjson` for performance, falls back to stdlib `json`:

```python
from hft_platform.utils.serialization import orjson_dumps, orjson_loads
data = orjson_loads(raw_bytes)
raw = orjson_dumps(data)
```
