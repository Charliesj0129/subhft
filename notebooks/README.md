# HFT Platform - Research Notebooks

This directory contains Jupyter notebooks for quantitative research and strategy development.

## Getting Started

1. Activate the virtual environment:
   ```bash
   source .venv/bin/activate
   ```

2. Start Jupyter Lab:
   ```bash
   make notebook
   # Or directly: jupyter lab --notebook-dir=notebooks
   ```

## Directory Structure

- `examples/` - Example notebooks for common workflows
- `research/` - Active research and experiments
- `reports/` - Generated analysis reports

## Data Access

Use the `hft_platform.research.data_loader` module to access historical data:

```python
from hft_platform.research import DataLoader

loader = DataLoader()
df = loader.load_market_data(symbol="2330", start="2024-01-01", end="2024-01-31")
```
