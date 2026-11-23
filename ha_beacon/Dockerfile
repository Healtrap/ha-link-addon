ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM}

RUN apk add --no-cache tzdata bash curl

COPY beacon.py /beacon.py
COPY run.sh /run.sh
RUN chmod +x /run.sh

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD curl -fsS http://127.0.0.1:8097/health || exit 1

CMD ["/run.sh"]
