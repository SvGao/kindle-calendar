#!/bin/sh
# Kindle side loop for an always-plugged-in Paperwhite 3.
# Fetch the server image, show it with FBInk, ask the server how long to sleep, repeat.
# Install to /mnt/us/kindle-calendar/ and start it from the KUAL menu.

DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$DIR/config.sh" ] && . "$DIR/config.sh"

SERVER="${SERVER:-http://192.168.1.10:8091}"
TOKEN="${TOKEN:-}"
FBINK="${FBINK:-/mnt/us/libkh/bin/fbink}"
FULL_REFRESH_EVERY="${FULL_REFRESH_EVERY:-24}"   # safety net: full refresh after N partial updates

IMG="$DIR/dash.png"
TMP="/tmp/kc-new.png"
HDR="/tmp/kc-headers"
ETAG_FILE="$DIR/dash.etag"
LOG="$DIR/dash.log"
partial_count=$FULL_REFRESH_EVERY   # first paint after start must be a full refresh
fail_count=0

# --- per-minute clock ----------------------------------------------------------
# The poster is only rebuilt every 15 minutes, so the HH:MM digits baked into it
# would freeze in between.  /clock.png is exactly that patch -- rendered from the
# same page, so the glyphs match the poster pixel for pixel -- and we blit it into
# the rectangle the server reports.  Only that rectangle gets a partial refresh.
CLOCK="${CLOCK:-1}"
CLOCK_BOX="$DIR/clock.box"
CLOCK_ETAG="$DIR/clock.etag"
CLOCK_IMG="/tmp/kc-clock.png"
CLOCK_HDR="/tmp/kc-clock-hdr"
clock_x=""; clock_y=""; clock_w=""; clock_h=""; clock_min=""
online=1

# Clock pack: every HH:MM patch, downloaded once (a few MB) into $PACK_DIR.  With it
# the minute is drawn from local files -- no network per minute, and the clock
# keeps running while the NAS or the Wi-Fi is down.  /clock.png stays as the
# fallback while the pack is missing.
PACK_DIR="$DIR/clock"
PACK_KEY="$DIR/clock.key"
plan_pack=""
CLOCK_FLASH=1          # flash just the clock strip on the hour to clear ghosting
TZ_OFFSET=""           # seconds east of UTC, from the plan
srv_epoch=""; srv_utc=""
wake_now=0             # set by light_tick when the server has a new poster for us

# --- front light ---------------------------------------------------------------
# The slider that normally lives in the stock UI is gone while we own the screen,
# so the server keeps the level and we poll it.  Same once-a-minute cadence as the
# clock patch, which means we never wake the wifi just for the light.
LIGHT="${LIGHT:-1}"
LIGHT_FILE="$DIR/light.remote"
light_min=""; light_level=""

# --- policy from the server ----------------------------------------------------
# Everything that used to be a constant in this file -- clock on/off, the
# full-refresh interval, the failure backoff cap, how long to sleep and the light
# level -- arrives from /api/plan.  Rewriting any of that is a server-side change:
# the Kindle never needs a new copy of this script again.
PLAN_FILE="$DIR/plan.env"
PLAN=1
MAX_SLEEP=1800
plan_sleep=""

q() { [ -n "$TOKEN" ] && echo "?token=$TOKEN"; }
qa() {  # $1 = "fmt=text" style extra query
    if [ -n "$TOKEN" ]; then echo "?$1&token=$TOKEN"; else echo "?$1"; fi
}
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

# Local HH:MM as the server sees it.  The Kindle shell's timezone is not to be
# trusted once the stock UI is stopped, so use the plan's UTC offset when we have it.
now_hhmm() {
    if [ -n "$TZ_OFFSET" ]; then
        t=$(( $(date +%s) + TZ_OFFSET ))
        printf '%02d%02d' $(( (t / 3600) % 24 )) $(( (t / 60) % 60 ))
    else
        date +%H%M
    fi
}

# Keep the Kindle clock on the server's time.  The minute we draw from the pack
# comes from this clock, so a Kindle running 40 s fast would flip the digits 40 s
# early.  busybox date understands "@epoch" on most builds; fall back to a UTC
# timestamp string otherwise.
time_sync() {
    [ -n "$srv_epoch" ] || return 0
    dev=$(date +%s)
    diff=$(( dev - srv_epoch ))
    [ "$diff" -lt 0 ] && diff=$(( -diff ))
    [ "$diff" -le 2 ] && return 0
    if date -s "@$srv_epoch" >/dev/null 2>&1 ||        { [ -n "$srv_utc" ] && date -u -s "$(echo "$srv_utc" | tr '_' ' ')" >/dev/null 2>&1; }; then
        log "clock synced, was off by ${diff}s"
        clock_min=""
    else
        log "clock off by ${diff}s but date -s failed"
    fi
}

# Download the clock pack when the server has a different one than we do.
pack_sync() {
    [ "$CLOCK" = 1 ] || return 0
    case "$plan_pack" in ''|-) return 0 ;; esac
    [ "$plan_pack" = "$(cat "$PACK_KEY" 2>/dev/null)" ] && [ -f "$PACK_DIR/1200.png" ] && return 0
    log "fetching clock pack $plan_pack"
    rm -rf "$PACK_DIR.new" /tmp/kc-pack.tar
    if ! curl -fsS -m 300 -o /tmp/kc-pack.tar "$SERVER/clock-pack.tar$(q)"; then
        log "clock pack download failed"
        return 1
    fi
    mkdir -p "$PACK_DIR.new"
    if tar -xf /tmp/kc-pack.tar -C "$PACK_DIR.new" && [ -f "$PACK_DIR.new/2359.png" ]; then
        rm -rf "$PACK_DIR"
        mv "$PACK_DIR.new" "$PACK_DIR"
        echo "$plan_pack" > "$PACK_KEY"
        log "clock pack $plan_pack installed ($(ls "$PACK_DIR" | wc -l) files)"
    else
        log "clock pack extract failed"
        rm -rf "$PACK_DIR.new"
    fi
    rm -f /tmp/kc-pack.tar
}

# Ship the log to the server so debugging never needs a USB cable again.
push_log() {
    [ -f "$LOG" ] || return 0
    { tail -c 6000 "$LOG" 2>/dev/null
      [ -f "$DIR/light.log" ] && { echo "--- light"; tail -c 800 "$DIR/light.log"; }
    } | curl -fsS -m 15 -X POST \
        -H 'Content-Type: text/plain' --data-binary @- \
        "$SERVER/api/log$(q)" -o /dev/null 2>/dev/null
}

# A cable plugged into a computer makes the system restart the stock UI, which
# paints its "USB drive mode" screen over ours. Take the screen back.
#
# Detection has to be belt-and-braces: upstart's view and the actual process can
# disagree, and pidof is the cheapest reliable check. Either one saying "the UI
# is up" is enough to act.
ui_guard() {
    found=""
    if pidof cvm >/dev/null 2>&1; then
        found="cvm running"
    else
        st=$(initctl status lab126_gui 2>/dev/null)
        case "$st" in
            *start/running*) found="upstart: $st" ;;
        esac
    fi
    [ -n "$found" ] || return 0

    log "stock UI is back ($found), reclaiming screen"
    stop lab126_gui 2>/dev/null
    initctl stop webreader 2>/dev/null
    sleep 3
    # It may respawn once; stop again before painting.
    stop lab126_gui 2>/dev/null
    lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
    # The framework restart drops the front light to its floor; put it back.
    [ -x "$DIR/light.sh" ] && "$DIR/light.sh" restore
    [ -f "$IMG" ] && "$FBINK" -q -f -g file="$IMG"
    clock_min=""          # the poster we just painted carries a stale HH:MM
    push_log
}

# Read the patch rectangle from the server.  It lives in framebuffer coordinates,
# so it depends on the template and on ROTATE -- never hardcode it here.
clock_box_load() {
    [ "$CLOCK" = 1 ] || return 0
    b=$(curl -fsS -m 15 "$SERVER/api/clock-box$(qa fmt=text)" 2>/dev/null)
    if [ -n "$b" ]; then
        # shellcheck disable=SC2086
        set -- $b
        if [ -n "$4" ]; then
            # A rectangle at the origin means the answer was garbage -- blitting
            # there would slap a white patch over the top-left corner.
            bad=0
            case "$1" in ''|*[!0-9]*) bad=1 ;; esac
            case "$2" in ''|*[!0-9]*) bad=1 ;; esac
            if [ "$bad" = 0 ] && { [ "$1" -le 0 ] || [ "$2" -le 0 ]; }; then bad=1; fi
            if [ "$bad" = 1 ]; then
                log "clock box rejected: $b"
                return 1
            fi
            clock_x=$1; clock_y=$2; clock_w=$3; clock_h=$4
            echo "$b" > "$CLOCK_BOX"
            log "clock box $b"
            return 0
        fi
    fi
    log "clock box unavailable ($b), using cached"
    clock_box_restore
}

clock_box_restore() {
    [ -f "$CLOCK_BOX" ] || return 1
    # shellcheck disable=SC2046
    set -- $(cat "$CLOCK_BOX")
    [ -n "$4" ] || return 1
    clock_x=$1; clock_y=$2; clock_w=$3; clock_h=$4
    return 0
}

# Draw just the clock, at most once per minute.  Cheap enough to call from the
# sleep loop: when the minute has not changed it returns without touching the net.
clock_tick() {
    [ "$CLOCK" = 1 ] || return 0
    [ -n "$clock_x" ] || return 0
    m=$(now_hhmm)
    [ "$m" = "$clock_min" ] && return 0

    # Local pack first: no network, works offline.
    f="$PACK_DIR/$m.png"
    if [ -f "$f" ]; then
        wave="-W REAGL"
        # On the hour, one flashing refresh of just this strip keeps the digits crisp.
        case "$m" in *00) [ "$CLOCK_FLASH" = 1 ] && wave="-f" ;; esac
        # shellcheck disable=SC2086
        if "$FBINK" -q $wave -g "file=$f,x=$clock_x,y=$clock_y" 2>/dev/null; then
            clock_min=$m
            case "$m" in *0|*5) log "clock ok $m (pack)" ;; esac
        else
            log "clock blit failed at $m, disabled until next poster"
            clock_x=""
        fi
        return 0
    fi

    [ "$online" = 1 ] || return 0
    et=$(cat "$CLOCK_ETAG" 2>/dev/null)
    code=$(curl -s -m 15 -D "$CLOCK_HDR" -o "$CLOCK_IMG" -w '%{http_code}' \
        -H "If-None-Match: $et" "$SERVER/clock.png$(q)")
    case "$code" in
        200)
            grep -i '^etag:' "$CLOCK_HDR" | sed 's/^[Ee][Tt][Aa][Gg]: *//' | tr -d '\r' > "$CLOCK_ETAG"
            if "$FBINK" -q -W REAGL -g "file=$CLOCK_IMG,x=$clock_x,y=$clock_y" 2>/dev/null; then
                clock_min=$m
                # One line every five minutes is enough to prove the tick is alive
                # through /api/log without flooding the file overnight.
                case "$m" in
                    *0|*5) log "clock ok $m" ;;
                esac
            else
                # A failed blit must not be retried 60 times an hour: drop the
                # rectangle and wait for the next poster to bring a fresh one.
                log "clock blit failed at $m, disabled until next poster"
                clock_x=""
            fi
            ;;
        304)
            clock_min=$m
            ;;
        *)
            log "clock fetch http $code"
            ;;
    esac
}

# Pull the front-light level the phone page set.  Cheap: one small GET per minute,
# and it piggybacks on the same minute boundary the clock already waits for.
light_tick() {
    [ "$LIGHT" = 1 ] || return 0
    [ "$online" = 1 ] || return 0
    m=$(date +%H:%M)
    [ "$m" = "$light_min" ] && return 0
    light_min=$m

    cur=$(curl -fsS -m 8 "$SERVER/api/light$(qa fmt=text)" 2>/dev/null) || return 0
    # Expect "level applied poster full".
    set -- $cur
    case "$1" in ''|*[!0-9]*) return 0 ;; esac
    lv=$1
    # A new poster (console edit, "update now") or a requested full refresh: fetch
    # it right away instead of waiting for the next 15-minute slot.
    mine=$(cut -c2-9 "$ETAG_FILE" 2>/dev/null)
    if { [ -n "$3" ] && [ "$3" != 0 ] && [ "$3" != "$mine" ]; } || [ "$4" = 1 ]; then
        wake_now=1
    fi
    [ "$lv" = "$light_level" ] && return 0
    [ -x "$DIR/light.sh" ] || return 0

    "$DIR/light.sh" "$lv"
    # Read it back: lipc can clamp or silently ignore the value, and the phone page
    # should show what the screen is actually at, not what we asked for.
    now=$("$DIR/light.sh" get 2>/dev/null | tr -d '[:space:]')
    case "$now" in ''|*[!0-9]*) now=$lv ;; esac
    light_level=$now
    echo "$now" > "$LIGHT_FILE"
    log "light ask=$lv now=$now"
    curl -fsS -m 8 -X POST "$SERVER/api/light/applied$(q)" -d "level=$now" -o /dev/null 2>/dev/null
    return 0
}

# One GET gives us the whole policy.  Kept in plan.env so a network blip at boot
# still leaves us with the last known schedule instead of hardcoded defaults.
plan_load() {
    [ "$PLAN" = 1 ] || return 0
    body=$(curl -fsS -m 10 "$SERVER/api/plan$(q)" 2>/dev/null) || body=""
    if [ -z "$body" ]; then
        log "plan unavailable, reusing $PLAN_FILE"
        [ -f "$PLAN_FILE" ] && plan_apply < "$PLAN_FILE"
        plan_sleep=""          # that sleep was computed for an earlier minute
        return 1
    fi
    # Every line is assigned to a shell variable, so refuse anything that is not a
    # plain key=value pair rather than letting the shell chew on it.
    bad=$(printf '%s\n' "$body" | grep -vcE '^[a-z_]+=[A-Za-z0-9_:.,-]*$')
    if [ "$bad" != 0 ]; then
        log "plan rejected: $bad malformed line(s)"
        return 1
    fi
    printf '%s\n' "$body" > "$PLAN_FILE.new" && mv "$PLAN_FILE.new" "$PLAN_FILE"
    plan_apply < "$PLAN_FILE"
    return 0
}

plan_apply() {
    plan_sleep=""; srv_epoch=""; srv_utc=""
    while IFS='=' read -r k v; do
        case "$k" in
            clock) CLOCK=$v ;;
            light) LIGHT=$v ;;
            full_refresh_every)
                case "$v" in ''|*[!0-9]*) ;; *) [ "$v" -ge 1 ] && FULL_REFRESH_EVERY=$v ;; esac ;;
            max_sleep_s)
                case "$v" in ''|*[!0-9]*) ;; *) [ "$v" -ge 60 ] && MAX_SLEEP=$v ;; esac ;;
            sleep_s)
                case "$v" in ''|*[!0-9]*) ;; *) [ "$v" -ge 30 ] && plan_sleep=$v ;; esac ;;
            clock_pack) plan_pack=$v ;;
            clock_flash) CLOCK_FLASH=$v ;;
            tz_offset_s)
                case "$v" in ''|*[!0-9-]*) ;; *) TZ_OFFSET=$v ;; esac ;;
            epoch)
                case "$v" in ''|*[!0-9]*) ;; *) srv_epoch=$v ;; esac ;;
            utc) srv_utc=$v ;;
        esac
    done
}

wait_network() {
    i=0
    while [ "$i" -lt 10 ]; do
        curl -fsS -m 4 "$SERVER/healthz" -o /dev/null && return 0
        i=$((i + 1))
        sleep 3
    done
    return 1
}

init() {
    : > "$LOG"
    # Keep the stock UI and screensaver from drawing over us.
    lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null
    # After a reboot (or a USB plug) the light is at its minimum; restore ours.
    [ -x "$DIR/light.sh" ] && "$DIR/light.sh" restore
    # At boot the WiFi may still be associating. The com.lab126.cmd service that
    # can toggle it dies together with the stock UI, so wait BEFORE stopping it.
    wait_network || log "network not ready at start, continuing anyway"
    stop lab126_gui 2>/dev/null || stop framework 2>/dev/null
    initctl stop webreader 2>/dev/null
    sleep 2
    "$FBINK" -q -c -f
    [ -f "$IMG" ] && "$FBINK" -q -f -g file="$IMG"
    partial_count=0      # the startup paint above already was a full refresh
    clock_min=""         # the poster on disk may predate this minute
    clock_box_restore
    log "started, server=$SERVER"
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

show() {  # $1 = full|partial ; sets $show_mode to what was actually done
    if [ "$1" = full ] || [ "$partial_count" -ge "$FULL_REFRESH_EVERY" ]; then
        "$FBINK" -q -f -g file="$IMG"
        partial_count=0
        show_mode=full
    else
        "$FBINK" -q -W REAGL -g file="$IMG"
        partial_count=$((partial_count + 1))
        show_mode=part
    fi
    # Whatever we just painted was rendered before now; let the next tick redraw
    # the digits so the clock never lags the poster by a full minute.
    clock_min=""
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
            log "http 200 new image, refresh=$show_mode"
            # The layout may have changed with the new poster; re-read the patch
            # rectangle before the next minute fires.
            clock_box_load
            online=1
            ;;
        304)
            if [ "$full" = full ]; then
                show full
                log "http 304 unchanged, but forced full refresh"
            else
                log "http 304 unchanged, no repaint"
            fi
            online=1
            ;;
        *)
            log "fetch failed: http $code"
            online=0
            return 1
            ;;
    esac
    return 0
}

main() {
    echo $$ > /tmp/kindle-calendar.pid
    init
    while true; do
        ok=0
        if ensure_wifi; then
            plan_load && time_sync
            fetch_and_show && ok=1
            [ "$ok" = 1 ] && pack_sync
        fi
        if [ "$ok" = 1 ]; then
            fail_count=0
            report
            secs=$plan_sleep
            case "$secs" in
                ''|*[!0-9]*)
                    # No plan this round: fall back to the old dedicated endpoint.
                    secs=$(curl -fsS -m 10 "$SERVER/api/next-wakeup$(q)")
                    case "$secs" in ''|*[!0-9]*) secs=900 ;; esac ;;
            esac
        else
            fail_count=$((fail_count + 1))
            offline_mark
            # Back off: 1, 2, 4 ... minutes, capped by the server's max_sleep_s.
            secs=60
            i=1
            while [ "$i" -lt "$fail_count" ] && [ "$secs" -lt "$MAX_SLEEP" ]; do
                secs=$((secs * 2))
                i=$((i + 1))
            done
            [ "$secs" -gt "$MAX_SLEEP" ] && secs=$MAX_SLEEP
        fi
        log "sleep ${secs}s"
        push_log
        # Sleep in short chunks so a resurrected stock UI is reclaimed quickly, and
        # land exactly on the minute boundary so the clock tick is never late.
        wake_now=0
        while [ "$secs" -gt 0 ] && [ "$wake_now" = 0 ]; do
            step=$secs
            [ "$step" -gt 15 ] && step=15
            ss=$(date +%S); ss=${ss#0}
            [ -z "$ss" ] && ss=0
            to_min=$((60 - ss))
            [ "$step" -gt "$to_min" ] && step=$to_min
            sleep "$step"
            secs=$((secs - step))
            ui_guard
            clock_tick
            light_tick
        done
        [ "$wake_now" = 1 ] && log "server has a new poster, fetching now"
    done
}

main
