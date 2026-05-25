# webui — 配置向导 + preflight 体检

把 `pipeline.py` 第一次跑通的 1-3 小时配置过程压到 ~15 分钟。

## 快速开始

```bash
# 后端依赖
pip install -r webui/requirements.txt

# 前端构建（一次）
cd webui/frontend && pnpm i && pnpm build && cd ../..

# 启动
python -m webui.server
# 浏览器开 http://127.0.0.1:8765
```

首次访问会跳到 `/setup` 创建管理员账号。

## 14 步流程

详见 `docs/superpowers/specs/2026-04-28-webui-design.md`。

| Phase | 步骤 |
|---|---|
| 1 基础（5）| 模式选择 / 系统依赖 / Cloudflare / IMAP / 代理 |
| 2 支付（2）| PayPal / 卡 + Billing |
| 3 验证码（2，可选）| 打码平台 / VLM endpoint |
| 4 下游（4）| Team plan / gpt-team / CPA / Daemon / Stripe runtime |
| 5 完成（1）| Review + 导出 |

每步右栏 `PreflightPanel` 实时显示已通过的 check。

## 反向代理（公网访问）

webui 默认 bind `127.0.0.1`。要让其他机器访问，nginx 反代：

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

## 开发

```bash
# 后端开发模式（auto-reload）
uvicorn webui.server:create_app --factory --reload --host 127.0.0.1 --port 8765

# 前端开发模式（Vite proxy 自动转 /api → 8765）
cd webui/frontend && pnpm dev
# 开 http://127.0.0.1:5173

# 跑测试
python -m pytest webui/tests/ -v       # 后端 47 测试
cd webui/frontend && pnpm test         # 前端 Vitest
```

## 架构

- 后端：FastAPI + SQLite (users + sessions) + JSON (wizard state) + bcrypt + sse-starlette
- 前端：Vue 3 + Vite + TypeScript + Naive UI + Pinia + Vue Router
- 鉴权：cookie session（httponly + SameSite=Lax）
- 启动：单进程 `python -m webui.server`，FastAPI 同时 serve API + 静态前端
