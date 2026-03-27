#!/usr/bin/env bash
# scripts/smart_check.sh — Parse SMART attributes and emit Prometheus textfile
# Cron: 0 5 * * 1 (weekly, Monday 05:00)
# Requires: sudo apt install smartmontools
set -euo pipefail

DEVICE="${1:-/dev/sda}"
TEXTFILE_DIR="${TEXTFILE_DIR:-/var/lib/node-exporter/textfile}"
OUTPUT="${TEXTFILE_DIR}/smartmon.prom"

if ! command -v smartctl &>/dev/null; then
    echo "smartctl not found. Install: sudo apt install smartmontools" >&2
    exit 1
fi

SMART_OUTPUT=$(sudo smartctl -A "$DEVICE" 2>/dev/null || true)

parse_attr() {
    local attr_name="$1"
    echo "$SMART_OUTPUT" | awk -v name="$attr_name" '$2 == name {print $10}' | head -1
}

REALLOCATED=$(parse_attr "Reallocated_Sector_Ct")
WEAR_LEVEL=$(parse_attr "Wear_Leveling_Count")
POWER_ON_HOURS=$(parse_attr "Power_On_Hours")
TEMP=$(parse_attr "Temperature_Celsius")

if [ -z "$REALLOCATED" ]; then
    REALLOCATED=$(parse_attr "Reallocated_Sector_Count")
fi
if [ -z "$WEAR_LEVEL" ]; then
    WEAR_LEVEL=$(parse_attr "Media_Wearout_Indicator")
fi

cat > "$OUTPUT" <<METRICS
# HELP smartmon_reallocated_sectors Number of reallocated sectors
# TYPE smartmon_reallocated_sectors gauge
smartmon_reallocated_sectors{device="$DEVICE"} ${REALLOCATED:-0}
# HELP smartmon_wear_leveling Wear leveling count (lower = more worn)
# TYPE smartmon_wear_leveling gauge
smartmon_wear_leveling{device="$DEVICE"} ${WEAR_LEVEL:-0}
# HELP smartmon_power_on_hours Total power-on hours
# TYPE smartmon_power_on_hours gauge
smartmon_power_on_hours{device="$DEVICE"} ${POWER_ON_HOURS:-0}
# HELP smartmon_temperature_celsius Drive temperature
# TYPE smartmon_temperature_celsius gauge
smartmon_temperature_celsius{device="$DEVICE"} ${TEMP:-0}
METRICS

echo "[$(date -Iseconds)] SMART metrics written to $OUTPUT"
