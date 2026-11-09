#!/bin/sh
set -eu

OPTIONS=/data/options.json
CONFIG_URL=$(jq -r '.config_url' "$OPTIONS")
RELAY_TOKEN=$(jq -r '.relay_token' "$OPTIONS")
CHECK_INTERVAL=$(jq -r '.check_interval // 300' "$OPTIONS")
FILES_TOKEN=$(jq -r '.files_token // empty' "$OPTIONS")
FILES_DOMAIN=$(jq -r '.files_domain // "files.virvinsky.ru"' "$OPTIONS")
FILES_PORT=8099

# check_interval — секунды между проверками agent.json.
# 0 — проверять только при старте. Минимум 30 с.
case "$CHECK_INTERVAL" in
  ''|*[!0-9]*) CHECK_INTERVAL=300 ;;
esac
if [ "$CHECK_INTERVAL" -ne 0 ] && [ "$CHECK_INTERVAL" -lt 30 ]; then
  CHECK_INTERVAL=30
fi

if [ -z "$RELAY_TOKEN" ] || [ "$RELAY_TOKEN" = "null" ]; then
  echo "ERROR: relay_token is empty. Set it in the add-on configuration."
  sleep 30
  exit 1
fi

echo "Check interval: ${CHECK_INTERVAL}s (0 = only at startup)"

ARCH=$(uname -m)
case "$ARCH" in
  aarch64|arm64) FRPC_URL="https://virvinsky.ru/ha/bin/frpc_linux_arm64" ;;
  armv7l|armv7|armhf) FRPC_URL="https://virvinsky.ru/ha/bin/frpc_linux_arm" ;;
  x86_64|amd64) FRPC_URL="https://virvinsky.ru/ha/bin/frpc_linux_amd64" ;;
  *) echo "Unsupported architecture: $ARCH"; exit 1 ;;
esac

download_frpc() {
  attempt=1
  while [ "$attempt" -le 10 ]; do
    if curl -4 -fsSL --max-time 300 "$FRPC_URL" -o /usr/local/bin/frpc.part; then
      chmod +x /usr/local/bin/frpc.part
      mv /usr/local/bin/frpc.part /usr/local/bin/frpc
      return 0
    fi
    delay=$((attempt * 5))
    echo "frpc download failed (attempt ${attempt}), retry in ${delay}s"
    sleep "$delay"
    attempt=$((attempt + 1))
  done
  return 1
}

if [ ! -x /usr/local/bin/frpc ]; then
  echo "Downloading frpc for $ARCH..."
  if ! download_frpc; then
    echo "ERROR: cannot download frpc, giving up"
    sleep 60
    exit 1
  fi
fi

# --- File service (web + WebDAV) -------------------------------------------
FILES_ENABLED=0
if [ -n "$FILES_TOKEN" ] && [ "$FILES_TOKEN" != "null" ]; then
  FILES_ENABLED=1
  export FILES_TOKEN FILES_DOMAIN FILES_PORT
  export FILES_ROOTS="/config:/share:/addons"
  (
    while true; do
      echo "Starting file service on 127.0.0.1:${FILES_PORT}"
      python3 /files_server.py || true
      echo "File service exited, restarting in 5s"
      sleep 5
    done
  ) &
fi

HOST=""
PORT=""

fetch_endpoint() {
  if ! curl -4 -fsSL --max-time 20 "$CONFIG_URL" -o /tmp/agent.json.tmp; then
    return 1
  fi
  HOST=$(jq -r '.relay.host // empty' /tmp/agent.json.tmp)
  PORT=$(jq -r '.relay.port // 7000' /tmp/agent.json.tmp)
  if [ -z "$HOST" ]; then
    return 1
  fi
  mv /tmp/agent.json.tmp /tmp/agent.json
}

write_config() {
  cat > /tmp/frpc.toml <<EOF
serverAddr = "$HOST"
serverPort = $PORT
auth.method = "token"
auth.token = "$RELAY_TOKEN"
log.to = "/dev/stdout"
log.level = "info"

[[proxies]]
name = "homeassistant"
type = "http"
customDomains = ["ha.virvinsky.ru"]
transport.useEncryption = true
transport.useCompression = true
localIP = "127.0.0.1"
localPort = 8123
EOF
  if [ "$FILES_ENABLED" = "1" ]; then
    cat >> /tmp/frpc.toml <<EOF

[[proxies]]
name = "ha-files"
type = "http"
customDomains = ["$FILES_DOMAIN"]
transport.useEncryption = true
transport.useCompression = true
localIP = "127.0.0.1"
localPort = $FILES_PORT
EOF
  fi
}

FETCH_BACKOFF=15
RESTART_BACKOFF=5

while true; do
  while ! fetch_endpoint; do
    echo "Cannot fetch $CONFIG_URL, retrying in ${FETCH_BACKOFF}s..."
    sleep "$FETCH_BACKOFF"
    FETCH_BACKOFF=$((FETCH_BACKOFF * 2))
    [ "$FETCH_BACKOFF" -le 300 ] || FETCH_BACKOFF=300
  done
  FETCH_BACKOFF=15

  echo "Using relay $HOST:$PORT"
  write_config

  START_TS=$(date +%s)
  frpc -c /tmp/frpc.toml &
  PID=$!

  if [ "$CHECK_INTERVAL" -eq 0 ]; then
    wait "$PID" 2>/dev/null || true
  else
    ELAPSED=0
    while kill -0 "$PID" 2>/dev/null; do
      sleep 1
      ELAPSED=$((ELAPSED + 1))
      if [ "$ELAPSED" -lt "$CHECK_INTERVAL" ]; then
        continue
      fi
      ELAPSED=0
      OLD_HOST=$HOST
      OLD_PORT=$PORT
      if fetch_endpoint; then
        if [ "$HOST" != "$OLD_HOST" ] || [ "$PORT" != "$OLD_PORT" ]; then
          echo "Endpoint changed: $OLD_HOST:$OLD_PORT -> $HOST:$PORT"
          kill "$PID" 2>/dev/null || true
          wait "$PID" 2>/dev/null || true
          break
        fi
      else
        echo "Endpoint check failed, keeping $OLD_HOST:$OLD_PORT"
      fi
    done
    wait "$PID" 2>/dev/null || true
  fi

  UPTIME=$(( $(date +%s) - START_TS ))
  if [ "$UPTIME" -ge 60 ]; then
    RESTART_BACKOFF=5
  fi
  echo "frpc stopped after ${UPTIME}s, restarting in ${RESTART_BACKOFF}s"
  sleep "$RESTART_BACKOFF"
  RESTART_BACKOFF=$((RESTART_BACKOFF * 2))
  [ "$RESTART_BACKOFF" -le 60 ] || RESTART_BACKOFF=60
done
