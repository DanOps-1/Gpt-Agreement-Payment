# Configuration Reference

[← Back to README](../README.md)

The repository only ships `*.example.json` templates. You need to copy a real configuration file (gitignored) and fill in the values yourself.```bash
cp CTF-pay/config.paypal.example.json       CTF-pay/config.paypal.json
cp CTF-reg/config.paypal-proxy.example.json CTF-reg/config.paypal-proxy.json
cp CTF-reg/config.example.json              CTF-reg/config.noproxy.json
```---

## `CTF-pay/config.paypal.json` — Main Configuration

The overall configuration for the payment side. The daemon mode also uses this same file.

### `team_system` —— Push downstream gpt-team system (optional)```json
"team_system": {
  "enabled": false,
  "base_url": "http://127.0.0.1:3000",
  "username": "admin",
  "password": "YOUR_TEAM_SYSTEM_PASSWORD",
  "timeout_s": 60,
  "domain_cooldown_hours": 24
}
```| Field | Meaning |
|---|---|
| `enabled` | Disable to stop pushing downstream |
| `base_url` | gpt-team backend address |
| `username` / `password` | Account credentials for logging into gpt-team |
| `domain_cooldown_hours` | Domain cooldown duration after `invite=no_permission` |

### `daemon` —— Daemon mode parameters```json
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
```| Field | Meaning |
|---|---|
| `target_ok_accounts` | Target capacity of the account pool; run pipeline if not reached |
| `poll_interval_s` | How often to check capacity |
| `rate_limit.per_hour / per_day` | Maximum runs per hour / per day (0 = unlimited) |
| `max_consecutive_failures` | Cooldown after N consecutive failures |
| `consecutive_fail_cooldown_s` | Cooldown duration after failures |
| `jitter_before_run_s [min, max]` | Random jitter before each run |
| `seat_limit` | Team invitation limit per Team when self-dealing |
| `gpt_team_db_path` | Path to directly read gpt-team database for CF cleanup |
| `cf_cleanup_every_n_runs` | Frequency of CF DNS dead subdomain cleanup |

### `webshare` — Proxy API Configuration```json
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
```| Field | Meaning |
|---|---|
| `api_key` | Key obtained from Webshare console |
| `refresh_threshold` | Number of consecutive `no_invite_permission` triggers to rotate IP |
| `no_rotation_cooldown_s` | Cooldown duration after quota exhaustion |
| `lock_country` | Lock country (US is relatively stable) |
| `zone_rotate_after_ip_rotations` | Switch zone after rotating IP this many times within the same zone |
| `gost_listen_port` | Local gost relay listening port |
| `sync_team_proxy` | Whether to sync gpt-team global proxy settings after IP rotation |

### `cpa` — Push downstream CPA server (optional)```json
"cpa": {
  "enabled": true,
  "base_url": "https://your-cpa-host/api",
  "admin_key": "YOUR_CPA_ADMIN_KEY",
  "oauth_client_id": "YOUR_OPENAI_CODEX_CLIENT_ID",
  "plan_tag": "team",
  "timeout_s": 20
}
````oauth_client_id` is the OAuth client_id of Codex CLI — the specific value can be seen from the Codex CLI source code.

### `proxies` — Global Proxy Pool```json
"proxies": {
  "enabled": true,
  "rotation": "random",
  "list": ["socks5://127.0.0.1:18898"]
}
```| Field | Meaning |
|---|---|
| `rotation` | `random` / `static` / `lru` (rotate by "least recently used") |
| `list` | Fill multiple entries when using multiple proxies |

### `paypal` / `cards` / `captcha` / `fresh_checkout` / `runtime`

For the remaining fields, see the comments in the template and [`hcaptcha-solver.md`](hcaptcha-solver.md) / [`operating-modes.md`](operating-modes.md).

---

## `CTF-reg/config.paypal-proxy.json` — Registration-side configuration```json
{
  "mail": {
    "_comment": "OTP goes through CF Email Worker → KV, credentials are in SQLite runtime_meta[secrets], only configure catch-all domain here",
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
```> **OTP Reception: CF Email Worker → KV** (no longer pulling QQ mailbox via IMAP)
>
> OTP emails for registration and PayPal login are routed through Cloudflare Email Routing → `otp-relay`
> Worker → KV storage (millisecond-level, see [`scripts/setup_cf_email_worker.py`](../scripts/setup_cf_email_worker.py) one-click deployment + [`scripts/otp_email_worker.js`](../scripts/otp_email_worker.js)).
>
> After one-time setup, OTP credentials are written to `SQLite runtime_meta[secrets]`:
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
> You can also temporarily override with environment variables `CF_API_TOKEN` / `CF_ACCOUNT_ID` / `CF_OTP_KV_NAMESPACE_ID`.

`mail.auto_provision` is a multi-zone domain pool configuration:

| Field | Meaning |
|---|---|
| `enabled` | Enable auto-provisioning of new subdomains |
| `zone_names` | List of candidate zones; switch to the next one when the first is exhausted |
| `min_available` | Minimum number of available subdomains in the pool; create new ones if below this |
| `min_segs` / `max_segs` | Number of segments in a subdomain (e.g., `aaa.bbb.zone` has 2 segments) |
| `min_seg_len` / `max_seg_len` | Length of each segment |
| `dns_propagation_s` | Wait time for DNS propagation after creating a new subdomain |

---

## VLM endpoint

The hCaptcha solver's VLM is configured via environment variables, defaulting to OpenAI connection:```bash
export CTF_VLM_BASE_URL="https://api.openai.com/v1"
export CTF_VLM_API_KEY="sk-..."
export CTF_VLM_MODEL="gpt-4o"
```You can also connect to any OpenAI-compatible endpoint (self-hosted OpenAI proxy / local vLLM / other vendor gateways).

---

## Tuning Environment Variables

| Variable | Default | Effect |
|---|---|---|
| `SKIP_SIGNUP_CODEX_RT` | `1` | Skip known-failing OAuth paths during signup phase, saves ~30s/account |
| `SKIP_HERMES_FAST_PATH` | `1` | Skip PayPal endpoints that return `genericError` for non-browser sessions, saves 5–10s/payment |
| `CTF_VLM_BASE_URL` | `https://api.openai.com/v1` | hCaptcha solver's VLM endpoint |
| `CTF_VLM_API_KEY` | (empty) | VLM bearer token |
| `CTF_VLM_MODEL` | `gpt-4o` | VLM model ID |

---

## Config Loading Priority

Lookup order for `load_config()`:

1. Command line `--config <path>` explicitly specified
2. Default `CTF-pay/config.auto.json`
3. Fallback to template `CTF-pay/config.paypal.example.json` (read-only)

Environment variables override config with higher priority, allowing temporary parameter adjustments without modifying files.