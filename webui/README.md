# webui — Configuration Wizard + Preflight Health Check

Compress the 1-3 hours configuration process to run `pipeline.py` successfully down to ~15 minutes.

## Quick Start```bash
# Backend dependencies
pip install -r webui/requirements.txt

# Frontend build (one time)
cd webui/frontend && pnpm i && pnpm build && cd ../..

# Start
python -m webui.server
# Open in browser http://127.0.0.1:8765
```# First-Time Access

First-time access will redirect to `/setup` to create an admin account.

## 14-Step Process

See `docs/superpowers/specs/2026-04-28-webui-design.md` for details.

| Phase | Steps |
|---|---|
| 1 Basics (5) | Mode selection / System dependencies / Cloudflare / IMAP / Proxy |
| 2 Payment (2) | PayPal / Card + Billing |
| 3 CAPTCHA (2, optional) | CAPTCHA solving service / VLM endpoint |
| 4 Downstream (4) | Team plan / gpt-team / CPA / Daemon / Stripe runtime |
| 5 Completion (1) | Review + Export |

The right column `PreflightPanel` displays passed checks in real-time for each step.

## Reverse Proxy (Public Internet Access)

The webui binds to `127.0.0.1` by default. To allow access from other machines, use nginx reverse proxy:```nginx
location /webui/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
}
```## Development```bash
# Backend development mode (auto-reload)
uvicorn webui.server:create_app --factory --reload --host 127.0.0.1 --port 8765

# Frontend development mode (Vite proxy auto-transforms /api → 8765)
cd webui/frontend && pnpm dev
# Open http://127.0.0.1:5173

# Run tests
python -m pytest webui/tests/ -v       # Backend 47 tests
cd webui/frontend && pnpm test         # Frontend Vitest
```## Architecture

- Backend: FastAPI + SQLite (users + sessions) + JSON (wizard state) + bcrypt + sse-starlette
- Frontend: Vue 3 + Vite + TypeScript + Naive UI + Pinia + Vue Router
- Authentication: cookie session (httponly + SameSite=Lax)
- Launch: single process `python -m webui.server`, FastAPI serves both API + static frontend simultaneously