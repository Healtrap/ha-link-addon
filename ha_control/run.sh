#!/bin/sh
set -eu

OPTIONS=/data/options.json

read_opt() {
  python3 -c "import json;print(json.load(open('$OPTIONS')).get('$1') if json.load(open('$OPTIONS')).get('$1') is not None else '$2')" 2>/dev/null || echo "$2"
}

if [ -f "$OPTIONS" ]; then
  CONTROL_TOKEN=$(read_opt control_token "")
  SSH_ENABLED=$(read_opt ssh_enabled true)
  SELFHEAL_ENABLED=$(read_opt selfheal_enabled true)
else
  CONTROL_TOKEN=""
  SSH_ENABLED=true
  SELFHEAL_ENABLED=true
fi

# Порт фиксирован: на него смотрит ingress (ingress_port: 8099) и внешний
# прокси ha_link. Отдельная опция control_port была ошибкой 0.5.0 —
# при значении 8098 панель слушала бы не тот порт и ingress отдавал бы 502.
export CONTROL_PORT=8099
export CONTROL_TOKEN
export SELFHEAL_ENABLED

if [ -n "$CONTROL_TOKEN" ]; then
  echo "HA Control: external token enabled, listening on ${CONTROL_PORT}"
else
  echo "HA Control: no external token (ingress only), listening on ${CONTROL_PORT}"
fi

# --- панель -----------------------------------------------------------------
python3 /control_server.py &
PANEL_PID=$!

# --- SSH --------------------------------------------------------------------
case "$SSH_ENABLED" in
  false|False|0|"") SSH_ENABLED=0 ;;
  *) SSH_ENABLED=1 ;;
esac

if [ "$SSH_ENABLED" = "1" ]; then
  if /ssh_setup.sh; then
    if /usr/sbin/sshd -f /etc/ssh/sshd_config -e; then
      echo "HA Control: sshd listening on 0.0.0.0:22"
    else
      echo "HA Control: sshd failed to start (panel keeps running)"
    fi
  else
    echo "HA Control: ssh setup failed (panel keeps running)"
  fi
else
  echo "HA Control: SSH disabled in options"
fi

stop_all() {
  kill "$PANEL_PID" 2>/dev/null || true
  pkill -f '/usr/sbin/sshd' 2>/dev/null || true
  exit 0
}
trap stop_all TERM INT

# --- супервизор: панель и sshd перезапускаются независимо -------------------
while true; do
  sleep 10
  if ! kill -0 "$PANEL_PID" 2>/dev/null; then
    echo "HA Control: panel process died, restarting"
    python3 /control_server.py &
    PANEL_PID=$!
  fi
  if [ "$SSH_ENABLED" = "1" ] && ! pgrep -f '/usr/sbin/sshd' >/dev/null 2>&1; then
    echo "HA Control: sshd died, restarting"
    /usr/sbin/sshd -f /etc/ssh/sshd_config -e || true
  fi
done
