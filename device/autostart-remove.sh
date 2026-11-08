#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
exec sh "$DIR/autostart.sh" remove
