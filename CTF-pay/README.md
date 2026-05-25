CTF 场景下当前协议主链，按真实抓包整理如下：

- `init`
- `elements/sessions`
- `consumers/sessions/lookup`
- `payment_pages/<session>` 地址 / tax_region 更新
- `confirm`
  - 优先走 `inline_payment_method_data`
  - 兼容 `shared_payment_method`
- `3ds2/authenticate`
- `poll`

几个容易误判的点：

- `setatt_` / `source` 有值，只代表拿到了 3DS authenticate 的 source，不代表已经成功。
- `state = challenge_required` 且 `ares.transStatus = C`，表示 **需要浏览器侧继续完成 challenge**，不是“废卡”。
- 只有浏览器把 challenge 真正做完，后面的 intent / setup_intent 状态才会继续推进。

当前脚本行为：

- 会在 `three_ds_result` 里记录：
  - `state`
  - `trans_status`
  - `source`
  - `acs_url`
  - `creq`
  - `three_ds_server_trans_id`
- 如果进入 `challenge_required`，脚本会停在这里，等待后续浏览器侧 challenge。

无图形环境下的 challenge 调试：

- `card.py` 现在会把最新 bridge 信息写到 `/tmp/stripe_hcaptcha_bridge_latest.json`
- 如果机器没有 `DISPLAY`，`card.py` 不再直接崩；可回退为 headless Playwright 或只输出 bridge URL
- 已提供本地 helper：`CTF-pay/hcaptcha_bridge_helper.py`
  - 用法：
    - `python hcaptcha_bridge_helper.py http://127.0.0.1:PORT/index.html`
  - 常用命令：
    - `TEXT`
    - `SHOT /tmp/bridge.png`
    - `CHSHOT /tmp/challenge.png`
    - `ECLICK x y`
    - `VERIFY`
    - `STATE`

`config.auto.json` 中和运行时最相关的字段：

- `runtime.confirm_mode`
  - `inline_payment_method_data`：更贴近当前真实前端链路
  - `shared_payment_method`：兼容旧的先建 `payment_method` 再 confirm
- `runtime.version`
- `runtime.js_checksum`
- `runtime.rv_timestamp`

注意：

- `runtime.js_checksum` 与 `runtime.rv_timestamp` 必须和当前 checkout runtime 对齐。
- `top_checkout_config_id` / `payment_method_checkout_config_id` 可以先留空，脚本会用当前会话上下文回填。
- 默认 `load_config()` 会优先读传入路径；如果没找到，会自动回退到 `CTF-pay/config.auto.json`。

## fresh checkout 自动生成

现在 `card.py` 支持在 session 失活前，先从 ChatGPT 侧重新生成新的 checkout：

- 命令：
  - `python card.py fresh --fresh-only`
  - `python card.py auto`
  - `python card.py --fresh`
  - 也可直接用预置方案1配置：
    - `python card.py auto --config config.auto-register.json`
- 默认推荐 **ABCard / access_token 模式**，不再依赖从 `flows` 提取登录态：
  - `Authorization: Bearer <access_token>`
  - 可选 `__Secure-next-auth.session-token`
  - 可选 `oai-device-id`
  - 先调 `GET /api/auth/session` 刷新 / 校验 access token
  - 再按 ABCard 兼容链路依次尝试：
    - `POST /backend-api/payments/checkout`
    - `POST /backend-api/subscriptions/checkout`
- ABCard 风格 payload：
  - `plan_type`
  - `payment_lower_bound_amount_cents`
  - `payment_upper_bound_amount_cents`
  - `billing_country_code`
  - `billing_currency_code`
  - `workspace_name`
  - `seat_quantity`
  - `promo_campaign_id`
- `flows` 现在只作为 **可选模板来源**：
  - 当 `request_style=modern` 或显式启用 `use_flows_for_templates` 时
  - 才会去读取 `../flows` 里的 sentinel/body 模板
- `config.auto.json` 已新增 `fresh_checkout` 段：
  - 默认：
    - `fresh_checkout.auth.mode = "access_token"`
    - `fresh_checkout.request_style = "abcard"`
    - `fresh_checkout.bootstrap_from_flows = false`
  - 主要填写：
    - `fresh_checkout.auth.access_token`
    - `fresh_checkout.auth.session_token`（可选，但推荐；脚本可自动刷新 access token）
    - `fresh_checkout.auth.device_id` / `oai_device_id`（可选）
    - `fresh_checkout.plan.plan_name`
    - `fresh_checkout.plan.promo_campaign_id`
  - 方案1（推荐闭环）：
    - `fresh_checkout.auth.auto_register.enabled = true`
    - `fresh_checkout.auth.auto_register.project_dir = "./CTF-reg"`
    - `fresh_checkout.auth.auto_register.config_path = "./CTF-reg/config.example.json"`
    - `CTF-reg` 是本仓库内置的注册流程代码目录，不再依赖外部项目路径
    - 如果 `mode = "auto_register"`，脚本会先调用本地注册流程拿到
      `access_token / session_token / device_id`，再生成 fresh checkout
    - 如果保留 `mode = "access_token"`，但开启了 `auto_register.enabled = true`，
      当现有 token 过期 / 失效 / 账号停用时，也会自动注册新号后重试
  - `auto_refresh_on_inactive: true` 时，如果 Stripe 返回 `checkout_not_active_session`，脚本会先自动生成 fresh checkout 再继续
  - 如果你需要把优惠 checkout 跑稳，建议再开启：
    - `fresh_checkout.check_coupon_after_checkout = true`
      - 创建 checkout 后补打一遍
        `GET /backend-api/promo_campaign/check_coupon`
      - 仅用于观测 `eligible / not_eligible`，**不**把它当成真正的 redeem 流程
    - `fresh_checkout.expected_due = 0`
      - 不看 ChatGPT checkout preview，而是以 Stripe `init.total_summary.due`
        为准校验是否命中优惠
    - `fresh_checkout.auto_refresh_on_due_mismatch = true`
      - 如果 fresh checkout 创建成功，但 Stripe `due` 不是预期金额，
        脚本会自动重新生成 fresh checkout 再试
    - `fresh_checkout.max_due_mismatch_refreshes = 3`
      - 控制金额不匹配时最多重刷几次
  - `pre_solve_passive_captcha = true`
    - 更贴近真实 `flows`：confirm 前先拿 `passive_captcha_token`
    - 历史 `due=0` 流里，confirm 请求里是带了 `passive_captcha_token` 的
  - `browser_challenge.use_for_passive_captcha = true`
    - 会优先尝试用本地 headless 浏览器执行 Stripe invisible hCaptcha，
      尽量拿到和真实前端更接近的 passive token
    - 如果浏览器方案没拿到 token，再回退到打码平台
  - `browser_challenge.passive_headless = true`
    - 在无 DISPLAY 的环境里也能跑 passive captcha 浏览器链路
  - `browser_challenge.passive_timeout_ms = 45000`
    - 控制 invisible/passive captcha 的浏览器等待时长

注意：

- 如果 `/api/auth/session` 还能返回用户信息，但 checkout 返回：
  - `401[token_invalidated]`：当前 access_token / session_token 登录态已被撤销；
  - `401[account_deactivated]`：当前账号本身已被停用；
  - 这两种情况都不是 Stripe 协议问题，而是 ChatGPT 侧凭证 / 账号状态问题。

## 纯本地 CTF mock gateway

如果当前目标是继续压实 **本地 challenge / 3DS 状态机**，而不希望依赖任何外部网络，可直接使用：

- `python card.py auto --config config.local-mock.json --local-mock`

行为：

- 自动从本地 `flows` 重建 fresh checkout 参数；
- 在 `127.0.0.1` 启动一个本地 HTTP mock gateway；
- 由 `card.py` 对本地 gateway 发起真实 HTTP 请求，回放：
  - `fresh checkout`
  - `confirm`
  - `verify_challenge`
  - `3ds2/authenticate`
  - `poll`

默认场景：

- `challenge_pass_then_decline`
  - 本地模拟 `network_checkcaptcha(pass=true)`
  - `verify_challenge -> requires_action`
  - `3DS2 -> succeeded`
  - 最终 `card_declined`

也支持：

- `challenge_failed`
- `no_3ds_card_declined`

回放工件默认写到：

- `/tmp/ctf_local_mock_latest.json`

## GoPay WhatsApp OTP 自动接收

GoPay linking 会把 OTP 发到 WhatsApp。旧流程只支持 CLI / WebUI 手动输入；
现在 `gopay.py` 支持自动从 WebUI WhatsApp 登录 sidecar、本地 HTTP relay、
state/log 文件或命令取码。

### 1. WebUI 推荐路径：只暴露一个 WhatsApp 登录入口

WebUI GoPay 配置页只显示一个入口：

```text
WhatsApp 登录 / 扫码接收 GoPay OTP
```

点击后进入 `/whatsapp`，扫码登录 WhatsApp。登录页可以自由选择
`baileys` 或 `wwebjs` 引擎：默认推荐 Baileys（`@whiskeysockets/baileys`）
直接监听 WhatsApp multi-device socket；如需回退到旧的
`whatsapp-web.js`/Chromium 路径，在页面下拉选择 `whatsapp-web.js`
并重启 sidecar 即可。`WEBUI_WA_ENGINE=wwebjs` 仍可作为首次启动默认值。
sidecar 会监听新消息，提取 GoPay OTP，并写入：

```text
SQLite runtime_meta[wa_state] / HTTP relay
```

注意：GoPay/WhatsApp 的 OTP 模板有时会被 WhatsApp 标记为敏感消息，
linked device（WhatsApp Web）只能看到类似“你收到了一次性密码，只能在主要
设备上查看”的占位提示，拿不到验证码正文。这不是解析正则问题，而是
WhatsApp Web 不下发 OTP 正文。WebUI runner 会在支付日志出现
`[gopay] waiting WhatsApp OTP from file: ...` 时自动弹出 GoPay OTP 兜底
输入框；把手机主设备上看到的验证码填进去后，会写入同一个
`SQLite runtime_meta[wa_state] / HTTP relay`，支付流程继续。

WebUI 导出 GoPay 配置时会自动写入：

```json
{
  "gopay": {
    "otp": {
      "source": "file",
      "path": "SQLite runtime_meta[wa_state] / HTTP relay",
      "timeout": 300,
      "interval": 1
    }
  }
}
```

sidecar 依赖在 `webui/whatsapp_relay/`：

```bash
cd webui/whatsapp_relay
npm install
```

当前依赖包含：

- `@whiskeysockets/baileys`：默认引擎；
- `whatsapp-web.js`：备用引擎。

### 2. 可选：独立 HTTP relay

仓库内置一个很小的 HTTP relay，只负责接收你自己控制的 WhatsApp webhook /
通知转发内容，提取 6 位 OTP，写入 `SQLite runtime_meta[wa_state]`，并暴露 `/latest`
给支付流程轮询：

```bash
python CTF-pay/whatsapp_otp_relay.py --port 8765
```

本地快速自测：

```bash
curl -X POST http://127.0.0.1:8765/ingest \
  -H 'Content-Type: application/json' \
  -d '{"from":"gopay","text":"Kode verifikasi GoPay Anda adalah 123456"}'

curl http://127.0.0.1:8765/latest
```

`/webhook` 兼容 Meta WhatsApp Cloud API 常见 webhook 形态：

- `GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`
  用于 webhook 校验；
- `POST /webhook` 接收 `entry[].changes[].value.messages[]` 消息。

如果你用 Android 通知转发、已有 WhatsApp Web bridge 或其他自建 relay，只要把
消息文本 POST 到 `/ingest`，或自己提供一个返回最新 OTP 的 `/latest` 接口即可。

### 3. 手动配置 `gopay.otp`

示例见 `CTF-pay/config.gopay.example.json`：

```json
{
  "gopay": {
    "country_code": "62",
    "phone_number": "81234567890",
    "pin": "YOUR_6_DIGIT_GOPAY_PIN",
    "otp": {
      "source": "file",
      "path": "SQLite runtime_meta[wa_state] / HTTP relay",
      "timeout": 300,
      "interval": 1,
      "code_regex": "(?<!\\d)(\\d{6})(?!\\d)",
      "issued_after_slack_s": 15
    }
  }
}
```

也支持文件轮询：

```json
{
  "gopay": {
    "otp": {
      "source": "file",
      "path": "SQLite runtime_meta[wa_state]",
      "timeout": 300,
      "interval": 1
    }
  }
}
```

HTTP relay 轮询：

```json
{
  "gopay": {
    "otp": {
      "source": "http",
      "url": "http://127.0.0.1:8765/latest",
      "timeout": 300,
      "interval": 1
    }
  }
}
```

或命令轮询：

```json
{
  "gopay": {
    "otp": {
      "source": "command",
      "command": ["python", "scripts/get_latest_wa_otp.py"],
      "timeout": 300,
      "interval": 2
    }
  }
}
```

### 4. 运行

CLI / pipeline 只要不传 `--gopay-otp-file`，就会优先使用 `gopay.otp`：

```bash
python CTF-pay/gopay.py --config CTF-pay/config.gopay.example.json
python CTF-pay/card.py auto --config CTF-pay/config.paypal.json --gopay
python pipeline.py --config CTF-pay/config.paypal.json --gopay
```

WebUI 模式下，如果配置里存在非手动 `gopay.otp`，runner 会跳过旧的
`--gopay-otp-file` 手动弹窗，让 `gopay.py` 直接轮询自动 OTP provider。
如果自动 provider 等待的是文件路径，runner 也会识别等待日志并打开同一个
手动兜底弹窗，用于处理 WhatsApp Web “主要设备可见”占位消息。如果没有配置
`gopay.otp`，行为保持不变：运行页弹窗手动输入 WhatsApp OTP。
