#!/bin/sh
# Install / remove the boot-time autostart job for the Kindle Calendar dashboard.
#
# Kindle boots with upstart; /etc/upstart lives on the read-only rootfs, so the
# job file is written with the rootfs temporarily remounted read-write and put
# back to read-only immediately after. Total write is ~300 bytes.
#
# The job is a "task" that starts AFTER the stock UI is up, so a failure here can
# never block the boot: it just means the library screen stays visible.
#
# Usage: autostart.sh install | remove | status

DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/autostart.log"
JOB=/etc/upstart/kindle-calendar.conf
ACTION="${1:-status}"

echo "=== $(date '+%F %T')  action=$ACTION ===" >> "$LOG"

case "$ACTION" in
    install)
        if ! command -v mntroot >/dev/null 2>&1; then
            echo "FAIL: mntroot not found, cannot touch rootfs" >> "$LOG"
            echo "=== aborted ===" >> "$LOG"
            exit 1
        fi

        mntroot rw >> "$LOG" 2>&1
        echo "mntroot_rw_exit=$?" >> "$LOG"

        cat > "$JOB" <<'EOF'
# Kindle Calendar dashboard: take over the screen shortly after boot.
# Managed by /mnt/us/kindle-calendar/autostart.sh -- do not edit by hand.
start on started lab126_gui
task
script
    # Let the stock UI finish, WiFi associate and /mnt/us mount.
    sleep 30
    if [ -f /mnt/us/kindle-calendar/start.sh ]; then
        /mnt/us/kindle-calendar/start.sh
    fi
end script
EOF
        echo "write_exit=$?" >> "$LOG"
        sync

        mntroot ro >> "$LOG" 2>&1
        echo "mntroot_ro_exit=$?" >> "$LOG"

        echo "-- installed job --" >> "$LOG"
        ls -l "$JOB" >> "$LOG" 2>&1
        sed -n '1,9p' "$JOB" >> "$LOG" 2>&1
        echo "-- rootfs mount now --" >> "$LOG"
        mount 2>/dev/null | grep ' / ' >> "$LOG"
        ;;

    remove)
        mntroot rw >> "$LOG" 2>&1
        echo "mntroot_rw_exit=$?" >> "$LOG"
        rm -f "$JOB"
        echo "rm_exit=$?" >> "$LOG"
        sync
        mntroot ro >> "$LOG" 2>&1
        echo "mntroot_ro_exit=$?" >> "$LOG"
        ls -l "$JOB" >> "$LOG" 2>&1
        ;;

    status)
        echo "-- job file --" >> "$LOG"
        ls -l "$JOB" >> "$LOG" 2>&1
        [ -f "$JOB" ] && sed -n '1,9p' "$JOB" >> "$LOG" 2>&1
        echo "-- initctl --" >> "$LOG"
        initctl status kindle-calendar >> "$LOG" 2>&1
        echo "-- rootfs mount --" >> "$LOG"
        mount 2>/dev/null | grep ' / ' >> "$LOG"
        ;;
esac

echo "=== done ===" >> "$LOG"
