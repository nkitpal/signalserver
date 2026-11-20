# syntax=docker/dockerfile:1

# --- Signal-Server engine --------------------------------------------------
FROM ubuntu:24.04 AS engine
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git cmake g++ make libbz2-dev zlib1g-dev libspdlog-dev \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/build-signalserver.sh /tmp/
ARG SIGNALSERVER_REF=7f6242a
# sed strips Windows line endings in case the checkout converted them.
RUN sed -i 's/\r$//' /tmp/build-signalserver.sh \
    && SIGNALSERVER_REF=${SIGNALSERVER_REF} bash /tmp/build-signalserver.sh /opt/signalserver

# --- Frontend --------------------------------------------------------------
FROM node:22-bookworm-slim AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- Runtime ---------------------------------------------------------------
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:/opt/signalserver/bin:$PATH \
    SSG_DATA_DIR=/data \
    SSG_SIGNALSERVER_PATH=/opt/signalserver/bin/signalserver \
    SSG_FRONTEND_DIST=/app/frontend/dist \
    SSG_HOST=0.0.0.0 \
    SSG_PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates python3 python3-venv libbz2-1.0 zlib1g libspdlog1.12 libfmt9 \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
WORKDIR /app
COPY pyproject.toml README.md ./
COPY signalserver_gui ./signalserver_gui
RUN pip install --no-cache-dir . && useradd --system --uid 10001 --home /app app \
    && mkdir -p /data && chown app /data
COPY --from=engine /opt/signalserver /opt/signalserver
COPY --from=frontend /src/dist /app/frontend/dist
USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1
CMD ["signalserver-gui", "serve"]
