---
description: Sync broker contracts and rebuild config/symbols.yaml from config/symbols.list.
---

# Sync Symbols

1) Load .env
```
set -a
. ./.env
set +a
```

2) Sync contracts and rebuild symbols
```
make sync-symbols
```

3) Validate
```
python -m hft_platform config preview
python -m hft_platform config validate
```
