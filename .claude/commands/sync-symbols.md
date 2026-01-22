---
description: Sync broker contracts and rebuild config/symbols.yaml from config/symbols.list.
---

# Sync Symbols

Use when symbols.list changes or when you need fresh contracts.

1) Load credentials from .env
```
set -a
. ./.env
set +a
```

2) Sync contracts and rebuild symbols
```
make sync-symbols
```

3) Verify counts
```
.venv/bin/python -m hft_platform config preview
.venv/bin/python -m hft_platform config validate
```

Notes:
- Requires SHIOAJI_API_KEY/SHIOAJI_SECRET_KEY or SHIOAJI_PERSON_ID/SHIOAJI_PASSWORD.
- Writes config/contracts.json and config/symbols.yaml.
