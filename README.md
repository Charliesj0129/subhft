# HFT Platform

High-Performance Event-Driven Trading Platform with Shioaji integration and HftBacktest support.

**What**: Event-driven HFT platform with market data, risk, execution, and recording services.
**How**: Use `uv` for dependency management and `make` for common workflows; configure credentials via `.env`.
**Status**: Alpha (active refactor + test coverage expansion).

## 🚀 Quick Start ( < 30 Minutes )

### Prerequisites
*   Python 3.10+
*   uv (recommended)
*   Make (optional)
*   Docker (optional, for ops)

### 1. Setup & Install
One command to sync dependencies and configure environment.
```bash
make dev
```
*(This command runs: `uv sync --dev`, and copies `.env.example` if missing.)*

Optional: install git hooks for Ruff auto-fixes:
```bash
make hooks
```

### 2. Run Simulation
Start the platform with mock data (no credentials required).
```bash
make run-sim
```
*   **Web Dashboard**: http://localhost:8080 (if enabled)
*   **Metrics**: http://localhost:9090

### 3. Run Strategy (Live/Mock)
Modify `src/hft_platform/strategies/simple_mm.py` or create your own:
```python
class MyStrategy(BaseStrategy):
    def on_book(self, ctx: StrategyContext, event: Union[BidAskEvent, TickEvent]):
        feats = ctx.get_features(event.symbol)
        if feats.get("spread", 0) > 5:
             # Logic here...
             pass
```

## 🏗 Architecture
*   **Services**: `MarketDataService`, `ExecutionService`, `SystemSupervisor`.
*   **Events**: Typed `TickEvent`, `BidAskEvent`, `OrderEvent` (Zero-copy slots).
*   **LOB**: Optimized (fast-path list based) with per-symbol locking.

## 🛠 Commands
| Command | Description |
| :--- | :--- |
| `make dev` | Sync environment (uv + .env) |
| `make hooks` | Install pre-commit hooks (Ruff auto-fix + format) |
| `make test` | Run unit tests |
| `make coverage` | Run coverage report |
| `make run-sim` | Run platform in Simulation mode |
| `make run-prod` | Run platform in Production mode (Requires `.env`) |

## 📦 Project Map
*   `src/hft_platform/`: src/ layout package root.
*   `src/hft_platform/services/`: Core micro-kernel services.
*   `src/hft_platform/strategies/`: Strategies (e.g. `simple_mm.py`).
*   `src/hft_platform/events.py`: Typed event definitions.
*   `config/`: Symbol and risk configurations.
*   `.env.example`: Environment variables template (credentials, modes).
*   `logs/`: Application logs.

## 🧪 Testing
We enforce high test coverage.
```bash
make coverage
```
**Current Baseline**: ~71%
**Target**: 95%

## 📚 Docs
*   `docs/quickstart.md` — 快速上手
*   `docs/feature_guide.md` — 功能手冊（各模組詳解）
*   `docs/deployment_guide.md` — 部署指南
*   `docs/ARCHITECTURE.md` — 系統架構
