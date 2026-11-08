#!/bin/sh
# Launch the dashboard loop detached from KUAL.
# dash.sh stops the stock UI, which kills KUAL and its whole process group, so
# the loop must live in its own session (setsid) or it dies together with the menu.
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE=/tmp/kindle-calendar.pid

# Also called from the boot-time upstart job, where /mnt/us may not be ready yet.
[ -f "$DIR/dash.sh" ] || exit 1

# Escape hatch: with autostart enabled the dashboard takes the screen back on
# every boot, and KUAL is unreachable while it runs. Drop an empty file named
# "no-autostart" next to this script (over USB) and the next boot stays on the
# stock UI. Delete the file to re-arm autostart.
[ -f "$DIR/no-autostart" ] && exit 0

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    exit 0
fi

if command -v setsid >/dev/null 2>&1; then
    setsid sh "$DIR/dash.sh" >/dev/null 2>&1 &
else
    nohup sh "$DIR/dash.sh" >/dev/null 2>&1 &
fi
exit 0
