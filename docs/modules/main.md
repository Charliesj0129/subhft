# main / entrypoints

## Purpose
Entry points for running the system as a Python module.

## Key Files
- `src/hft_platform/__main__.py`: Redirects to CLI.
- `src/hft_platform/main.py`: Async launcher for `HFTSystem` (legacy or direct run).

## Notes
- Primary entry is CLI: `python -m hft_platform run sim`.
- `main.py` provides a direct asyncio runner around `HFTSystem`.
