# syntax=docker/dockerfile:1
#
# 多阶段构建：
#   1) frontend-builder: node 20 + npm 装依赖 + vue-tsc + vite build → dist/
#   2) runtime: ubuntu 24.04 + python3 + xvfb + GTK 系统库 + camoufox + playwright + gost
#
# 镜像里 cook 全部依赖 + 浏览器二进制 + 前端 dist；用户代码与配置由 docker-compose
# 通过 bind mount 注入（host 的 git working tree 当 SOURCE OF TRUTH，方便 git pull）。

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

# 系统依赖：python3 + python-is-python3 软链 + xvfb（虚拟 X server）
# + curl/ca-certificates（拉 gost）+ locale（playwright 启动会查）。
# Node 不从 Ubuntu apt 装：runtime 直接复用 frontend-builder 的官方 node:20，
# 避免 apt 源版本漂移 / nodejs 只提供 nodejs 不提供 node 的兼容坑。
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python-is-python3 python3-venv \
        xvfb \
        curl ca-certificates \
        locales tzdata \
        gosu \
    && locale-gen en_US.UTF-8 \
    && rm -rf /var/lib/apt/lists/*

# OpenAI Sentinel QuickJS 路径必须能在 runtime 里执行 `node`。
# 只需要 node + npm/npx shim；项目运行时不依赖 npm install，但保留 npm
# 便于容器内临时诊断/重建前端。
COPY --from=frontend-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend-builder /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version \
    && npm --version

WORKDIR /app

# Python 依赖：先 webui requirements（layer cache 友好），再 pipeline 用的
COPY webui/requirements.txt /tmp/webui-requirements.txt
RUN pip install -r /tmp/webui-requirements.txt \
        'qrcode[pil]' \
        requests curl_cffi 'playwright>=1.59,<2' 'camoufox[geoip]' \
        browserforge mitmproxy pybase64 \
    && rm /tmp/webui-requirements.txt

# 浏览器系统库：playwright 知道当前版本要的所有 deps（libgtk-3 / libdbus-glib / libasound2 …）
# Chromium 供 Node/Playwright PayPal RPA 使用；Firefox 供旧 sniff/bridge 路径使用。
RUN playwright install-deps chromium firefox

# 浏览器二进制：playwright 自身的 chromium（Node PayPal RPA 用）+ firefox（stripe_sniff_worker.py 用）
# + camoufox antidetect firefox（CTF-reg/browser_register.py + PayPal Camoufox 用）
RUN playwright install chromium firefox \
    && camoufox fetch

# gost socks5 中继 v3（webshare 走它，pipeline._ensure_gost_alive 自动拉起）。
# 用 linux_amd64.tar.gz（GOAMD64=v1）而非 amd64v3.tar.gz，保证老 CPU 也能跑。
ARG GOST_VERSION=3.2.6
RUN curl -fsSL "https://github.com/go-gost/gost/releases/download/v${GOST_VERSION}/gost_${GOST_VERSION}_linux_amd64.tar.gz" \
        | tar -xz -C /tmp/ \
    && mv /tmp/gost /usr/local/bin/gost \
    && chmod +x /usr/local/bin/gost \
    && /usr/local/bin/gost -V

# 项目代码（运行时 docker-compose 会用 bind mount 覆盖，方便 git pull 即时生效；
# 镜像 baked 一份是为了让 `docker run` 不挂载也能裸启）
COPY pipeline.py ./
COPY CTF-pay/ ./CTF-pay/
COPY CTF-reg/ ./CTF-reg/
COPY scripts/ ./scripts/
COPY webui/ ./webui/

# 前端 dist 来自 frontend-builder 阶段
COPY --from=frontend-builder /build/dist /app/webui/frontend/dist
# 运行时 Node 协议 helper（PayPal DataDome / hCaptcha 实验路径）会复用
# 前端构建阶段的纯 JS 依赖，例如 happy-dom。docker-compose 给
# /app/webui/frontend/node_modules 挂了匿名 volume；镜像里预置该目录后，
# 首次创建容器时 volume 会自动带上依赖，避免运行期 `require("happy-dom")`
# 失败。
COPY --from=frontend-builder /build/node_modules /app/webui/frontend/node_modules

# 启动脚本：bootstrap 用户配置（首次 host 上没 config.*.json 时从 .example 拷一份）
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8765

# 默认 webui bind 0.0.0.0:8765；外部 docker port mapping 控制是否暴露公网
ENV WEBUI_BIND_HOST=0.0.0.0 \
    WEBUI_BIND_PORT=8765

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "webui.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765"]
