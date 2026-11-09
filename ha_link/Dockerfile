ARG BUILD_FROM=alpine:3.20
FROM ${BUILD_FROM}

RUN apk add --no-cache curl jq ca-certificates tzdata python3 py3-pip \
 && pip3 install --no-cache-dir --break-system-packages wsgidav cheroot

COPY run.sh /run.sh
COPY files_server.py /files_server.py
RUN chmod +x /run.sh

CMD ["/run.sh"]
