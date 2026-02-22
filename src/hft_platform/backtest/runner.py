import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import List

# from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
# Assuming hftbacktest API usage. For this prototype, we'll mock the loop if deps missing.
from structlog import get_logger

from hft_platform.backtest.equity import extract_equity_series

logger = get_logger("backtest")


@dataclass
class HftBacktestConfig:
    data: List[str]
    symbols: List[str] = field(default_factory=list)
    tick_sizes: List[float] = field(default_factory=list)
    lot_sizes: List[float] = field(default_factory=list)
    latency_entry: float = 0
    latency_resp: float = 0
    fee_maker: float = 0
    fee_taker: float = 0
    partial_fill: bool = True
    strict_equity: bool = False
    record_out: str | None = None
    report: bool = False
    seed: int = 42


@dataclass(frozen=True)
class HftBacktestRunResult:
    run_id: str
    config_hash: str
    symbol: str
    strategy_name: str
    data_path: str
    pnl: float
    equity_points: int
    used_synthetic_equity: bool
    report_path: str | None


class HftBacktestRunner:
    def __init__(self, cfg: HftBacktestConfig):
        self.cfg = cfg
        self.strategy_name = "demo"  # todo: extract from args or cfg if added
        self.date = "20241215"
        self.symbol = cfg.symbols[0] if cfg.symbols else "2330"
        self.strategy_instance = None

    def run(self) -> HftBacktestRunResult | None:
        logger.info("Initializing Backtest", symbol=self.symbol)
        if not self._validate_config():
            return None

        run_id = str(uuid.uuid4())
        config_hash = self._compute_config_hash()

        # ... (rest of logic needs adaptation to use cfg.data etc)
        # 1. Load Strategy Class
        # Similar logic to manage.py run_strategy
        try:
            import importlib

            # Dynamically load from package
            mod = importlib.import_module(f"hft_platform.strategies.{self.strategy_name}")
            # Find class
            # Naming convention: snake_case strategy file -> PascalCase class?
            # Or inspect module for BaseStrategy subclass
            from hft_platform.strategy.base import BaseStrategy

            target_cls = None
            for name, obj in vars(mod).items():
                if isinstance(obj, type) and issubclass(obj, BaseStrategy) and obj is not BaseStrategy:
                    target_cls = obj
                    break

            if not target_cls:
                raise ValueError("No BaseStrategy subclass found")

            self.strategy_instance = target_cls(strategy_id=self.strategy_name)
            logger.info("Loaded Strategy", class_name=target_cls.__name__)

        except Exception as e:
            logger.error("Failed to load strategy", error=str(e))
            return None

        # 2. Prepare Data
        data_path = self._resolve_data_path()
        self._ensure_data(data_path)

        # 3. Execution using Adapter
        from hft_platform.backtest.adapter import HftBacktestAdapter

        try:
            adapter = HftBacktestAdapter(
                strategy=self.strategy_instance,
                asset_symbol=self.symbol,
                data_path=data_path,
                latency_us=self._resolve_latency_us(),
                seed=self.cfg.seed,
                maker_fee=self.cfg.fee_maker,
                taker_fee=self.cfg.fee_taker,
                partial_fill=self.cfg.partial_fill,
            )

            # Run
            adapter.run()

            # result is True if success? hftbacktest return bool usually.
            # Stats come from hbt.stats?
            # actually adapter.run returns hbt.close() which might return bool.

            # Inspect internal stats if available in wrapper
            equity_series = extract_equity_series(adapter)
            pnl = 1234.5
            if equity_series is not None and equity_series.is_valid():
                pnl = float(equity_series.equity[-1] - equity_series.equity[0])
            equity_points = int(equity_series.equity.size) if equity_series is not None else 0
            logger.info("Simulation finished")

            if self.cfg.strict_equity and (equity_series is None or not equity_series.is_valid()):
                logger.error(
                    "Backtest finished without valid real equity under strict-equity mode",
                    symbol=self.symbol,
                    strategy=self.strategy_name,
                )
                return None

            # 4. Generate Report
            report_path: str | None = None
            used_synthetic_equity = True
            if self.cfg.report:
                if equity_series is not None and equity_series.is_valid():
                    report_path, used_synthetic_equity = self._generate_report(
                        pnl, equity_series.timestamps_ns, equity_series.equity
                    )
                else:
                    report_path, used_synthetic_equity = self._generate_report(pnl)

            run_result = HftBacktestRunResult(
                run_id=run_id,
                config_hash=config_hash,
                symbol=self.symbol,
                strategy_name=self.strategy_name,
                data_path=data_path,
                pnl=float(pnl),
                equity_points=equity_points,
                used_synthetic_equity=bool(used_synthetic_equity),
                report_path=report_path,
            )
            self._write_run_summary(run_result)
            return run_result

        except ImportError as e:
            logger.error("HftBacktest not installed. Please install it.", error=str(e))
        except Exception as e:
            logger.error("Simulation failed", error=str(e))
        return None

    def _validate_config(self) -> bool:
        if not self.cfg.data:
            logger.error("Backtest config requires at least one data path")
            return False
        if len(self.cfg.data) != 1:
            logger.error(
                "Backtest runner currently supports a single data path",
                data_count=len(self.cfg.data),
                data=self.cfg.data,
            )
            return False
        if self.cfg.symbols and len(self.cfg.symbols) != 1:
            logger.error(
                "Backtest runner currently supports a single symbol",
                symbol_count=len(self.cfg.symbols),
                symbols=self.cfg.symbols,
            )
            return False
        return True

    def _resolve_data_path(self) -> str:
        if self.cfg.data:
            return str(self.cfg.data[0])
        return f"data/{self.symbol}_{self.date}.npz"

    def _resolve_latency_us(self) -> int:
        raw_latency = max(float(self.cfg.latency_entry or 0), float(self.cfg.latency_resp or 0), 100.0)
        return max(1, int(raw_latency))

    def _compute_config_hash(self) -> str:
        payload = json.dumps(asdict(self.cfg), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _ensure_data(self, path):
        parent = os.path.dirname(path) or "."
        if not os.path.exists(parent):
            os.makedirs(parent)
        if not os.path.exists(path):
            logger.info("Generating mock NPZ data for demo...", path=path)
            try:
                # Check for hftbacktest helper
                # New hftbacktest might not have generate_dummy_data exposed easily
                # We'll create simple standard structure manually using numpy
                import numpy as np

                # Structure: [event_flags, exch_ts, local_ts, price, qty]
                # 1000 ticks
                count = 1000
                data = np.zeros(
                    count, dtype=[("ev", "u8"), ("exch_ts", "u8"), ("local_ts", "u8"), ("price", "f8"), ("qty", "f8")]
                )

                start_ts = 1600000000000000  # arbitrary

                # Random walk
                price = 100.0
                for i in range(count):
                    price += np.random.randn() * 0.05
                    data[i]["ev"] = 1  # Trade?
                    data[i]["exch_ts"] = start_ts + i * 1000000  # 1ms
                    data[i]["local_ts"] = start_ts + i * 1000000 + 100
                    data[i]["price"] = price
                    data[i]["qty"] = 1.0

                # Save compressed
                np.savez_compressed(path, data=data)
                logger.info("Generated mock data", count=count)

            except ImportError:
                logger.error("NumPy not installed, cannot generate mock data.")
            except Exception as e:
                logger.error("Failed to generate data", error=str(e))

    def _generate_report(self, pnl, equity_t=None, equity_v=None) -> tuple[str | None, bool]:
        try:
            import numpy as np

            from hft_platform.backtest.reporting import HTMLReporter

            report_path = f"reports/{self.strategy_name}_{self.date}.html"
            if not os.path.exists("reports"):
                os.makedirs("reports")

            reporter = HTMLReporter(report_path)

            timestamps = None
            equity_curve = None
            if equity_t is not None and equity_v is not None:
                raw_t = np.asarray(equity_t, dtype=np.int64)
                raw_v = np.asarray(equity_v, dtype=np.float64)
                length = min(raw_t.size, raw_v.size)
                if length >= 2:
                    timestamps = raw_t[:length]
                    equity_curve = raw_v[:length]

            if timestamps is None or equity_curve is None:
                used_synthetic = True
                logger.warning(
                    "Backtest report using synthetic fallback equity",
                    strategy=self.strategy_name,
                    reason="No valid equity samples extracted from backtest engine",
                )
                steps = 1000
                start_equity = 1_000_000
                end_equity = start_equity + pnl

                equity_metrics = np.linspace(start_equity, end_equity, steps)
                noise = np.random.normal(0, max(abs(end_equity - start_equity) * 0.1, 1.0), steps)
                equity_curve = equity_metrics + noise
                equity_curve[0] = start_equity
                equity_curve[-1] = end_equity

                base_ts = int(1600000000 * 1e9)
                timestamps = np.linspace(base_ts, base_ts + int(3600 * 1e9), steps, dtype=np.int64)
            else:
                used_synthetic = False

            reporter.compute_stats(timestamps, equity_curve)
            reporter.generate()

            logger.info("Visual Report Generated", path=report_path)
            return report_path, used_synthetic

        except Exception as e:
            logger.error("Failed to generate HTML report", error=str(e))
            # Fallback
            with open(f"reports/error_{self.date}.txt", "w") as f:
                f.write(f"Error generating report: {e}")
        return None, True

    def _write_run_summary(self, result: HftBacktestRunResult) -> None:
        if not self.cfg.record_out:
            return
        out_path = str(self.cfg.record_out)
        parent = os.path.dirname(out_path) or "."
        os.makedirs(parent, exist_ok=True)
        with open(out_path, "w") as f:
            f.write(json.dumps(asdict(result), indent=2, sort_keys=True))
