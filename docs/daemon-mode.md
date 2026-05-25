# Daemon Self-Healing Chain

[← Back to README](../README.md)

Daemon is designed with the goal of "unattended operation for weeks". All twelve self-healing loops are failure modes encountered in practice, not invented ones.

---

## Start / Stop / View Status```bash
# Run in background (recommended)
(nohup xvfb-run -a python3 -u pipeline.py --config CTF-pay/config.paypal.json --paypal --daemon \
    > output/logs/daemon-$(date +%Y%m%d-%H%M%S).log 2>&1 &)

# Follow logs
tail -f output/logs/daemon-*.log

# Check status
cat SQLite runtime_meta[daemon_state] | jq .

# Graceful stop (finish current cycle before exiting)
pkill -TERM -f "pipeline.*--daemon"

# Force kill
pkill -9 -f "pipeline.*--daemon"
pkill -9 -f camoufox-bin
pkill -9 -f browser_register
for pid in $(pgrep Xvfb); do kill -9 $pid; done
```---

## Work Cycle```
loop:
    sleep poll_interval_s

    if rate_limit is throttled:
        continue

    usable = query gpt-team DB (!isBanned && !isDisabled && !noInvitePermission && !expired && seat not full)

    if usable >= target_ok_accounts:
        continue   # full, check again next round

    try:
        ensure_gost_alive()         # watchdog
        cleanup_temp_leftovers()    # /tmp orphans
        if CF cleanup needed:
            cleanup_dead_cf()
        run_pipeline()
        clear self-healing flag (if invite=ok)
    except:
        route to corresponding self-healing branch based on exception type
        accumulate consecutive_failures, enter cooldown if threshold exceeded
```---

## 12-Path Self-Healing Loop

| Trigger | Detection | Recovery |
|---|---|---|
| **DataDome Slider** | `[B-DDC]` / `[B6]` iframe slider DOM appears | Playwright uses `smoothstep` easing + random jitter to simulate human dragging |
| **PayPal Pre-filled Email** | Email step `<input disabled value="...">` | Skip input, click Next directly to password step |
| **Consecutive `no_invite_permission`** | `webshare.refresh_threshold` (default 2) | Webshare API rotate IP → switch gost upstream → sync downstream proxies |
| **IP Rotation Exhausted in Zone** | `zone_rotate_after_ip_rotations` (default 2) | Switch active CF zone, next provision uses another zone |
| **Webshare Quota Depleted** | HTTP 402 / 429 | Mark `webshare_rotation_disabled`, enter `no_rotation_cooldown_s` (default 3h) cooldown |
| **`invite=ok` Self-Healing** | One successful registration | Clear `ip_no_perm_streak` + `zone_ip_rotations` + `webshare_rotation_disabled` |
| **`/tmp` Orphan Cleanup** | Daemon startup + after each pipeline run | Reclaim `chatgpt_reg_*` Camoufox profiles / `xvfb-run.*` directories older than 30 minutes |
| **CF DNS Quota (Free 200/zone)** | Every `cf_cleanup_every_n_runs` runs (default 30) | Diff `gpt-team` DB against current CF records, delete banned/disabled/expired/orphan records |
| **Multi-Zone Quota Fallback** | CF returns `400 quota exceeded` | Auto-try next zone in `zone_names` |
| **CPA Auto-Import** | Payment success | Refresh OAuth token → `POST /v0/management/auth-files` push to CPA server |
| **gost Relay Down** | Listen port unbound | Kill process, re-pull Webshare proxy, restart relay |
| **OAuth `add-phone` Wall** | Free account second login redirected to `/add-phone` | Log and skip |

---

## Detailed Branches

### 1. DataDome Slider Auto-Dragging

PayPal has a chance of showing DataDome slider during B-DDC stage (device fingerprinting) and B6 stage (hermes). The daemon auto-drags after detecting slider DOM:```python
# Simplified logic
def _try_solve_ddc_slider(page):
    # Find slider iframe (src contains ddc/captcha/datadome)
    for frame in page.frames:
        if any(k in frame.url for k in ("ddc", "captcha", "datadome")):
            slider = frame.query_selector('[class*="slider"]')
            if slider:
                box = slider.bounding_box()
                # smoothstep easing + jitter
                animate_drag(box, distance=240, duration_ms=800, jitter=True)
                return True
    return False
```# Does not consume IP burn quota. Throws `DatadomeSliderError` on failure, daemon reruns current round (without switching IP).

### 2. Webshare API automatic IP switching```python
def _rotate_webshare_ip():
    # 1. Call Webshare API to get a new proxy
    new_proxy = webshare.refresh_pool(country="US")

    # 2. Kill local gost and start a new one pointing to the new upstream
    _swap_gost_relay(port=18898, upstream=new_proxy)

    # 3. Sync gpt-team global proxy settings
    if cfg.webshare.sync_team_proxy:
        team_system.update_global_proxy(new_proxy)
```# Trigger Conditions

Trigger condition: Consecutive N times (`refresh_threshold`, default 2) registration returns `no_invite_permission`.

### 3. Multi-zone Domain Pool Fallback

CF Free plan has a limit of 200 DNS records per zone. Once a zone is full:```python
try:
    provisioner.set_active_zone(current_zone).provision()
except CloudflareQuotaExceeded:
    # Switch to the next zone
    next_zone = zone_pool.next(current_zone)
    state["current_zone"] = next_zone
    state["zone_ip_rotations"] = 0
    state["total_zone_rotations"] += 1
    provisioner.set_active_zone(next_zone).provision()
```# Translation

It can also be triggered at the IP dimension: if switching through N IPs within the same zone still doesn't work (indicating the entire zone is blacklisted), move to the next zone.

### 4. CF DNS Dead Subdomain Cleanup

CF Free plan has a 200 record/zone limit. After the daemon runs for a while, it accumulates a large number of "account-banned but DNS records still exist" subdomains, consuming the quota.

Run cleanup every `cf_cleanup_every_n_runs` rounds (default 30):```python
def _cleanup_dead_cf_subdomains():
    # 1. Get email addresses of banned/disabled/expired/no_invite_permission accounts from gpt-team DB
    dead_emails = query_dead_accounts(gpt_team_db_path)
    dead_subdomains = {e.split("@")[1] for e in dead_emails}

    # 2. List all current catch-all subdomain records in CF
    cf_records = provisioner.list_subdomains()

    # 3. Find orphans (exist in CF but not recorded in gpt-team DB)
    orphans = cf_records - all_known_subdomains_from_db()

    # 4. Delete intersection + orphans
    to_delete = (cf_records & dead_subdomains) | orphans
    for sub in to_delete:
        provisioner.delete_record(sub)
```One run of this step released 154 records.

### 5. tmpfs Orphan Cleanup

`/tmp` is tmpfs (RAM-backed, ~1 GB) on most distributions. Camoufox profiles that don't exit cleanly leave orphan directories. Clean up on daemon startup + after each run:```python
def _cleanup_temp_leftovers(max_age_s=1800):
    patterns = [
        "/tmp/chatgpt_reg_*",
        "/tmp/xvfb-run.*",
        "/tmp/pipeline_cardw_*",
        "/tmp/pipeline_pay_*",
    ]
    for pat in patterns:
        for path in glob.glob(pat):
            if os.path.getmtime(path) < time.time() - max_age_s:
                shutil.rmtree(path, ignore_errors=True)
```### 6. gost Relay Watchdog

gost occasionally crashes on its own (OOM / network exceptions). Check at daemon startup + before each pipeline round:```python
def _ensure_gost_alive(port=18898):
    if not is_port_listening(port):
        # Pull a fresh proxy from Webshare
        proxy = webshare.refresh_pool()
        _swap_gost_relay(port, proxy)
        if cfg.webshare.sync_team_proxy:
            team_system.update_global_proxy(proxy)
```### 7. RegistrationError Classification

When registration fails, distinguish between **infrastructure failures** and **anti-fraud failures**:```python
INFRA_KEYWORDS = (
    "InvalidIP", "geoip", "cannot open display",
    "proxy", "socks5", "camoufox", "connection refused"
)

def classify_registration_error(exc):
    msg = str(exc).lower()
    if any(k in msg for k in INFRA_KEYWORDS):
        return "infra"  # Not counted toward zone_reg_fail_streak
    return "domain_risk"
```Only `domain_risk` class failures accumulate `zone_reg_fail_streak`, avoiding infrastructure jitter from triggering zone switching.

### 8. CPA Auto Import

Automatically push to external CPA system after successful payment:```python
def _cpa_import_after_team(record):
    rt = record["refresh_token"]
    # Exchange RT for access_token once
    at = exchange_codex_refresh_token(rt, client_id=cfg.cpa.oauth_client_id)
    # Push to CPA
    requests.post(
        f"{cfg.cpa.base_url}/v0/management/auth-files?name={record['email']}",
        headers={"Authorization": f"Bearer {cfg.cpa.admin_key}"},
        json={"access_token": at, "refresh_token": rt, "plan_tag": cfg.cpa.plan_tag},
    )
```# CPA host Behind CF

Use `curl_cffi` instead of `requests` when CPA host is behind CF, to bypass CF WAF.

---

## State Persistence

`SQLite runtime_meta[daemon_state]` for resuming runs after restart:```json
{
  "started_at": "2026-04-27T03:14:22Z",
  "total_attempts": 761,
  "total_succeeded": 472,
  "total_failed": 289,
  "consecutive_failures": 0,
  "ip_no_perm_streak": 0,
  "current_proxy_ip": "198.51.100.X",
  "total_ip_rotations": 16,
  "webshare_rotation_disabled": false,
  "no_perm_cooldown_until": 0,
  "current_zone": "zone-a.example",
  "zone_ip_rotations": 0,
  "total_zone_rotations": 2,
  "last_stats": {
    "total_active": 44,
    "usable": 38,
    "no_invite_permission": 5
  }
}
```# Process Restart and `started_at`

When a process restarts, `started_at` will be reset, while all other fields are preserved.

---

## Rate Limiting Protection```json
"daemon": {
  "rate_limit": { "per_hour": 0, "per_day": 0 },
  "max_consecutive_failures": 5,
  "consecutive_fail_cooldown_s": 1800,
  "jitter_before_run_s": [60, 180]
}
```| Field | Meaning |
|---|---|
| `rate_limit.per_hour / per_day` | 0 = unlimited. After setting, sleep to next window on excess |
| `max_consecutive_failures` | Enter cooldown after N consecutive failures |
| `consecutive_fail_cooldown_s` | Cooldown duration |
| `jitter_before_run_s [min, max]` | Random jitter before each run to avoid detection of precise intervals |

---

## Debugging daemon```bash
# Real-time log tailing
tail -f output/logs/daemon-*.log

# Search for specific keywords
grep -E "ROTATE|FAILED|SUCCEEDED" output/logs/daemon-*.log | tail -50

# View hit rate for each IP
grep "current_proxy_ip" output/logs/daemon-*.log | sort | uniq -c

# View which zone is used most
grep "current_zone" output/logs/daemon-*.log | sort | uniq -c

# View anti-fraud trigger count
grep -c "no_invite_permission" output/logs/daemon-*.log
```## Weekly Trial Run Recommendations

- Write logs to disk files, not stdout (journal will be truncated by systemd)
- Configure logrotate to rotate logs:```
# /etc/logrotate.d/Gpt-Agreement-Payment
/path/to/Gpt-Agreement-Payment/output/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
```- Set up monitoring (Prometheus / Telegram bot pulls `SQLite runtime_meta[daemon_state]`), key metrics:
  - `total_succeeded` / `total_attempts` ratio
  - `consecutive_failures` spike
  - `webshare_rotation_disabled = true` lasting over 1 hour
  - `ip_no_perm_streak` frequently jumping to 2

- Periodically ssh in to check `pgrep camoufox` — should be ≤ 1, more means processes didn't exit cleanly
- `df -h /tmp`: tmpfs usage over 50% indicates orphaned files weren't cleaned, manually `rm -rf /tmp/chatgpt_reg_*`