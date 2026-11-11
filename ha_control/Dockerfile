ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

# openssh-* — SSH-сервер вместо стороннего Advanced SSH.
# docker-cli — чтобы `ha` внутри контейнера работал через hassio_cli.
RUN apk add --no-cache \
      python3 tzdata bash curl jq nano \
      openssh-server openssh-client openssh-keygen \
      docker-cli

COPY control_server.py /control_server.py
COPY ssh_setup.sh /ssh_setup.sh
COPY run.sh /run.sh
COPY bin/ha /usr/local/bin/ha
RUN chmod +x /run.sh /ssh_setup.sh /usr/local/bin/ha

# Сборочный smoke-тест: sshd должен принимать наш конфиг, а нужные утилиты —
# существовать. Если пакеты или пути поменяются, сборка упадёт здесь, а не на
# малине. Отдельного бинарника sftp-server в Alpine нет и он не нужен:
# в конфиге используется `Subsystem sftp internal-sftp` (встроен в sshd).
RUN set -eu; \
    mkdir -p /run/sshd; \
    ssh-keygen -q -t ed25519 -N "" -f /tmp/smoke_key; \
    printf 'Port 22\nHostKey /tmp/smoke_key\nSubsystem sftp internal-sftp\n' > /tmp/smoke.conf; \
    /usr/sbin/sshd -t -f /tmp/smoke.conf; \
    test -x /usr/bin/docker; \
    test -x /usr/bin/ssh-keygen; \
    command -v chpasswd >/dev/null; \
    command -v pgrep >/dev/null; \
    command -v pkill >/dev/null; \
    rm -rf /tmp/smoke_key /tmp/smoke_key.pub /tmp/smoke.conf; \
    echo "smoke: sshd ok"

CMD ["/run.sh"]
