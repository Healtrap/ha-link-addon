#!/bin/sh
# Готовит sshd: постоянные host-ключи в /data/ssh, authorized_keys из опций,
# пароль root (если задан). Запускается из run.sh на каждом старте контейнера.
set -eu

DATA=/data/ssh
OPTIONS=/data/options.json

mkdir -p "$DATA" /run/sshd /root
chmod 700 "$DATA"

# --- host keys (переживают перезапуск и обновление аддона) -------------------
for t in ed25519 rsa; do
  key="$DATA/ssh_host_${t}_key"
  if [ ! -f "$key" ]; then
    echo "ssh: generating $t host key"
    ssh-keygen -q -t "$t" -N "" -f "$key"
  fi
done
chmod 600 "$DATA"/ssh_host_*_key

# --- authorized_keys из опций ----------------------------------------------
python3 - <<'PY'
import json
import os

try:
    opt = json.load(open("/data/options.json", encoding="utf-8"))
except Exception:
    opt = {}

keys = opt.get("ssh_authorized_keys") or []
if isinstance(keys, str):
    keys = [keys]
lines = [k.strip() for k in keys if isinstance(k, str) and k.strip()]
path = "/data/ssh/authorized_keys"
with open(path, "w", encoding="utf-8") as fh:
    if lines:
        fh.write("\n".join(lines) + "\n")
os.chmod(path, 0o600)
print("ssh: authorized keys: %d" % len(lines))
PY

# --- пароль root (необязательно) -------------------------------------------
PASS_AUTH=no
PASSWORD=$(python3 -c "import json;print(json.load(open('$OPTIONS')).get('ssh_password') or '')" 2>/dev/null || echo "")
if [ -n "$PASSWORD" ]; then
  printf 'root:%s\n' "$PASSWORD" | chpasswd
  PASS_AUTH=yes
  echo "ssh: password authentication enabled"
else
  echo "ssh: password authentication disabled (keys only)"
fi

# --- конфиг ----------------------------------------------------------------
cat > /etc/ssh/sshd_config <<EOF
Port 22
AddressFamily any
HostKey $DATA/ssh_host_ed25519_key
HostKey $DATA/ssh_host_rsa_key

PermitRootLogin yes
PubkeyAuthentication yes
PasswordAuthentication $PASS_AUTH
PermitEmptyPasswords no
AuthorizedKeysFile $DATA/authorized_keys

UseDNS no
PrintMotd no
Subsystem sftp internal-sftp
AllowTcpForwarding yes
ClientAliveInterval 30
ClientAliveCountMax 3
EOF

# bash вместо ash — привычнее для ручной работы
if grep -q '^root:x:0:0:root:/root:/bin/ash$' /etc/passwd; then
  sed -i 's|^root:x:0:0:root:/root:/bin/ash$|root:x:0:0:root:/root:/bin/bash|' /etc/passwd
fi

# приветствие и подсказки
cat > /etc/profile.d/ha.sh <<'EOF'
export PS1='\[\e[36m\]ha-control\[\e[0m\]:\w# '
alias ll='ls -alF'
if command -v ha >/dev/null 2>&1; then
  echo "HA Control shell. Полезное: ha core info | ha apps | ha host reboot"
  echo "Файлы HA: /config  /share  /media  /backup"
fi
EOF

echo "ssh: configuration ready"
