# Changelog

记录 webui / pipeline / scripts 的功能与协议改动，按 commit 倒序。

---

## [0074642] webui 账号面板大升级 + CPA / 注册链路多处修复

> commit message 里漏写了**运行时数据 JSONL → SQLite 大迁移**，这里补全完整范围。详见 `docs/architecture.md` 191 行起的 SQLite 存储说明。

### 运行时数据迁移（之前 message 漏）
- 账号 / 支付 / OAuth 状态从分散的 `output/*.jsonl` 文件迁移到单一 SQLite (`output/webui.db`):
  - `output/registered_accounts.jsonl` → 表 `registered_accounts`
  - `output/results.jsonl` → 表 `pipeline_results` + `card_results`
  - `output/secrets.json` / `daemon_state.json` / `webui_wizard_state.json` / `email_domain_state.json` / `wa_state.json` → 表 `runtime_meta` (key/value JSON)
  - 新表 `oauth_status` 单独跟踪 OAuth 链路状态
- 启动时 `_purge_legacy_runtime_files` 自动清掉旧 jsonl，避免双写造成数据漂移
- pipeline 调用面同步切换：`_append_result` / 读 results.jsonl 等全部走 db 接口

### 新增功能（已在 commit message 里）
- webui 账号库存：批量验证 + 批量删除 + plan 推断（free/plus/team）+ CPA 推送状态展示与"推送→CPA"按钮
- 账号有效性验证三层探活：rt → at → cookie，401/invalid_grant 判 invalid，CF 拦截/超时判 unknown
- CPA preflight 改用 `GET /v0/management/auth-files` + Bearer
- Codex OAuth `client_id` 后端硬编码兜底 `app_EMoamEEZ73f0CkXaXp7hrann`，前端不再让用户手填
- webshare preflight 补 `mode=direct` 查询参数
- `config_writer` webshare 模式自动注入 `socks5://127.0.0.1:18898`，避免 example 模板的 `USER:PASS` 占位透传
- vite `WEBUI_BASE` 修复 + `server.py` 同时挂 `/` 和 `/webui/`，直连和反代都通
- 新 favicon (`webicon.png`) + 右下角 GitHub 链接
- `batch` / `register_only` / `pay_only` 三个 flag 解耦，`batch + register-only` = 批量注册 N 个不付费
- worker OTP 抽取排除 `#XXXXXX` hex 颜色 + `color: / bgcolor=` 上下文（OpenAI 邮件 `#353740` 假阳性根因）
- `browser_register` 检测 OpenAI "Incorrect code" 红字立即 fail，避免触发 `max_check_attempts` 风控

---

## [bf0cca2] WhatsApp relay 支持自由切换引擎
（前略）
