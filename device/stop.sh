#!/bin/sh
# Stop the dashboard loop and bring the stock Kindle UI back.
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f /tmp/kindle-calendar.pid ]; then
    kill "$(cat /tmp/kindle-calendar.pid)" 2>/dev/null
    rm -f /tmp/kindle-calendar.pid
fi
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
start lab126_gui 2>/dev/null || start framework 2>/dev/null

# Restarting the framework drops the front light to its floor; put it back so
# handing the screen back does not leave it at minimum brightness.
sleep 3
[ -x "$DIR/light.sh" ] && "$DIR/light.sh" restore
exit 0
