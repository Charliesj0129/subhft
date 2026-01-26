#!/bin/bash
set -e

# ==============================================================================
# Unified Ops Script for HFT Platform
# Usage: sudo ./ops.sh [COMMAND]
# Commands:
#   setup       : Initialize directories, install Docker, and start services (Dev).
#   tune        : Apply Low-Latency host tuning (Sysctl, CPU Governor).
#   hugepages   : Enable Hugepages (Numba Optimization).
#   isolate     : Isolate CPUs for Strategy (Soft-Realtime).
#   install-rt  : Install Real-Time Kernel (Producton/Bare Metal).
#   start-ptp   : Start PTP Time Sync (Production/Bare Metal).
#   monitor-ch  : Check ClickHouse data flow stats.
#   replay-wal  : Move archived WAL files back to active folder for re-ingestion.
#   test        : Run system tests via pytest.
#   clean       : Remove temporary ops artifacts.
# ==============================================================================

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo ./ops.sh ...)"
  exit 1
fi

PROJECT_ROOT=$(pwd)

# --- Function: Setup (Dev/VM) ---
cmd_setup() {
    echo ">> [Setup] Initializing HFT Platform..."
    
    # 1. Install Docker
    if ! command -v docker &> /dev/null; then
        echo "Installing Docker..."
        curl -fsSL https://get.docker.com | sh
        usermod -aG docker $SUDO_USER || true
    fi
    
    # 2. Config Directories
    mkdir -p .wal/archive config
    mkdir -p data/clickhouse/hot data/clickhouse/cold
    chown -R 101:101 data/clickhouse || true
    if [ -n "${SUDO_USER:-}" ]; then
        chown -R "$SUDO_USER:$SUDO_USER" .wal || true
    fi

    # 3. Tuning (Optional)
    if [ "${HFT_SKIP_TUNING:-0}" != "1" ]; then
        cmd_tune
        cmd_hugepages
    fi

    # 4. Docker Compose
    echo ">> [Setup] Starting Services..."
    # Determine Compose Files via Unified Config
    COMPOSE="docker-compose.yml"
    
    # Data Root Override
    if [ -n "${HFT_CH_DATA_ROOT:-}" ]; then
        export CH_DATA_HOT="$HFT_CH_DATA_ROOT/hot"
        export CH_DATA_COLD="$HFT_CH_DATA_ROOT/cold"
    fi
    if [ -n "${HFT_CH_DATA_HOT:-}" ]; then
        export CH_DATA_HOT="$HFT_CH_DATA_HOT"
    fi
    if [ -n "${HFT_CH_DATA_COLD:-}" ]; then
        export CH_DATA_COLD="$HFT_CH_DATA_COLD"
    fi
    
    # Run
    docker compose -f $COMPOSE up -d --build
    echo ">> [Setup] Done. Services are running."
}

# --- Function: Host Tuning ---
cmd_tune() {
    echo ">> [Tune] Applying Sysctl & CPU Settings..."
    
    # Sysctl Config
    cat <<EOF > /etc/sysctl.d/99-hft.conf
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.core.netdev_max_backlog = 250000
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_low_latency = 1
net.core.somaxconn = 4096
EOF
    sysctl --system > /dev/null

    # CPU Governor
    if command -v cpupower &> /dev/null; then
        cpupower frequency-set -g performance > /dev/null || true
    else
        echo "   (Skipping cpupower: not installed)"
    fi

    # IRQ Balance
    systemctl stop irqbalance 2>/dev/null || true
}

# --- Function: Hugepages ---
cmd_hugepages() {
    NR_PAGES=${1:-1024}
    echo ">> [Hugepages] Allocating $NR_PAGES pages..."
    echo $NR_PAGES > /proc/sys/vm/nr_hugepages
    
    if ! mount | grep -q "/dev/hugepages"; then
        mount -t hugetlbfs none /dev/hugepages
    fi
}

# --- Function: CPU Isolation ---
cmd_isolate() {
    CMD=$1
    TOTAL=$(nproc)
    if [ "$TOTAL" -lt 4 ]; then
        echo ">> [Isolate] Warning: Need 4+ cores. Aborting."
        return
    fi
    
    SPLIT=$((TOTAL / 2))
    HFT_CORES="$SPLIT-$((TOTAL - 1))"
    
    echo ">> [Isolate] Launching on Cores $HFT_CORES with RT Priority..."
    if [ -n "$CMD" ]; then
        taskset -c $HFT_CORES chrt -f 50 $CMD
    else
        echo "   Usage: sudo ./ops.sh isolate '<command>'"
    fi
}

# --- Function: Install RT Kernel (Prod) ---
cmd_install_rt() {
    echo ">> [Prod] Installing Real-Time Kernel..."
    source /etc/os-release
    if [[ "$ID" == "debian" ]]; then
        apt-get update && apt-get install -y linux-image-rt-amd64 linux-headers-rt-amd64
    elif [[ "$ID" == "ubuntu" ]]; then
        apt-get install -y linux-image-realtime linux-headers-realtime
    else
        echo "   Unsupported OS: $ID"
        exit 1
    fi
    
    echo ">> [Prod] Updating GRUB..."
    ISOL="2-$(($(nproc)-1))"
    PARAMS="isolcpus=$ISOL nohz_full=$ISOL rcu_nocbs=$ISOL skew_tick=1"
    sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="/GRUB_CMDLINE_LINUX_DEFAULT="'"$PARAMS"' /' /etc/default/grub
    update-grub
    echo "   Done. Please reboot."
}

# --- Function: Start PTP (Prod) ---
cmd_start_ptp() {
    IFACE=${1:-eth0}
    echo ">> [Prod] Starting PTP on $IFACE..."
    
    # Generate PTP Config
    cat <<EOF > /tmp/ptp4l.conf
[global]
time_stamping hardware
network_transport L2
delay_mechanism E2E
pi_proportional_const 0.0
pi_integral_const 0.0
step_threshold 0.00002
EOF

    # Start Services
    ptp4l -i $IFACE -f /tmp/ptp4l.conf -s -m &
    PTP_PID=$!
    phc2sys -s $IFACE -c CLOCK_REALTIME -w -m &
    PHC_PID=$!
    
    echo "   PTP Running (PIDs: $PTP_PID, $PHC_PID). Ctrl+C to stop."
    wait $PTP_PID $PHC_PID
}

# --- Function: Monitor ClickHouse ---
cmd_monitor_ch() {
    CH_CONTAINER="clickhouse"
    CH_CLIENT="docker exec ${CH_CONTAINER} clickhouse-client --query"
    echo ">> [Monitor] ClickHouse Stats:"
    $CH_CLIENT "SELECT count(), min(toDateTime64(exch_ts/1e9,3)), max(toDateTime64(exch_ts/1e9,3)) FROM hft.market_data"
}

# --- Function: Replay WAL ---
cmd_replay_wal() {
    ARCHIVE_DIR=".wal/archive"
    WAL_DIR=".wal"
    echo ">> [Replay] Moving archived WALs..."
    docker compose stop wal-loader || true
    mkdir -p "$WAL_DIR"
    find "$ARCHIVE_DIR" -type f -name "*.jsonl" -exec mv {} "$WAL_DIR"/ \;
    docker compose start wal-loader
    echo "   Done."
}

# --- Function: Test ---
cmd_test() {
    echo ">> [Test] Running pytest (requires venv)..."
    if [ -f .venv/bin/activate ]; then
        source .venv/bin/activate
        PYTHONPATH=src pytest tests/
    else
        echo "Error: .venv not found. Please setup python environment first."
    fi
}


# --- Main Dispatch ---
case "$1" in
    setup)
        cmd_setup
        ;;
    tune)
        cmd_tune
        ;;
    hugepages)
        cmd_hugepages "$2"
        ;;
    isolate)
        shift
        cmd_isolate "$@"
        ;;
    install-rt)
        cmd_install_rt
        ;;
    start-ptp)
        cmd_start_ptp "$2"
        ;;
    monitor-ch)
        cmd_monitor_ch
        ;;
    replay-wal)
        cmd_replay_wal
        ;;
    test)
        cmd_test
        ;;
    *)
        echo "Usage: $0 {setup|tune|hugepages|isolate|install-rt|start-ptp|monitor-ch|replay-wal|test}"
        exit 1
        ;;
esac
