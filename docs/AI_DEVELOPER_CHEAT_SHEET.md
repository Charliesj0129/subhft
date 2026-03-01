# AI & Developer Cheat Sheet (Token Optimizer)

> **Purpose**: This file is a highly compressed "How-To" reference designed specifically for AI agents and developers. Read this file to understand HOW to perform common tasks in the HFT Platform without exhaustively searching the `docs/` or `.agent/` directories.

## 🚀 1. How to Add a New Strategy / Protocol

1. **Location**: `src/hft_platform/strategies/`
2. **Template**: Inherit from `BaseStrategy` (`src/hft_platform/strategy/base.py`).
3. **Core Methods**:
   - `handle_event(self, event: Event) -> list[OrderIntent]`: Where the logic lives.
   - Access LOB state via `self.context.get_l1_scaled(symbol)`.
   - Submit orders via `self.context.place_order(symbol, side, price_scaled, qty)`.
4. **Mandatory Rule**: Do **not** use `float` for prices. All prices must be `int` scaled by `x10000` (`price_scaled`). Use `PriceCodec.scale()` if converting.

## 🗄️ 2. How to Modify the Database Schema

We use an automated Migration Runner. **Do not** edit random SQL files!

1. **Location**: `src/hft_platform/migrations/clickhouse/`
2. **Format**: Create a new file starting with the date and sequence, e.g., `20260305_002_add_account_id.sql`.
3. **Syntax**:
   ```sql
   -- Up
   ALTER TABLE hft.orders ADD COLUMN account_id String;
   -- Down
   ALTER TABLE hft.orders DROP COLUMN account_id;
   ```
4. **Execution**: The system executes this automatically at runtime bootstrap via `src/hft_platform/recorder/schema.py`.

## 🧬 3. How to Develop an Alpha Feature (Research)

The platform strictly separates Alpha research from live deployment via Gates (A-E).

1. **Scaffold**: Run `make research-scaffold ALPHA=your_alpha_name` (or `python -m research scaffold <name>`).
2. **Implementation**: Edit `research/alphas/<name>/alpha.py`.
3. **Logic Rule**: Extract your DataFrame/Polars compute logic clearly, avoiding blocking I/O.
4. **Promote**: Run `uv run hft alpha validate <name>` to pass Gate B/C. Once the backtest scorecard is generated, use `uv run hft alpha promote <name>` to push it to the live Canary config.

## 🛡️ 4. How to Add a Risk Gate (Pre-Trade)

1. **Location**: `src/hft_platform/risk/validators.py`
2. **Implementation**: Create a class inheriting a base validator interface. It must return a boolean (True=Pass, False=Reject) given an `OrderIntent`.
3. **Registration**: Add it to `RiskEngine` inside `src/hft_platform/risk/engine.py`.
4. **Performance**: Must execute in < 10 microseconds. Avoid remote DB lookups.

## 🏃 5. How to Run & Test

Here are the terminal commands you'll use 95% of the time:

- **Unit/Integration Testing**: `uv run pytest` or `make test`
- **Lint & Format**: `uv run ruff check src/ tests/ --fix` (Always run this before committing)
- **Run the Engine (Sim Mode)**: `uv run hft run sim`
- **Check DB Status**: `uv run hft recorder status`
- **Compile Rust Extension**: `uv run maturin develop --manifest-path rust_core/Cargo.toml`

## 🔎 6. The Core Hotpath (For Debugging)

If an order isn't flowing, check the sequence:
`ShioajiClient (callback)` → `MarketDataService` → `LOBEngine (Feature Update)` → `EventBus` → `StrategyRunner` → `RiskEngine` → `OrderAdapter` (or `GatewayService` if enabled) → `Broker API`.

_For more detailed architecture, see `docs/ARCHITECTURE.md` or `CLAUDE.md`._

## 🔌 7. MCP Servers & Hooks

The repository comes pre-wired with MCP servers (`clickhouse`, `arxiv`, `git`, `github`, `docker`, etc.) and process lifecycle hooks.
These are automatically picked up by your IDE or AI Host because we already generated `.cursor/mcp.json`, `.cline/mcp_settings.json`, and `claude.json` in the project root.

- **Do not** manually redefine these servers.
- **Do not** complain about missing `.agent/mcp/mcp-servers.json` since they are already merged into the root dot-files.
