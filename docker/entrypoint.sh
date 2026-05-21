#!/bin/sh
# 容器启动钩子：bootstrap 用户配置（host bind mount 后如果还是空的，从 .example 拷一份），
# 然后 exec docker CMD（uvicorn）。

set -eu

bootstrap() {
    src="$1"
    dst="$2"
    if [ ! -f "$dst" ] && [ -f "$src" ]; then
        cp "$src" "$dst"
        echo "[entrypoint] bootstrap $dst from $(basename "$src")"
        echo "[entrypoint]   ↑ 这是模板默认值，请编辑后重启容器（host 上同步生效，bind mount）"
    fi
}

bootstrap /app/CTF-pay/config.paypal.example.json     /app/CTF-pay/config.paypal.json
bootstrap /app/CTF-reg/config.paypal-proxy.example.json /app/CTF-reg/config.paypal-proxy.json

# Sentinel QuickJS 强依赖 node。这里启动前显式校验，避免运行到注册阶段才
# 出现 `[Errno 2] No such file or directory: 'node'`，或意外回退纯 Python。
if ! command -v node >/dev/null 2>&1; then
    if command -v nodejs >/dev/null 2>&1; then
        ln -sf "$(command -v nodejs)" /usr/local/bin/node
    else
        echo "[entrypoint] ERROR: node is required for OpenAI Sentinel QuickJS but was not found in PATH" >&2
        echo "[entrypoint]        Rebuild image: docker compose up -d --build --force-recreate -V webui" >&2
        exit 127
    fi
fi
export OPENAI_SENTINEL_NODE_PATH="${OPENAI_SENTINEL_NODE_PATH:-$(command -v node)}"
export NODE_PATH="${NODE_PATH:-/app/webui/frontend/node_modules:/usr/local/lib/node_modules}"
echo "[entrypoint] node: $("$OPENAI_SENTINEL_NODE_PATH" --version) ($OPENAI_SENTINEL_NODE_PATH)"
if [ -d /app/webui/frontend/node_modules ]; then
    "$OPENAI_SENTINEL_NODE_PATH" -e "require.resolve('happy-dom')" >/dev/null 2>&1 \
        && echo "[entrypoint] node deps: happy-dom ok" \
        || echo "[entrypoint] WARN: happy-dom not resolvable; rebuild image or run npm install in /app/webui/frontend" >&2
fi

# 创建 webui 运行时数据目录（webui SQLite + wizard state + 注册结果）
mkdir -p /app/output

exec "$@"
