# Multi-Broker Setup Workflow

## Prerequisites
- Platform installed (`uv sync`)
- Broker SDK installed (`pip install shioaji` or `pip install fubon-neo`)
- Broker credentials obtained and set as env vars

## Step 1: Choose Broker
Set `HFT_BROKER` environment variable:
```bash
export HFT_BROKER=shioaji  # or fubon
```

## Step 2: Configure Credentials

### Shioaji (永豐金)
```bash
export SHIOAJI_API_KEY=your_api_key
export SHIOAJI_SECRET_KEY=your_secret_key
# For live mode:
export HFT_ACTIVATE_CA=1
```

### Fubon (富邦)
```bash
export HFT_FUBON_API_KEY=your_api_key
export HFT_FUBON_PASSWORD=your_password
```

## Step 3: Verify Config
```bash
ls config/base/brokers/${HFT_BROKER}.yaml  # Config exists
uv run python -c "from hft_platform.broker.config import load_broker_config; print(load_broker_config('${HFT_BROKER}'))"
```

## Step 4: Start Platform
```bash
uv run hft run sim  # Simulation mode first
```

## Switching Brokers
1. Stop the running engine (`Ctrl+C` or `hft stop`)
2. Change `HFT_BROKER` env var
3. Update credential env vars
4. Verify config (Step 3)
5. Restart platform

## Troubleshooting
- **SDK not installed**: `ModuleNotFoundError` → install the correct SDK
- **Missing credentials**: Platform refuses to start with clear error
- **Rate limit exceeded**: Check `config/base/brokers/<broker>.yaml` rate_limits
- **Price precision mismatch**: Verify all prices are scaled int x10000
