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
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy

class MyStrategy(BaseStrategy):
    def on_stats(self, event: LOBStatsEvent) -> None:
        if event.spread > 5:
            self.buy(event.symbol, event.best_bid, 1)
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
See `docs/project_layout.md` for the full layout. Key paths:
*   `src/hft_platform/`: Core package (services, strategy, risk, execution, recorder).
*   `config/`: Base configs and env overrides.
*   `docs/`: Documentation (start at `docs/README.md`).
*   `tests/`: Test suites and fixtures.
*   `examples/` and `notebooks/`: Samples and research.
*   `ops/` and `scripts/`: Deployment and tooling.

## 🧪 Testing
We enforce high test coverage.
```bash
make coverage
```
**Current Baseline**: ~71%
**Target**: 95%

## 📚 Docs
*   `docs/README.md` — 文件入口與閱讀順序
*   `docs/project_layout.md` — 專案結構與擴充點
*   `docs/quickstart.md` — 快速上手
*   `docs/feature_guide.md` — 功能手冊（各模組詳解）
*   `docs/strategy-guide.md` — 策略開發指南
*   `docs/config_reference.md` — 設定參考
*   `docs/cli_reference.md` — CLI 使用說明
*   `docs/troubleshooting.md` — 常見問題排查
*   `docs/deployment_guide.md` — 部署指南
*   `docs/ARCHITECTURE.md` — 系統架構
