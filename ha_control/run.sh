#!/bin/sh
set -eu

echo "HA Control starting (port ${CONTROL_PORT:-8099})"
exec python3 /control_server.py
