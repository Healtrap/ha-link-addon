#!/bin/sh
set -eu

echo "HA Beacon: starting"

python3 /beacon.py &
PID=$!

stop_all() {
  kill "$PID" 2>/dev/null || true
  exit 0
}
trap stop_all TERM INT

while true; do
  sleep 10
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "HA Beacon: publisher died, restarting"
    python3 /beacon.py &
    PID=$!
  fi
done
