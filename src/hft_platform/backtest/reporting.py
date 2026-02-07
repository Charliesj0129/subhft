import json
from datetime import datetime
from typing import Any

import numpy as np
from structlog import get_logger

logger = get_logger("backtest.reporting")


class HTMLReporter:
    def __init__(self, output_path: str):
        self.output_path = output_path
        self.equity_curve: dict[str, list[Any]] = {"time": [], "value": []}
        self.trades: list[dict[str, Any]] = []
        self.metrics: dict[str, str] = {}

    def compute_stats(self, equity_t: np.ndarray, equity_v: np.ndarray) -> None:
        """Compute Sharpe, Drawdown, etc."""
        # Simple daily returns approx
        # For HFT, we might check minute-by-minute
        returns = np.diff(equity_v) / equity_v[:-1]
        returns = np.nan_to_num(returns)

        total_ret = (equity_v[-1] - equity_v[0]) / equity_v[0] if len(equity_v) > 0 else 0

        # Sharpe (simplified, annualized assuming 1 sec samples * 252*6.5*3600? No, this is raw)
        sharpe = np.mean(returns) / np.std(returns) * np.sqrt(len(returns)) if np.std(returns) > 0 else 0

        # Drawdown
        peak = np.maximum.accumulate(equity_v)
        dd = (equity_v - peak) / peak
        max_dd = np.min(dd)

        self.metrics = {
            "Total Return": f"{total_ret * 100:.2f}%",
            "Sharpe Ratio": f"{sharpe:.2f}",
            "Max Drawdown": f"{max_dd * 100:.2f}%",
            "Final Equity": f"{equity_v[-1]:.2f}",
            "Total Trades": f"{len(self.trades)}",
        }

        # Downsample for charting if too big (> 2000 points)
        step = max(1, len(equity_t) // 2000)
        self.equity_curve = {
            "time": [str(datetime.fromtimestamp(t / 1e9)) for t in equity_t[::step]],
            "value": equity_v[::step].tolist(),
        }

    def generate(self):
        template = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>HFT Backtest Report</title>
            <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
            <style>
                body {{ font-family: -apple-system, sans-serif; background: #f0f2f5; padding: 20px; }}
                .container {{ max_width: 1000px; margin: 0 auto; background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
                .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 20px; margin-bottom: 30px; }}
                .metric-card {{ background: #f8f9fa; padding: 15px; border-radius: 8px; text-align: center; }}
                .metric-val {{ font-size: 24px; font-weight: bold; color: #1a73e8; }}
                .metric-label {{ color: #666; font-size: 14px; margin-top: 5px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Strategy Performance Report 🚀</h1>
                <div class="metrics">
                    {"".join([f'<div class="metric-card"><div class="metric-val">{v}</div><div class="metric-label">{k}</div></div>' for k, v in self.metrics.items()])}
                </div>
                <canvas id="equityChart"></canvas>
            </div>
            <script>
                const ctx = document.getElementById('equityChart').getContext('2d');
                new Chart(ctx, {{
                    type: 'line',
                    data: {{
                        labels: {json.dumps(self.equity_curve["time"])},
                        datasets: [{{
                            label: 'Equity Curve',
                            data: {json.dumps(self.equity_curve["value"])},
                            borderColor: '#1a73e8',
                            backgroundColor: 'rgba(26, 115, 232, 0.1)',
                            fill: true,
                            tension: 0.1,
                            pointRadius: 0
                        }}]
                    }},
                    options: {{
                        responsive: true,
                        interaction: {{ intersect: false, mode: 'index' }},
                        scales: {{ x: {{ display: false }} }}
                    }}
                }});
            </script>
        </body>
        </html>
        """

        with open(self.output_path, "w") as f:
            f.write(template)
        logger.info("Report generated", path=self.output_path)
