"""Entry point: ``python -m scripts.shioaji_api_diff <subcommand>``."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
