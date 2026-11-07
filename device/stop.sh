#!/bin/sh
# Stop the dashboard loop and bring the stock Kindle UI back.
if [ -f /tmp/kindle-calendar.pid ]; then
    kill "$(cat /tmp/kindle-calendar.pid)" 2>/dev/null
    rm -f /tmp/kindle-calendar.pid
fi
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null
start lab126_gui 2>/dev/null || start framework 2>/dev/null
