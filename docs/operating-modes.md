# Running Modes

[← Back to README](../README.md)

All four modes use the same `pipeline.py` entry point and switch between them via command-line parameters.

---

## Single Run```bash
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal
```Register → Pay → OAuth → Write SQLite runtime library (`output/webui.db`). About five minutes overall.

The most commonly used mode during debugging. Run through the complete pipeline each time to see where it fails.

---

## Batch Parallel```bash
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal \
    --batch 10 --workers 3
```| Parameter | Meaning |
|---|---|
| `--batch N` | Run N complete pipelines |
| `--workers M` | Parallelize M workers |

> ⚠️ **Parallelization is not free**. Multiple account registrations within the same time window trigger batch association, which from an anti-fraud perspective is treated as a cohort. It's recommended to keep `--workers` no more than 3, and have each worker use different proxies / domains / time jitter. See [`anti-fraud-research.md`](anti-fraud-research.md) for details.

---

## Self-dealer

The most cost-effective approach. One PayPal charge gets you a complete Team workspace with 1 owner + N members. Each member is an independent ChatGPT account with a separate OAuth `refresh_token`.```bash
# 1 owner + 4 members
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal --self-dealer 4

# Reuse paid owner, skip Step 1 to save one charge
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal \
    --self-dealer 4 --self-dealer-resume <owner_email>
```### Internal Timeline

| Stage | Count | Reuse | Output |
|---|---|---|---|
| 1. Register → Payment → Push Downstream | 1 (owner) | `pipeline()` / `card.py` | `team_id` + owner refresh_token |
| 2. Register → Invite → Accept Invite → Push Downstream | N (member) | `register()` + invites API + `_exchange_refresh_token_with_session` | N member refresh_tokens, all bound to same `team_id` |

### Member Loop Single Iteration Timeline (~3 minutes steady state)

1. Pick proxy + pick subdomain + write temp `cardw` config (consistent with owner pipeline)
2. `register()` —— Camoufox passes Turnstile + fetch OTP from CF KV (≈ 1 minute)
3. Owner's Bearer calls `POST /backend-api/accounts/{team_id}/invites` (< 1 second)
4. Member's Bearer calls `POST /backend-api/accounts/{team_id}/invites/accept` (< 1 second)
5. `card._exchange_refresh_token_with_session` —— Camoufox re-login (email + password + consent) to get refresh_token (~30 seconds)
6. Append records per SQLite runtime → push downstream

### Key APIs (reverse-engineered from chatgpt.com frontend JS)```
POST https://chatgpt.com/backend-api/accounts/{team_id}/invites
   ↓ owner Bearer, body: {emails: ["target@..."]}

POST https://chatgpt.com/backend-api/accounts/{team_id}/invites/accept
   ↓ invitee Bearer, frontend JS `4813494d-*.js` contains /accounts/{account_id}/invites/accept
```### Security Mechanism (Reuse owner pipeline)

- Each member independently picks `proxy_pool.pick()` + `domain_pool.pick()` + temporary cardw config, no association
- Any single member failure at any step (registration / invitation / acceptance / re-login / CPA) is caught by try/except, continue to next one
- `--self-dealer-resume` reads existing owners (already paid) from SQLite with `team_account_id` + `refresh_token`, avoiding duplicate charges

### Two Codex OAuth calls per member (first one must fail)

- **First call**: signup state hydra session at the end of registration flow in `browser_register.py`. **Must fail** (`token_exchange_user_error`). Default `SKIP_SIGNUP_CODEX_RT=1` to skip; set to `0` to see old path
- **Second call**: Camoufox re-login (login state full user session) —— successfully obtain RT

---

## Daemon Mode —— Continuously maintain recovery account pool

Allow pipeline to run as a resident background service, automatically maintaining the number of available accounts in external [gpt-team](https://github.com/DanOps-1/gpt-team) **recovery account pool** (`account_usage='recovery'`) to always be ≥ target value.```bash
# Run in background (recommended, -u disables buffering for real-time log writes)
(nohup xvfb-run -a python3 -u pipeline.py --config CTF-pay/config.paypal.json --paypal --daemon \
    > output/logs/daemon-$(date +%Y%m%d-%H%M%S).log 2>&1 &)

# Tail logs
tail -f output/logs/daemon-*.log

# Run in foreground (for debugging, graceful exit with Ctrl+C)
xvfb-run -a python3 -u pipeline.py --config CTF-pay/config.paypal.json --paypal --daemon

# Check status
cat SQLite runtime_meta[daemon_state] | jq .

# Graceful stop (finishes current cycle before exiting)
pkill -TERM -f "pipeline.*--daemon"

# Force kill (cleans up Camoufox + Xvfb remnants together)
pkill -9 -f "pipeline.*--daemon"
pkill -9 -f camoufox-bin
pkill -9 -f browser_register
for pid in $(pgrep Xvfb); do kill -9 $pid; done
```# Work Loop```
loop:
    sleep poll_interval_s

    if rate_limit stuck:
        continue

    usable = check gpt-team DB (!isBanned && !isDisabled && !noInvitePermission && !expired && seat not full)

    if usable < target_ok_accounts:
        try:
            ensure_gost_alive()       # watchdog
            cleanup_temp_leftovers()  # /tmp orphans
            if should cleanup CF:
                cleanup_dead_cf()
            run_pipeline()
            clear state machine flags (if invite=ok)
        except:
            route to corresponding self-healing branch by exception type
```### 12-Path Self-Healing Loop

See [`daemon-mode.md`](daemon-mode.md) for details.

---

## Registration Only / Payment Only

For debugging: run the process in separate steps:```bash
# Register only
python pipeline.py --register-only --cardw-config CTF-reg/config.paypal-proxy.json

# Pay only (using the latest account in SQLite)
xvfb-run -a python pipeline.py --pay-only --config CTF-pay/config.paypal.json --paypal
```## Directly Call card.py

Skip the pipeline orchestrator and directly call card.py:```bash
# Standard card payment (auto_register mode)
python CTF-pay/card.py auto --config CTF-pay/config.auto.json

# Continue from existing checkout session
python CTF-pay/card.py cs_live_xxx --config CTF-pay/config.auto.json

# Use the Nth card (0-based)
python CTF-pay/card.py auto --card 1 --config CTF-pay/config.auto.json

# Offline replay (no external requests)
python CTF-pay/card.py auto --config CTF-pay/config.offline-replay.json --offline-replay

# Local mock gateway
python CTF-pay/card.py auto --config CTF-pay/config.local-mock.json --local-mock

# Repeatedly test decline card terminal state
python CTF-pay/retry_house_decline.py cs_live_xxx --attempts 5
```---

## Measured Time Optimization Switches

Based on recent comprehensive daemon + self-dealer logs, two paths with 100% failure rates now have switches that skip them by default:

| Environment Variable | Default | Savings | Description |
|---|---|---|---|
| `SKIP_SIGNUP_CODEX_RT` | `1` | ~30s/registration | signup state hydra session cannot exchange for Codex RT (`token_exchange_user_error`), refresh_token is obtained later via `_exchange_refresh_token_with_session` (pay / self-dealer re-login) |
| `SKIP_HERMES_FAST_PATH` | `1` | 5–10s/payment | PayPal returns `/checkoutweb/genericError?code=REVGQVVMVA` ("DEFAULT") for non-browser cookied sessions, all payments actually use the browser path |

Both switches are enabled by default (skipping guaranteed failure paths). Set `SKIP_*=0` to restore the old behavior if you want to compare or when PayPal changes the protocol.