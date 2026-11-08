#!/bin/sh
# Front-light control for the Paperwhite 3.
#
#   light.sh 12          set level 0-24
#   light.sh min|low|mid|high|max|restore
#   light.sh get         print the current level
#
# The stock UI resets the light to its floor whenever it restarts -- which is
# exactly what happens when you plug the cable into a computer, or when dash.sh
# stops lab126_gui to take the screen. This script is the way back, and it
# remembers the last value so restore can put it back after a restart.

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/light.log"
SAVE="$DIR/light.value"
MAX=24

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

get_lipc() { lipc-get-prop com.lab126.powerd flIntensity 2>/dev/null; }

case "$1" in
    get) get_lipc; echo; exit 0 ;;
    min)  V=1 ;;
    low)  V=5 ;;
    mid)  V=10 ;;
    high) V=16 ;;
    max)  V=24 ;;
    restore|"") if [ -f "$SAVE" ]; then V=$(cat "$SAVE" 2>/dev/null); else V=10; fi ;;
    *) V="$1" ;;
esac

case "$V" in ''|*[!0-9]*) V=10 ;; esac
[ "$V" -gt "$MAX" ] && V=$MAX

echo "$V" > "$SAVE"

# 1) powerd is the supported path.
if lipc-set-prop com.lab126.powerd flIntensity "$V" 2>/dev/null; then
    log "level=$V via lipc, now=$(get_lipc)"
else
    log "level=$V lipc failed, trying sysfs"
fi

# 2) Fallback: poke the backlight class directly (some firmwares ignore lipc
#    once powerd has decided the light is "off").
for f in /sys/class/backlight/*/brightness; do
    [ -w "$f" ] || continue
    m=$(cat "${f%/brightness}/max_brightness" 2>/dev/null)
    case "$m" in ''|*[!0-9]*) m=$MAX ;; esac
    val=$V
    [ "$m" -gt 0 ] && [ "$m" -ne "$MAX" ] && val=$(( V * m / MAX ))
    echo "$val" > "$f" 2>/dev/null && log "wrote $val to $f"
done

# 3) Make sure auto-brightness is not fighting us.
lipc-set-prop com.lab126.powerd flAuto 0 2>/dev/null

exit 0
