#!/bin/sh
# One-shot probe: does FBInk exist, does it support PNG, what is the framebuffer?
# Does NOT stop the UI, so KUAL stays alive and you can read the output.
DIR=/mnt/us/kindle-calendar
FB=/mnt/us/libkh/bin/fbink
LOG="$DIR/probe.log"

{
    echo "=== probe $(date) ==="
    echo ""
    echo "-- fbink binary --"
    ls -l "$FB" 2>&1 || echo "MISSING: $FB"
    echo ""
    echo "-- fbink version --"
    "$FB" --version 2>&1 || "$FB" -V 2>&1
    echo ""
    echo "-- device info --"
    "$FB" -i 2>&1 | head -25
    echo ""
    echo "-- framebuffer --"
    echo -n "virtual_size: "; cat /sys/class/graphics/fb0/virtual_size 2>&1
    echo -n "bits_per_pixel: "; cat /sys/class/graphics/fb0/bits_per_pixel 2>&1
    echo -n "stride: "; cat /sys/class/graphics/fb0/stride 2>&1
    echo ""
    echo "-- kernel release --"
    cat /proc/sys/kernel/osrelease 2>&1
    echo ""
    echo "-- model --"
    grep -i "product" /proc/cpuinfo 2>&1 | head -3
    echo ""
    echo "-- show test png (no UI stop) --"
    "$FB" -q -g file="$DIR/test-rot90.png" 2>&1
    echo "png_exit=$?"
    echo ""
    echo "=== probe done ==="
} > "$LOG" 2>&1

# Echo to screen so KUAL displays it.
cat "$LOG"
