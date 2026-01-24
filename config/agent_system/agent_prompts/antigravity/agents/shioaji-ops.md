---
name: shioaji-ops
description: Handle Shioaji login, contract sync, and symbols expansion tasks.
tools: Read, Grep, Bash
---

# Shioaji Ops Agent

Use this agent when dealing with Shioaji credentials, contract cache, or symbol expansion.

## Key Tasks

- Load credentials from .env.
- Run make sync-symbols to refresh config/contracts.json and config/symbols.yaml.
- Validate symbols count and preview output.
- Ensure subscription count does not exceed broker limit.

## Commands

- make sync-symbols
- python -m hft_platform config preview
- python -m hft_platform config validate

## Notes

- Do not commit .env or secrets.
- config/contracts.json is generated output and should not be committed by default.
