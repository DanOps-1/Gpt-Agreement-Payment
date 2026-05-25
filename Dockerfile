# syntax=docker/dockerfile:1
#
# Multi-stage build:
#   1) frontend-builder: node 20 + npm install dependencies + vue-tsc + vite build → dist/
#   2) runtime: ubuntu 24.04 + python3 + xvfb + GTK system libraries + camoufox + playwright + gost
#
# Image bakes all dependencies + browser binaries + frontend dist; user code and config injected
# by docker-compose via bind mount (host git working tree as SOURCE OF TRUTH for easy git pull).

# ─────────────── frontend-builder ───────────────
FROM node:20-bookworm-slim AS frontend-builder
WORKDIR /build
COPY webui/frontend/package.json webui/frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund --loglevel=error
COPY webui/frontend/ ./
RUN npm run build


# ─────────────── runtime ───────────────
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    TZ=UTC \
    NODE_PATH=/app/webui/frontend/node_modules:/usr/local/lib/node_modules

# System dependencies: python3 + python-is-python3 symlink + xvfb (virtual X server)
# + curl/ca-certificates (fetch gost) + locale (playwright startup checks).
# Node not installed from Ubuntu apt: runtime directly reuses official node:20 from frontend-builder,
# avoiding apt source version drift / nodejs-only compatibility quirks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python-is-python3 python3-venv \
        xvfb \
        curl ca-certificates \
        locales tzdata \
        gosu \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# OpenAI Sentinel QuickJS path must be able to execute `node` in runtime.
# Only need node + npm/npx shims; project runtime doesn't depend on npm install, but keep npm
# for convenience in-container diagnostics/frontend rebuild.
COPY --from=frontend-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend-builder /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version \
    && npm --version

WORKDIR /app

# Python dependencies: webui requirements first (layer cache friendly), then pipeline deps
COPY webui/requirements.txt /tmp/webui-requirements.txt
RUN pip install -r /tmp/webui-requirements.txt \
        'qrcode[pil]' \
        requests curl_cffi 'playwright>=1.59,<2' 'camoufox[geoip]' \
        browserforge mitmproxy pybase64 \
    && rm /tmp/webui-requirements.txt

# Browser system libraries: playwright knows all current version deps (libgtk-3 / libdbus-glib / libasound2 …)
# Chromium for Node/Playwright PayPal RPA; Firefox for legacy sniff/bridge paths.
RUN playwright install-deps chromium firefox

# Browser binaries: playwright's chromium (Node PayPal RPA) + firefox (stripe_sniff_worker.py)
# + camoufox antidetect firefox (CTF-reg/browser_register.py + PayPal Camoufox)
RUN playwright install chromium firefox \
    && camoufox fetch

# gost socks5 relay v3 (webshare uses it, pipeline._ensure_gost_alive auto-starts).
# Use linux_amd64.tar.gz (GOAMD64=v1) not amd64v3.tar.gz to support older CPUs.
ARG GOST_VERSION=3.2.6
RUN curl -fsSL "https://github.com/go-gost/gost/releases/download/v${GOST_VERSION}/gost_${GOST_VERSION}_linux_amd64.tar.gz" \
        | tar -xz -C /tmp/ \
    && mv /tmp/gost /usr/local/bin/gost \
    && chmod +x /usr/local/bin/gost \
    && /usr/local/bin/gost -V

# Project code (runtime docker-compose will override via bind mount for immediate git pull effect;
# baking in image allows bare `docker run` without mount to still work)
COPY pipeline.py ./
COPY CTF-pay/ ./CTF-pay/
COPY CTF-reg/ ./CTF-reg/
COPY scripts/ ./scripts/
COPY webui/ ./webui/

# Frontend dist from frontend-builder stage
COPY --from=frontend-builder /build/dist /app/webui/frontend/dist
# Runtime Node protocol helpers (PayPal DataDome / hCaptcha experimental paths) reuse
# pure JS dependencies from frontend build stage, e.g. happy-dom. docker-compose mounts
# /app/webui/frontend/node_modules as anonymous volume; image pre-bakes this directory so
# first container creation auto-includes dependencies, preventing runtime `require("happy-dom")`
# failure.
COPY --from=frontend-builder /build/node_modules /app/webui/frontend/node_modules

# Startup script: bootstrap user config (copy from .example on first run if host lacks config.*.json)
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8765

# Default webui binds 0.0.0.0:8765; external docker port mapping controls public exposure
ENV WEBUI_BIND_HOST=0.0.0.0 \
    WEBUI_BIND_PORT=8765

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "webui.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]