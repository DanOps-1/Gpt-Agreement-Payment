# Changelog

记录 webui / pipeline / scripts 的功能与协议改动，按 commit 倒序。

---

## PayPal 协议支付收尾 Plus 订阅路径

之前 `--paypal` 走的全是 Team 链路；Plus 在 modern 路径已支持，但 abcard + WebUI 导出 + CLI 都有遗漏，本次收尾。

- **`pipeline.py`** 新增 `--plan {team,plus}`：在所有分支（单次 / pay-only / batch / daemon / self-dealer）前先做一次配置覆盖——生成临时 config 把 `fresh_checkout.plan.plan_name` / `entry_point` / `promo_campaign_id` 对齐目标计划，Plus 模式额外剥掉 `workspace_name` / `seat_quantity`，不动用户原文件，跑完 atexit 清理
- **`card.py::_build_abcard_checkout_payload`**：识别 `plus` plan_name 时不再硬塞 `workspace_name` / `seat_quantity`。之前 Plus + access_token / abcard 链路会把 team 字段一起发出去，被 ChatGPT 后端 400
- **`card.py::_provision_openai_auth_via_local_bundle`**：Plus 时同步剥掉 ab_cfg.team_plan 里 example 残留的 team 字段，避免 CTF-reg 新开号阶段的 plan_name 与 seat 字段错配
- **`webui/backend/config_writer.py`**：导出 PayPal / Plus 配置时，主动剥 `fresh_checkout.plan` + `team_plan` 两段的 team-only 字段；之前 `_deep_merge` 会保留 example skeleton 默认的 `seat_quantity=5` / `workspace_name=MyWorkspace`，污染 Plus 导出
- **`config.paypal.example.json`**：plan 段补 `_comment`，提示 Plus 切换需要改哪些字段或直接用 `--plan plus`
- 测试覆盖：
  - `webui/tests/test_pipeline_plan_override.py` 覆盖 `_apply_plan_override` 的 Plus / Team 行为 + `_build_abcard_checkout_payload` 的 Plus payload
  - `webui/tests/test_config_writer.py` 加 `test_export_strips_team_only_fields_when_plan_is_plus` / `test_export_keeps_team_fields_when_plan_is_team`

---

## GoPay 支付 429 风控 bypass

`CTF-pay/gopay.py::_midtrans_init_linking` 增加风控绕过路径：

- **触发条件**：Midtrans `POST /snap/v3/accounts/{snap}/linking` 返回 429，或 body 含 `technical error` / `too many` / `rate limit` 等关键字（部分 IP / 高频场景必现）
- **bypass 做法**：同 endpoint 同 body 重发一次，但**剥掉 `Authorization: Basic …` 头**。不带 Auth 的请求绕过了 Midtrans 端的 SDK 风控分支，直接返回 `201 + activation_link_url`，下游 `validate-reference / user-consent / OTP / PIN` 流程不变
- **失败兜底**：bypass 也失败时抛 `GoPayError("midtrans linking bypass 失败 …")`，方便 daemon 层走重试 / 换 IP 逻辑
- 测试覆盖：`test_linking_429_bypass_drops_authorization` / `test_linking_200_with_technical_error_body_triggers_bypass` / `test_linking_429_bypass_also_fails_raises`

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
