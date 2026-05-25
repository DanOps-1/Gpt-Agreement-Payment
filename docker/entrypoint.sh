#!/bin/sh
# Container startup hook: bootstrap user config (if still empty after host bind mount,
# copy from .example), then exec docker CMD (uvicorn).

set -eu

bootstrap() {
    src="$1"
    dst="$2"
    if [ ! -f "$dst" ] && [ -f "$src" ]; then
        cp "$src" "$dst"
        echo "[entrypoint] bootstrap $dst from $(basename "$src")"
        echo "[entrypoint]   ↑ This is the template default value, please edit and restart the container (changes on host take effect via bind mount)"
    fi
}

bootstrap /app/CTF-pay/config.paypal.example.json     /app/CTF-pay/config.paypal.json
bootstrap /app/CTF-reg/config.paypal-proxy.example.json /app/CTF-reg/config.paypal-proxy.json

# Sentinel QuickJS has a hard dependency on node. Explicitly validate before startup here
# to avoid `[Errno 2] No such file or directory: 'node'` appearing during registration phase,
# or unexpected fallback to pure Python.
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

# Create webui runtime data directory (webui SQLite + wizard state + registration results)
mkdir -p /app/output

exec "$@"