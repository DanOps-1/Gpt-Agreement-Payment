# webui — Configuration Wizard + Preflight Checks

Compresses the 1-3 hour first-time `pipeline.py` setup into ~15 minutes.

## Quick Start

```bash
# Backend dependencies
pip install -r webui/requirements.txt

# Frontend build (once)
cd webui/frontend && pnpm i && pnpm build && cd ../..

# Start
python -m webui.server
# Open http://127.0.0.1:8765 in browser
```

First visit jumps to `/setup` to create admin account.

## 14-Step Process

See `docs/superpowers/specs/2026-04-28-webui-design.md`.

| Phase | Steps |
|-------|-------|
| 1 Basic (5) | Mode selection / System deps / Cloudflare / IMAP / Proxy |
| 2 Payment (2) | PayPal / Card + Billing |
| 3 CAPTCHA (2, optional) | CAPTCHA service / VLM endpoint |
| 4 Downstream (4) | Team plan / gpt-team / CPA / Daemon / Stripe runtime |
| 5 Complete (1) | Review + Export |

Each step's right panel `PreflightPanel` shows real-time passed checks.

## Reverse Proxy (Public Access)

webui binds to `127.0.0.1` by default. For remote access, nginx reverse proxy:

```nginx
location /webui/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
}
```

## Development

```bash
# Backend dev mode (auto-reload)
uvicorn webui.server:create_app --factory --reload --host 127.0.0.1 --port 8765

# Frontend dev mode (Vite proxy /api → 8765)
cd webui/frontend && pnpm dev
# Open http://127.0.0.1:5173

# Run tests
python -m pytest webui/tests/ -v       # Backend 47 tests
cd webui/frontend && pnpm test         # Frontend Vitest
```

## Architecture

- Backend: FastAPI + SQLite (users + sessions) + JSON (wizard state) + bcrypt + sse-starlette
- Frontend: Vue 3 + Vite + TypeScript + Naive UI + Pinia + Vue Router
- Auth: cookie session (httponly + SameSite=Lax)
- Startup: single process `python -m webui.server`, FastAPI serves API + static frontend
