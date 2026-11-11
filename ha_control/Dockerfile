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

# Сборочный smoke-тест: sshd должен запускаться и принимать конфиг.
# Если пакеты или пути поменяются — сборка упадёт здесь, а не на малине.
RUN set -eu; \
    mkdir -p /run/sshd; \
    ssh-keygen -q -t ed25519 -N "" -f /tmp/smoke_key; \
    printf 'Port 22\nHostKey /tmp/smoke_key\n' > /tmp/smoke.conf; \
    /usr/sbin/sshd -t -f /tmp/smoke.conf; \
    test -x /usr/lib/ssh/sftp-server; \
    test -x /usr/bin/docker; \
    rm -rf /tmp/smoke_key /tmp/smoke_key.pub /tmp/smoke.conf; \
    echo "smoke: sshd ok"

CMD ["/run.sh"]
