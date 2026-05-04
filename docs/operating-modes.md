# Operating Modes

[← Back to README](../README.md)

All four modes use the same `pipeline.py` entrypoint, switched by CLI flags.

---

## Single Run

```bash
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal
```

Registration → Payment → OAuth → Write SQLite runtime DB (`output/webui.db`). ~5 minutes total.

Most common mode for debugging. Run full pipeline each time to see where it fails.

---

## Batch Parallel

```bash
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal \
    --batch 10 --workers 3
```

| Flag | Meaning |
|------|---------|
| `--batch N` | Run N full pipelines |
| `--workers M` | M parallel workers |

> ⚠️ **Parallel is not free**. Multiple registrations in same time window trigger batch association (cohort detection). Limit `--workers` ≤ 3, use different proxy/domain/timing jitter per worker. See [`anti-fraud-research.md`](anti-fraud-research.md).

---

## Self-Dealer (Self-Produce Self-Sell)

Cheapest mode. One PayPal charge gets 1 owner + N members in full Team workspace. Each member is independent ChatGPT account, can swap OAuth `refresh_token` individually.

```bash
# 1 owner + 4 members
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal --self-dealer 4

# Reuse paid owner, skip Step 1 to save one charge
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal \
    --self-dealer 4 --self-dealer-resume <owner_email>
```

### Internal Timing

| Phase | Count | Reuse | Output |
|-------|-------|-------|--------|
| 1. Register → Pay → Push downstream | 1 (owner) | `pipeline()` / `card.py` | `team_id` + owner refresh_token |
| 2. Register → Invite → Accept → Push downstream | N (member) | `register()` + invites API + `_exchange_refresh_token_with_session` | N member refresh_tokens, all bound to same `team_id` |

### Member Loop Single Timing (~3 min steady state)

1. Pick proxy + subdomain + temp `cardw` config (matches owner pipeline)
2. `register()` — Camoufox passes Turnstile + CF KV fetch OTP (≈1 min)
3. Owner Bearer calls `POST /backend-api/accounts/{team_id}/invites` (<1s)
4. Member Bearer calls `POST /backend-api/accounts/{team_id}/invites/accept` (<1s)
5. `card._exchange_refresh_token_with_session` — Camoufox re-login (email+pass+consent) gets refresh_token (~30s)
6. Append to SQLite runtime → Push downstream

### Key APIs (Reverse-engineered from chatgpt.com frontend JS)

```
POST https://chatgpt.com/backend-api/accounts/{team_id}/invites
   ↓ owner Bearer, body: {emails: ["target@..."]}

POST https://chatgpt.com/backend-api/accounts/{team_id}/invites/accept
   ↓ invitee Bearer, from frontend JS `4813494d-*.js` /accounts/{account_id}/invites/accept
```

### Safety Mechanisms (Reuse owner pipeline)

- Each member picks unique `proxy_pool.pick()` + `domain_pool.pick()` + temp cardw config, no association
- Any single member step failure (register/invite/accept/relogin/CPA) caught by try/except, continue next
- `--self-dealer-resume` reads existing owner (paid) `team_account_id` + `refresh_token` from SQLite, avoids re-charge

### Each Member Two Codex OAuth (First Always Fails)

- **First**: `browser_register.py` end-of-signup hydra session. **Always fails** (`token_exchange_user_error`). Default `SKIP_SIGNUP_CODEX_RT=1` skips; set `0` to see old path
- **Second**: Camoufox re-login (full login session) — Succeeds with RT

---

## Daemon Mode — Continuous Account Pool Maintenance

Runs pipeline as persistent background service, auto-maintains external [gpt-team](https://github.com/DanOps-1/gpt-team) **recovery pool** (`account_usage='recovery'`) usable count ≥ target.

```bash
# Background (recommended, -u disables buffering for real-time logs)
(nohup xvfb-run -a python3 -u pipeline.py --config CTF-pay/config.paypal.json --paypal --daemon \
    > output/logs/daemon-$(date +%Y%m%d-%H%M%S).log 2>&1 &)

# Tail logs
tail -f output/logs/daemon-*.log

# Foreground (debug, Ctrl+C graceful exit)
xvfb-run -a python3 -u pipeline.py --config CTF-pay/config.paypal.json --paypal --daemon

# Check status
cat SQLite runtime_meta[daemon_state] | jq .

# Graceful stop (finishes current cycle)
pkill -TERM -f "pipeline.*--daemon"

# Force kill (clears Camoufox + Xvfb leftovers)
pkill -9 -f "pipeline.*--daemon"
pkill -9 -f camoufox-bin
pkill -9 -f browser_register
for pid in $(pgrep Xvfb); do kill -9 $pid; done
```

### Work Loop

```
loop:
    sleep poll_interval_s

    if rate_limit blocked:
        continue

    usable = query gpt-team DB (!isBanned && !isDisabled && !noInvitePermission && !expired && seat available)

    if usable < target_ok_accounts:
        try:
            ensure_gost_alive()       # Watchdog
            cleanup_temp_leftovers()  # /tmp orphans
            if time to clear CF:
                cleanup_dead_cf()
            run_pipeline()
            Clear state flags (if invite=ok)
        except:
            Follow corresponding self-healing branch by exception type
```

### 12 Self-Healing Loops

See [`daemon-mode.md`](daemon-mode.md).

---

## Register-Only / Pay-Only

Debug: Split workflow:

```bash
# Register only
python pipeline.py --register-only --cardw-config CTF-reg/config.paypal-proxy.json

# Pay only (uses latest account from SQLite)
xvfb-run -a python pipeline.py --pay-only --config CTF-pay/config.paypal.json --paypal
```

---

## Direct card.py Invocation

Bypass pipeline orchestrator, call card.py directly:

```bash
# Standard card payment (auto_register mode)
python CTF-pay/card.py auto --config CTF-pay/config.auto.json

# Resume from existing checkout session
python CTF-pay/card.py cs_live_xxx --config CTF-pay/config.auto.json

# Use Nth card (0-based)
python CTF-pay/card.py auto --card 1 --config CTF-pay/config.auto.json

# Offline replay (no external requests)
python CTF-pay/card.py auto --config CTF-pay/config.offline-replay.json --offline-replay

# Local mock gateway
python CTF-pay/card.py auto --config CTF-pay/config.local-mock.json --local-mock

# Replay declined card terminal state
python CTF-pay/retry_house_decline.py cs_live_xxx --attempts 5
```

---

## Tested Timing Optimization Switches

Based on recent daemon + self-dealer full logs, two 100% failure paths have opt-in switches (default skip):

| Env Var | Default | Savings | Description |
|---------|---------|---------|-------------|
| `SKIP_SIGNUP_CODEX_RT` | `1` | ~30s/reg | signup hydra session cannot exchange Codex RT (`token_exchange_user_error`), refresh_token from later `_exchange_refresh_token_with_session` (pay/self-dealer relogin) |
| `SKIP_HERMES_FAST_PATH` | `1` | 5–10s/pay | PayPal rejects non-browser cookied sessions `/checkoutweb/genericError?code=REVGQVVMVA` ("DEFAULT"), all payments use browser path |

Both switches default on (skip guaranteed-fail paths). Set `SKIP_*=0` to restore old behavior for comparison or if PayPal changes protocol.
