#!/bin/sh
# Kindle side loop for an always-plugged-in Paperwhite 3.
# Fetch the server image, show it with FBInk, ask the server how long to sleep, repeat.
# Install to /mnt/us/kindle-calendar/ and start it from the KUAL menu.

DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$DIR/config.sh" ] && . "$DIR/config.sh"

SERVER="${SERVER:-http://192.168.1.10:8090}"
TOKEN="${TOKEN:-}"
FBINK="${FBINK:-/mnt/us/libkh/bin/fbink}"
FULL_REFRESH_EVERY="${FULL_REFRESH_EVERY:-24}"   # safety net: full refresh after N partial updates

IMG="$DIR/dash.png"
TMP="/tmp/kc-new.png"
HDR="/tmp/kc-headers"
ETAG_FILE="$DIR/dash.etag"
LOG="$DIR/dash.log"
partial_count=0
fail_count=0

q() { [ -n "$TOKEN" ] && echo "?token=$TOKEN"; }
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

init() {
    # Keep the stock UI and screensaver from drawing over us.
    lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
    stop lab126_gui 2>/dev/null || stop framework 2>/dev/null
    initctl stop webreader 2>/dev/null
    sleep 2
    "$FBINK" -q -c -f
    [ -f "$IMG" ] && "$FBINK" -q -g file="$IMG"
    : > "$LOG"
}

ensure_wifi() {
    if curl -fsS -m 10 "$SERVER/healthz" -o /dev/null; then
        return 0
    fi
    log "server unreachable, toggling wifi"
    lipc-set-prop com.lab126.cmd wirelessEnable 0
    sleep 5
    lipc-set-prop com.lab126.cmd wirelessEnable 1
    sleep 20
    curl -fsS -m 10 "$SERVER/healthz" -o /dev/null
}

report() {
    batt=$(lipc-get-prop com.lab126.powerd battLevel 2>/dev/null)
    chg=$(lipc-get-prop com.lab126.powerd isCharging 2>/dev/null)
    curl -fsS -m 10 -X POST "$SERVER/api/device$(q)" \
        -d "battery=${batt}&charging=${chg}" -o /dev/null
}

show() {  # $1 = full|partial
    if [ "$1" = full ] || [ "$partial_count" -ge "$FULL_REFRESH_EVERY" ]; then
        "$FBINK" -q -f -g file="$IMG"
        partial_count=0
    else
        "$FBINK" -q -W REAGL -g file="$IMG"
        partial_count=$((partial_count + 1))
    fi
}

offline_mark() {
    # Small marker in the bottom-left corner; keep the old picture otherwise.
    "$FBINK" -q -x 1 -y -1 "! offline"
}

fetch_and_show() {
    etag=$(cat "$ETAG_FILE" 2>/dev/null)
    code=$(curl -s -m 30 -D "$HDR" -o "$TMP" -w '%{http_code}' \
        -H "If-None-Match: $etag" "$SERVER/dash.png$(q)")
    full=part
    grep -qi '^x-full-refresh: *1' "$HDR" && full=full

    case "$code" in
        200)
            mv "$TMP" "$IMG"
            grep -i '^etag:' "$HDR" | sed 's/^[Ee][Tt][Aa][Gg]: *//' | tr -d '\r' > "$ETAG_FILE"
            show "$full"
            ;;
        304)
            [ "$full" = full ] && show full
            ;;
        *)
            log "fetch failed: http $code"
            return 1
            ;;
    esac
    return 0
}

main() {
    init
    while true; do
        if ensure_wifi && fetch_and_show; then
            fail_count=0
            report
            secs=$(curl -fsS -m 10 "$SERVER/api/next-wakeup$(q)")
            case "$secs" in ''|*[!0-9]*) secs=900 ;; esac
        else
            fail_count=$((fail_count + 1))
            offline_mark
            # Back off: 1, 2, 4 ... minutes, capped at 30 minutes.
            secs=60
            i=1
            while [ "$i" -lt "$fail_count" ] && [ "$secs" -lt 1800 ]; do
                secs=$((secs * 2))
                i=$((i + 1))
            done
            [ "$secs" -gt 1800 ] && secs=1800
        fi
        sleep "$secs"
    done
}

main
