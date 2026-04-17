# Module: main

## Purpose

Process entry point for the HFT runtime. Wires the service graph, installs
signal handlers, and starts the event loop.

## Contents

- `main.py` — boot orchestration.
- `__main__.py` — enables `python -m hft_platform`.

## Used By

- CLI: `hft run {sim|live|replay}` → `cli.py` → `main.py`.
- Direct invocation: `python -m hft_platform run sim`.

## Flow

1. Load config via `config/loader.py`.
2. Build `HFTSystem` via `services/bootstrap.py`.
3. Run `HFTSystem.run()` under `uvloop`.
4. On shutdown signal, drain queues and close broker sessions.

## Notes

Never hold blocking work here. All orchestration lives in `services/`.
