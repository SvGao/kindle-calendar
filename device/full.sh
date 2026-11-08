#!/bin/sh
# Full-path test: stop the stock UI, clear the screen, show the test image with
# FBInk, and leave it on screen. Detaches itself first so that stopping
# framework does not kill this script along with KUAL.
#
# To get the stock UI back afterwards: reboot the Kindle (hold power ~20s),
# or run stop.sh from a shell.

DIR=/mnt/us/kindle-calendar
FB=/mnt/us/libkh/bin/fbink
LOG="$DIR/full.log"

if [ -z "$KC_DETACHED" ]; then
    export KC_DETACHED=1
    if command -v setsid >/dev/null 2>&1; then
        setsid sh "$0" >/dev/null 2>&1 &
    else
        nohup sh "$0" >/dev/null 2>&1 &
    fi
    exit 0
fi

exec > "$LOG" 2>&1

echo "=== full $(date) ==="
echo ""

echo "-- prevent screensaver --"
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>&1
echo "lipc_exit=$?"
echo ""

echo "-- stop stock UI --"
stop lab126_gui 2>&1
echo "stop_lab126_gui=$?"
stop framework 2>&1
echo "stop_framework=$?"
initctl stop webreader 2>&1
echo "stop_webreader=$?"
sleep 3

echo ""
echo "-- fb after UI stop --"
echo -n "virtual_size: "; cat /sys/class/graphics/fb0/virtual_size 2>&1
echo -n "bits_per_pixel: "; cat /sys/class/graphics/fb0/bits_per_pixel 2>&1
echo ""

echo "-- clear (full flash) --"
"$FB" -q -c -f 2>&1
echo "clear_exit=$?"
sleep 1

echo "-- show test-rot90 (REAGL) --"
"$FB" -q -g file="$DIR/test-rot90.png" -W REAGL 2>&1
echo "show_reagl_exit=$?"
sleep 2

echo "-- show test-rot90 (plain) --"
"$FB" -q -g file="$DIR/test-rot90.png" 2>&1
echo "show_plain_exit=$?"

echo ""
echo "=== full done ==="
