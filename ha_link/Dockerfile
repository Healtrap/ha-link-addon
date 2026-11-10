ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

ARG FRP_VERSION=0.71.0
ARG TARGETARCH

RUN apk add --no-cache curl jq ca-certificates tzdata python3 py3-pip \
 && pip3 install --no-cache-dir --break-system-packages wsgidav cheroot

# frpc вшивается в образ на этапе сборки: при старте ничего не скачивается,
# поэтому обновление и перезапуск не зависят от доступности virvinsky.ru.
RUN set -eu; \
    case "${TARGETARCH:-$(uname -m)}" in \
      arm64|aarch64) frp_arch=arm64 ;; \
      amd64|x86_64) frp_arch=amd64 ;; \
      arm|armv7l|armhf) frp_arch=arm ;; \
      *) echo "unsupported arch: ${TARGETARCH:-$(uname -m)}"; exit 1 ;; \
    esac; \
    url="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${frp_arch}.tar.gz"; \
    echo "frpc: $url"; \
    curl -fsSL --retry 3 --retry-delay 2 --max-time 180 -o /tmp/frp.tar.gz "$url"; \
    tar -xzf /tmp/frp.tar.gz -C /tmp; \
    install -m 0755 "/tmp/frp_${FRP_VERSION}_linux_${frp_arch}/frpc" /usr/local/bin/frpc; \
    rm -rf /tmp/frp*; \
    /usr/local/bin/frpc -v

COPY run.sh /run.sh
COPY files_server.py /files_server.py
RUN chmod +x /run.sh

CMD ["/run.sh"]
