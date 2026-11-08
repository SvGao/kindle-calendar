#!/bin/sh
# KUAL has no console, so dump the light state into light.log instead.
# Read it back from the server with: curl http://<nas>:8091/api/log
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/light.log"
{
    echo "=== $(date '+%F %T') status"
    echo "flIntensity=$(lipc-get-prop com.lab126.powerd flIntensity 2>/dev/null)"
    echo "saved=$(cat "$DIR/light.value" 2>/dev/null)"
    ls /sys/class/backlight/ 2>/dev/null | while read -r d; do
        echo "backlight/$d brightness=$(cat "/sys/class/backlight/$d/brightness" 2>/dev/null) max=$(cat "/sys/class/backlight/$d/max_brightness" 2>/dev/null)"
    done
    echo "charging=$(lipc-get-prop com.lab126.powerd isCharging 2>/dev/null)"
} >> "$LOG"
# The running dashboard pushes light.log along with dash.log on its next tick,
# so there is nothing to upload here (/api/log would overwrite dash.log anyway).
exit 0
