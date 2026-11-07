#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f /tmp/kindle-calendar.pid ] && kill -0 "$(cat /tmp/kindle-calendar.pid)" 2>/dev/null; then
    exit 0
fi
nohup sh "$DIR/dash.sh" > /dev/null 2>&1 &
echo $! > /tmp/kindle-calendar.pid
