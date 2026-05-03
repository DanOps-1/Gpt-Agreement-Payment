# 配置参考

[← 回到 README](../README.md)

仓库里只 ship `*.example.json` 模板，真实配置（gitignored）需要自己拷一份填值。

```bash
cp CTF-pay/config.paypal.example.json       CTF-pay/config.paypal.json
cp CTF-reg/config.paypal-proxy.example.json CTF-reg/config.paypal-proxy.json
cp CTF-reg/config.example.json              CTF-reg/config.noproxy.json
```

---

## `CTF-pay/config.paypal.json` — 主配置

支付侧的总配置。daemon 模式用的也是这一个文件。

### `team_system` —— 推下游 gpt-team 系统（可选）

```json
"team_system": {
  "enabled": false,
  "base_url": "http://127.0.0.1:3000",
  "username": "admin",
  "password": "YOUR_TEAM_SYSTEM_PASSWORD",
  "timeout_s": 60,
  "domain_cooldown_hours": 24
}
```

| 字段 | 含义 |
|---|---|
| `enabled` | 关掉就不推下游 |
| `base_url` | gpt-team 后端地址 |
| `username` / `password` | 登 gpt-team 的账号 |
| `domain_cooldown_hours` | `invite=no_permission` 后的域冷却时长 |

### `daemon` —— 常驻模式参数

```json
"daemon": {
  "target_ok_accounts": 80,
  "usage_pool": "recovery",
  "poll_interval_s": 600,
  "rate_limit": { "per_hour": 0, "per_day": 0 },
  "max_consecutive_failures": 5,
  "consecutive_fail_cooldown_s": 1800,
  "jitter_before_run_s": [0, 0],
  "seat_limit": 5,
  "gpt_team_db_path": "/path/to/gpt-team/backend/db/database.sqlite",
  "cf_cleanup_every_n_runs": 30
}
```

| 字段 | 含义 |
|---|---|
| `target_ok_accounts` | 补号池目标容量，达不到就跑 pipeline |
| `poll_interval_s` | 多久查一次容量 |
| `rate_limit.per_hour / per_day` | 每小时 / 每天最多跑几次（0 = 无限） |
| `max_consecutive_failures` | 连续 N 次失败后冷却 |
| `consecutive_fail_cooldown_s` | 失败冷却时长 |
| `jitter_before_run_s [min, max]` | 每次跑前的随机抖动 |
| `seat_limit` | self-dealer 时单 Team 邀请上限 |
| `gpt_team_db_path` | 直接读 gpt-team 数据库的路径，CF 清理用 |
| `cf_cleanup_every_n_runs` | CF DNS 死子域清理频率 |

### `webshare` —— 代理 API 配置

```json
"webshare": {
  "enabled": true,
  "api_key": "YOUR_WEBSHARE_API_KEY",
  "refresh_threshold": 2,
  "no_rotation_cooldown_s": 10800,
  "lock_country": "US",
  "zone_rotate_after_ip_rotations": 2,
  "gost_listen_port": 18898,
  "sync_team_proxy": true
}
```

| 字段 | 含义 |
|---|---|
| `api_key` | Webshare 控制台拿到的 key |
| `refresh_threshold` | 连续多少次 `no_invite_permission` 触发换 IP |
| `no_rotation_cooldown_s` | 配额耗尽后的冷却时长 |
| `lock_country` | 锁国（US 比较稳） |
| `zone_rotate_after_ip_rotations` | 同 zone 内换几次 IP 后切 zone |
| `gost_listen_port` | 本地 gost 中继监听的端口 |
| `sync_team_proxy` | 换 IP 后是否同步 gpt-team 全局代理设置 |

### `cpa` —— 推下游 CPA 服务器（可选）

```json
"cpa": {
  "enabled": true,
  "base_url": "https://your-cpa-host/api",
  "admin_key": "YOUR_CPA_ADMIN_KEY",
  "oauth_client_id": "YOUR_OPENAI_CODEX_CLIENT_ID",
  "plan_tag": "team",
  "timeout_s": 20
}
```

`oauth_client_id` 是 Codex CLI 的 OAuth client_id —— 从 Codex CLI 源码可以看到具体值。

### `proxies` —— 全局代理池

```json
"proxies": {
  "enabled": true,
  "rotation": "random",
  "list": ["socks5://127.0.0.1:18898"]
}
```

| 字段 | 含义 |
|---|---|
| `rotation` | `random` / `static` / `lru`（按"最近使用"轮换） |
| `list` | 多代理时填多条 |

### `paypal` / `cards` / `captcha` / `fresh_checkout` / `runtime`

剩下的字段含义详见模板里的注释和 [`hcaptcha-solver.md`](hcaptcha-solver.md) / [`operating-modes.md`](operating-modes.md)。

---

## `CTF-reg/config.paypal-proxy.json` — 注册侧配置

```json
{
  "mail": {
    "_comment": "OTP 走 CF Email Worker → KV，凭证在 SQLite runtime_meta[secrets]，这里只配 catch-all 域名",
    "catch_all_domain": "subdomain.example.com",
    "catch_all_domains": ["subdomain.example.com"],
    "auto_provision": {
      "enabled": false,
      "zone_names": ["zone-a.example", "zone-b.example"],
      "min_available": 3,
      "min_segs": 1, "max_segs": 4,
      "min_seg_len": 2, "max_seg_len": 5,
      "dns_propagation_s": 20
    }
  },
  "card": { "number": "...", "cvc": "...", "exp_month": "...", "exp_year": "..." },
  "billing": { ... },
  "team_plan": { "plan_name": "chatgptteamplan", "workspace_name": "MyWorkspace", ... },
  "captcha": { "client_key": "YOUR_CAPTCHA_API_KEY" },
  "proxy": "socks5://USER:PASS@PROXY_HOST:PORT"
}
```

> **OTP 接收：CF Email Worker → KV**（不再用 IMAP 拉 QQ 邮箱）
>
> 注册和 PayPal 登录的 OTP 邮件都经 Cloudflare Email Routing → `otp-relay`
> Worker → KV 落库（毫秒级，见 [`scripts/setup_cf_email_worker.py`](../scripts/setup_cf_email_worker.py) 一键部署 + [`scripts/otp_email_worker.js`](../scripts/otp_email_worker.js)）。
>
> 一次性配好后，OTP 凭证写到 `SQLite runtime_meta[secrets]`：
>
> ```json
> {
>   "cloudflare": {
>     "api_token": "cfut_...",
>     "account_id": "<account-id>",
>     "otp_kv_namespace_id": "<kv-namespace-id>",
>     "otp_worker_name": "otp-relay",
>     "zone_names": ["zone-a.example", "zone-b.example"]
>   }
> }
> ```
>
> 也可以用环境变量 `CF_API_TOKEN` / `CF_ACCOUNT_ID` / `CF_OTP_KV_NAMESPACE_ID`
> 临时覆盖。

`mail.auto_provision` 是多 zone 域池配置：

| 字段 | 含义 |
|---|---|
| `enabled` | 启用自动开新子域 |
| `zone_names` | 候选 zone 列表，第一个用完切第二个 |
| `min_available` | 池里至少有多少个可用子域，少于就开新的 |
| `min_segs` / `max_segs` | 子域有几段（如 `aaa.bbb.zone` 是 2 段） |
| `min_seg_len` / `max_seg_len` | 每段长度 |
| `dns_propagation_s` | 开新子域后等多久 DNS 生效 |

---

## VLM endpoint

hCaptcha 求解器的 VLM 通过环境变量配置，默认连 OpenAI：

```bash
export CTF_VLM_BASE_URL="https://api.openai.com/v1"
export CTF_VLM_API_KEY="sk-..."
export CTF_VLM_MODEL="gpt-4o"
```

也可以连任何 OpenAI 兼容的 endpoint（自建的 OpenAI proxy / 本地 vLLM / 其他厂商网关）。

---

## 调优环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `SKIP_SIGNUP_CODEX_RT` | `1` | 跳过 signup 阶段已知失败的 OAuth 路径，省 ~30s/账号 |
| `SKIP_HERMES_FAST_PATH` | `1` | 跳过 PayPal 对非浏览器 session 返 `genericError` 的端点，省 5–10s/支付 |
| `CTF_VLM_BASE_URL` | `https://api.openai.com/v1` | hCaptcha solver 的 VLM endpoint |
| `CTF_VLM_API_KEY` | （空） | VLM bearer token |
| `CTF_VLM_MODEL` | `gpt-4o` | VLM 模型 ID |

---

## 配置加载优先级

`load_config()` 的查找顺序：

1. 命令行 `--config <path>` 显式指定
2. 默认 `CTF-pay/config.auto.json`
3. fallback 到模板 `CTF-pay/config.paypal.example.json`（只读）

环境变量覆盖比 config 优先级高，可以临时调参不用改文件。
