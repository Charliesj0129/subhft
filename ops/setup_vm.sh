#!/bin/bash
set -e
echo "Starting VM Setup..."

# Compose files selection (can be overridden by env)
COMPOSE_FILES=${COMPOSE_FILE:-docker-compose.yml}
if [ "${HFT_USE_STRESS:-0}" = "1" ]; then
    COMPOSE_FILES="$COMPOSE_FILES:docker-compose.stress.yml"
fi
if [ "${HFT_LOW_LATENCY:-0}" = "1" ]; then
    COMPOSE_FILES="$COMPOSE_FILES:docker-compose.lowlatency.yml"
fi
# If a data-root override exists, ensure path and add chdata override
if [ -z "${HFT_CH_DATA_ROOT:-}" ] && [ -d /mnt/data ]; then
    HFT_CH_DATA_ROOT="/mnt/data/clickhouse"
fi
if [ -n "${HFT_CH_DATA_ROOT:-}" ]; then
    sudo mkdir -p "$HFT_CH_DATA_ROOT"
    export CH_DATA_ROOT="$HFT_CH_DATA_ROOT"
    COMPOSE_FILES="$COMPOSE_FILES:docker-compose.chdata.yml"
fi
export COMPOSE_FILE=$COMPOSE_FILES
echo "Using COMPOSE_FILE=$COMPOSE_FILE"

# Install Docker
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    # Activate group changes for current script execution? 
    # Usually easier to just use sudo for docker commands in this script
else
    echo "Docker already installed."
fi

# Install Docker Compose Plugin if needed (get-docker.sh usually handles it)
if ! docker compose version &> /dev/null; then
  sudo apt-get update
  sudo apt-get install -y docker-compose-plugin
fi

# Create directories
mkdir -p ~/hft_platform/data ~/hft_platform/.wal ~/hft_platform/config

cd ~/hft_platform

# Check for .env
if [ ! -f .env ]; then
    echo "WARNING: .env file missing! Creating empty one if not exists."
    touch .env
fi

if [ "${HFT_REMOTE_IMAGES:-0}" = "1" ]; then
    echo "Pulling remote images..."
    sudo docker compose pull || true
else
    echo "Building images..."
    sudo docker compose build
fi

echo "Generating subscription config (requires valid .env)..."
# We run this to populate config/symbols.yaml
sudo docker compose run --rm hft-engine python scripts/subscribe_futures.py || echo "Subscription script failed (maybe bad keys?), continuing..."

echo "Starting services..."
sudo docker compose up -d

echo "Setup Complete! To follow logs: sudo docker compose logs -f"
