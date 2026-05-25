# Debug Manual

[← Back to README](../README.md)

Follow this checklist item by item when you encounter issues.

---

## Log Locations```bash
# Complete pipeline logs
tail -f output/logs/card.log

# Daemon main logs
tail -f output/logs/daemon-*.log

# hCaptcha solver output per round
ls -lah /tmp/hcaptcha_auto_solver_live/

# PayPal browser screenshots at each stage
ls -lah /tmp/paypal_*.png

# Secondary OAuth login screenshots
ls -lah /tmp/rt_*.png

# Daemon status
cat SQLite runtime_meta[daemon_state] | jq .
```---

## Common Exceptions

### `CheckoutSessionInactive`

Stripe session has become inactive. Stripe checkout sessions expire after 24 hours by default, which can occur during long-running workflows or when the machine resumes after sleep.

**Auto Recovery**: Set `auto_refresh_on_inactive: true` in config, and `card.py` will automatically regenerate a fresh checkout to resume the workflow.```json
"fresh_checkout": {
  "auto_refresh_on_inactive": true
}
```### `ChallengeReconfirmRequired`

hCaptcha result has expired. hCaptcha tokens have a TTL (approximately 2 minutes) and will expire if there is too much delay before confirm.

**Manual recovery**: Re-run the confirm phase.

**Root solution**: Don't set the daemon's `jitter_before_run_s` too long, or avoid other time-consuming operations before confirm.

### `FreshCheckoutAuthError`

ChatGPT side rejected your auth credentials. Possible causes:

- `access_token` expired
- `session_token` invalid
- Account banned / disabled
- Account triggered `add-phone` wall

**Troubleshooting**:```python
# Call /api/auth/session directly once to check the response
import requests
r = requests.get(
    "https://chatgpt.com/api/auth/session",
    headers={"Cookie": "__Secure-next-auth.session-token=..."}
)
print(r.status_code, r.json())
```If 401 / token_invalidated → Re-register or refresh session_token.
If 401 / account_deactivated → The account is dead, switch to another account.

### `DatadomeSliderError`

Failed to solve PayPal's DataDome slider.

**Troubleshooting**:```bash
# Check the most recent failed screenshot
ls -lt /tmp/paypal_ddc_*.png | head -1

# Check solver decisions (if in daemon mode)
grep "DatadomeSliderError" output/logs/daemon-*.log | tail -5
```**daemon behavior**: daemon will rerun the current round without consuming IP burn quota.

**Manual debugging**:```python
# Temporarily add page.pause() in card.py::_try_solve_ddc_slider to pause the browser
# Then run with headless=False once to see what the DOM looks like
```### `WebshareQuotaExhausted`

Webshare API has no available replacement proxies (monthly quota exhausted).

**daemon behavior**: Mark `webshare_rotation_disabled = true`, enter `no_rotation_cooldown_s` (default 3h) cooldown.

**manual recovery**:```bash
# Upgrade Webshare plan, or
# manually modify SQLite runtime_meta[daemon_state] to set webshare_rotation_disabled to false
jq '.webshare_rotation_disabled = false | .no_perm_cooldown_until = 0' \
   SQLite runtime_meta[daemon_state] > /tmp/state.json && \
   mv /tmp/state.json SQLite runtime_meta[daemon_state]
```### `socks5 auth not supported`

Camoufox does not support socks5 with authentication. Configure a gost relay:```bash
gost -L=socks5://:18898 -F=socks5://USER:PASS@PROXY_HOST:PORT &
```Change the proxy in config to `socks5://127.0.0.1:18898`. The daemon mode has a built-in gost watchdog that automatically manages this process.

### `cannot open display`

xvfb is not running or `DISPLAY` was not passed:```bash
# Wrap with xvfb-run (recommended)
xvfb-run -a python pipeline.py ...

# Or manually start Xvfb
Xvfb :99 -screen 0 1920x1080x24 &
DISPLAY=:99 python pipeline.py ...
```### `geoip InvalidIP` / Camoufox Error `InvalidIP`

Usually the gost relay is down, and Camoufox cannot get a legitimate exit IP when connecting directly.

**daemon**: `_ensure_gost_alive()` will automatically detect port unbinding and restart gost.
**Single run**: Manually restart gost:```bash
pkill gost
gost -L=socks5://:18898 -F=socks5://USER:PASS@HOST:PORT &
```## Diagnostic Commands

### Check How It's Running```bash
# Overall success rate
jq -r '.total_succeeded as $s | .total_attempts as $t | "\($s)/\($t) = \($s/$t*100)%"' \
   SQLite runtime_meta[daemon_state]

# Hit rate per IP
grep "current_proxy_ip" output/logs/daemon-*.log | sort | uniq -c | sort -rn

# Usage count per zone
grep "current_zone" output/logs/daemon-*.log | sort | uniq -c

# Anti-fraud trigger count
grep -c "no_invite_permission" output/logs/daemon-*.log

# Survival rate for the past week (requires gpt-team DB)
sqlite3 /path/to/gpt-team/db/database.sqlite \
    "SELECT
       SUM(CASE WHEN is_banned=1 THEN 1 ELSE 0 END) AS banned,
       SUM(CASE WHEN is_banned=0 THEN 1 ELSE 0 END) AS alive,
       COUNT(*) AS total
     FROM gpt_accounts
     WHERE created_at > datetime('now', '-7 days')"
```### See hCaptcha Failure Reasons```bash
# List the most recent failures
ls -lt /tmp/hcaptcha_auto_solver_live/checkcaptcha_fail_*.json | head -5

# View the decision process
cat /tmp/hcaptcha_auto_solver_live/round_05.json | jq .

# Count failed question types
for f in /tmp/hcaptcha_auto_solver_live/round_*.json; do
    jq -r 'select(.result == "fail") | .prompt' "$f"
done | sort | uniq -c | sort -rn
```### See Where PayPal Gets Stuck```bash
# List screenshots from each stage
ls /tmp/paypal_*.png

# Stage distribution
ls /tmp/paypal_*.png | sed 's/.*paypal_//;s/_[0-9]*\.png//' | sort | uniq -c
```---

## Offline / Mock Debugging

### Offline Playback (No Real Requests)```bash
python CTF-pay/card.py auto --config CTF-pay/config.offline-replay.json --offline-replay
```# Capture and Replay from `flows/`, No Internet Required. Suitable for Debugging Internal Logic in `card.py`.

### Local Mock Gateway```bash
python CTF-pay/card.py auto --config CTF-pay/config.local-mock.json --local-mock
```# Start Local HTTP Server to Simulate Stripe State Machine

You can select scenarios:

- `challenge_pass_then_decline`: challenge passes but card is declined in final state
- `challenge_failed`: challenge fails directly
- `no_3ds_card_declined`: card is declined without entering 3DS

Suitable for debugging challenge / 3DS state machine logic, no need for real cards / real proxies.

---

## Packet Capture Analysis```bash
# Parse mitmproxy flows file
python -c "
from mitmproxy.io import FlowReader
for f in FlowReader(open('flows', 'rb')).stream():
    print(f.request.method, f.request.pretty_url)
"

# Find Stripe protocol chain
python -c "
from mitmproxy.io import FlowReader
for f in FlowReader(open('flows', 'rb')).stream():
    if 'stripe.com' in f.request.pretty_url:
        print(f.request.method, f.request.pretty_url, '→', f.response.status_code)
"

# Dump body of a specific endpoint
python -c "
from mitmproxy.io import FlowReader
for f in FlowReader(open('flows', 'rb')).stream():
    if '/v1/setup_intents/' in f.request.pretty_url and 'confirm' in f.request.pretty_url:
        print(f.request.get_text())
"
```---

## "I didn't change anything, but it suddenly stopped working"

Most common causes (sorted by probability):

1. **Stripe changed runtime fingerprint**: `runtime.version` / `js_checksum` / `rv_timestamp` drifted
2. **OpenAI changed OAuth flow**: URL parameters changed, new step added
3. **PayPal changed DOM**: selectors broke
4. **hCaptcha released new challenge type**: solver throws `unknown_prompt`
5. **Proxy got flagged**: switch IP

Check in this order. Issues 1 and 4 are most likely already open in the issue tracker, check there first.

---

## Before submitting an issue

Prepare the following information according to the [`bug_report.yml` template](../.github/ISSUE_TEMPLATE/bug_report.yml):

1. Complete stack trace (redacted)
2. Last 50 lines of `output/logs/card.log`
3. Last screenshot before error (`/tmp/*.png`)
4. `pip freeze | grep -E "playwright|camoufox|curl_cffi|requests"`
5. Your run mode and command line arguments

**Redaction checklist**: Always censor logs/screenshots before posting. Redact tokens, cookies, real email addresses, IPs, and all PII.