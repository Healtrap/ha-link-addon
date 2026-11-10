#!/bin/sh
set -eu

OPTIONS=/data/options.json
if [ -f "$OPTIONS" ]; then
  CONTROL_TOKEN=$(python3 -c "import json;print(json.load(open('$OPTIONS')).get('control_token') or '')" 2>/dev/null || echo "")
  CONTROL_PORT_OPT=$(python3 -c "import json;print(json.load(open('$OPTIONS')).get('control_port') or 8099)" 2>/dev/null || echo 8099)
else
  CONTROL_TOKEN=""
  CONTROL_PORT_OPT=8099
fi

export CONTROL_TOKEN
export CONTROL_PORT="${CONTROL_PORT_OPT:-8099}"

if [ -n "$CONTROL_TOKEN" ]; then
  echo "HA Control: external token enabled, listening on ${CONTROL_PORT}"
else
  echo "HA Control: no external token (ingress only), listening on ${CONTROL_PORT}"
fi

exec python3 /control_server.py
