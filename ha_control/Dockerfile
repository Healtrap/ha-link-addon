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

CMD ["/run.sh"]
