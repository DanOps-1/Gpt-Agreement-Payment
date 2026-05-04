# Debugging Manual

[← Back to README](../README.md)

Follow this checklist item by item when issues occur.

---

## Log Locations

```bash
# Complete pipeline logs
tail -f output/logs/card.log

# Daemon main logs
tail -f output/logs/daemon-*.log

# hCaptcha solver outputs per round
ls -lah /tmp/hcaptcha_auto_solver_live/

# Screenshots of PayPal browser stages
ls -lah /tmp/paypal_*.png

# Screenshots of second OAuth login
ls -lah /tmp/rt_*.png

# Daemon status
cat SQLite runtime_meta[daemon_state] | jq .
```

---

## Common Exceptions

### `CheckoutSessionInactive`

Stripe session has become inactive. Stripe checkout sessions expire after 24 hours by default. This occurs during long runs or if the machine sleeps and resumes.

**Automatic Recovery**: Set `auto_refresh_on_inactive: true` in config. `card.py` will automatically regenerate a fresh checkout and continue.

```json
"fresh_checkout": {
  "auto_refresh_on_inactive": true
}
```

### `ChallengeReconfirmRequired`

hCaptcha result expired. hCaptcha tokens have a TTL (approx. 2 minutes). If delayed too long before confirm, it will expire.

**Manual Recovery**: Rerun the confirm stage.

**Root Cause Solution**: Adjust the daemon's `jitter_before_run_s` to be shorter, or avoid other time-consuming operations before confirm.

### `FreshCheckoutAuthError`

ChatGPT side rejected your auth credentials. Possible causes:

- `access_token` expired
- `session_token` invalidated
- Account banned / disabled
- Account triggered `add-phone` wall

**Troubleshooting**:

```python
# Call /api/auth/session once to check response
import requests
r = requests.get(
    "https://chatgpt.com/api/auth/session",
    headers={"Cookie": "__Secure-next-auth.session-token=..."}
)
print(r.status_code, r.json())
```

If 401 / token_invalidated → Re-register or refresh session_token.
If 401 / account_deactivated → Account is dead, use another.

### `DatadomeSliderError`

PayPal's DataDome slider solving failed.

**Troubleshooting**:

```bash
# View recent failed screenshot
ls -lt /tmp/paypal_ddc_*.png | head -1

# View solver decision (if in daemon mode)
grep "DatadomeSliderError" output/logs/daemon-*.log | tail -5
```

**Daemon Behavior**: The daemon will rerun the current round **without** consuming the IP burn quota.

**Manual Debugging**:

```python
# Add page.pause() temporarily in card.py::_try_solve_ddc_slider to pause the browser
# Then run once with headless=False to inspect the DOM
```

### `WebshareQuotaExhausted`

Webshare API 没有可用替换代理了（套餐每月配额耗尽）。

**daemon 行为**：标 `webshare_rotation_disabled = true`，进 `no_rotation_cooldown_s`（默认 3h）冷却。

**手动恢复**：

```bash
# 升级 Webshare 套餐，或者
# 手动改 SQLite runtime_meta[daemon_state] 把 webshare_rotation_disabled 设 false
jq '.webshare_rotation_disabled = false | .no_perm_cooldown_until = 0' \
   SQLite runtime_meta[daemon_state] > /tmp/state.json && \
   mv /tmp/state.json SQLite runtime_meta[daemon_state]
```

### `socks5 auth not supported`

Camoufox 不吃带 auth 的 socks5。配 gost 中继：

```bash
gost -L=socks5://:18898 -F=socks5://USER:PASS@PROXY_HOST:PORT &
```

config 里 proxy 改成 `socks5://127.0.0.1:18898`。daemon 模式有内置 gost 看门狗自动管理这个进程。

### `cannot open display`

xvfb 没起或 `DISPLAY` 没传：

```bash
# 用 xvfb-run 包一层（推荐）
xvfb-run -a python pipeline.py ...

# 或者手动起 Xvfb
Xvfb :99 -screen 0 1920x1080x24 &
DISPLAY=:99 python pipeline.py ...
```

### `geoip InvalidIP` / Camoufox 报错 `InvalidIP`

通常是 gost 中继挂了，Camoufox 直连出去拿不到合法出口 IP。

**daemon**：`_ensure_gost_alive()` 会自动检测端口失绑并重启 gost。
**单跑**：手动重启 gost：

```bash
pkill gost
gost -L=socks5://:18898 -F=socks5://USER:PASS@HOST:PORT &
```

---

## 诊断命令

### 看跑得怎么样

```bash
# 总成功率
jq -r '.total_succeeded as $s | .total_attempts as $t | "\($s)/\($t) = \($s/$t*100)%"' \
   SQLite runtime_meta[daemon_state]

# 每个 IP 的命中率
grep "current_proxy_ip" output/logs/daemon-*.log | sort | uniq -c | sort -rn

# 每个 zone 的使用次数
grep "current_zone" output/logs/daemon-*.log | sort | uniq -c

# 反欺诈触发次数
grep -c "no_invite_permission" output/logs/daemon-*.log

# 最近一周存活率（需要 gpt-team DB）
sqlite3 /path/to/gpt-team/db/database.sqlite \
    "SELECT
       SUM(CASE WHEN is_banned=1 THEN 1 ELSE 0 END) AS banned,
       SUM(CASE WHEN is_banned=0 THEN 1 ELSE 0 END) AS alive,
       COUNT(*) AS total
     FROM gpt_accounts
     WHERE created_at > datetime('now', '-7 days')"
```

### 看 hCaptcha 失败原因

```bash
# 列最近失败
ls -lt /tmp/hcaptcha_auto_solver_live/checkcaptcha_fail_*.json | head -5

# 看决策过程
cat /tmp/hcaptcha_auto_solver_live/round_05.json | jq .

# 统计失败题型
for f in /tmp/hcaptcha_auto_solver_live/round_*.json; do
    jq -r 'select(.result == "fail") | .prompt' "$f"
done | sort | uniq -c | sort -rn
```

### 看 PayPal 卡在哪

```bash
# 列各阶段截图
ls /tmp/paypal_*.png

# 阶段分布
ls /tmp/paypal_*.png | sed 's/.*paypal_//;s/_[0-9]*\.png//' | sort | uniq -c
```

---

## 离线 / mock 调试

### 离线回放（不发真实请求）

```bash
python CTF-pay/card.py auto --config CTF-pay/config.offline-replay.json --offline-replay
```

从 `flows/` 抓包重放，不出网。适合 debug `card.py` 内部逻辑。

### 本地 mock gateway

```bash
python CTF-pay/card.py auto --config CTF-pay/config.local-mock.json --local-mock
```

启本地 HTTP server 模拟 Stripe 状态机，可以选场景：

- `challenge_pass_then_decline`：challenge 过了但卡终态被拒
- `challenge_failed`：challenge 直接失败
- `no_3ds_card_declined`：不进 3DS 直接拒卡

适合 debug challenge / 3DS 状态机逻辑，不需要真实卡 / 真实代理。

---

## 抓包分析

```bash
# 解析 mitmproxy flows 文件
python -c "
from mitmproxy.io import FlowReader
for f in FlowReader(open('flows', 'rb')).stream():
    print(f.request.method, f.request.pretty_url)
"

# 找 Stripe 协议链路
python -c "
from mitmproxy.io import FlowReader
for f in FlowReader(open('flows', 'rb')).stream():
    if 'stripe.com' in f.request.pretty_url:
        print(f.request.method, f.request.pretty_url, '→', f.response.status_code)
"

# dump 某个 endpoint 的 body
python -c "
from mitmproxy.io import FlowReader
for f in FlowReader(open('flows', 'rb')).stream():
    if '/v1/setup_intents/' in f.request.pretty_url and 'confirm' in f.request.pretty_url:
        print(f.request.get_text())
"
```

---

## "我没改任何东西，但突然不工作了"

最常见原因（按概率排序）：

1. **Stripe 改了 runtime 指纹**：`runtime.version` / `js_checksum` / `rv_timestamp` 漂了
2. **OpenAI 改了 OAuth 流程**：URL 参数变了、新增了一个步骤
3. **PayPal 改了 DOM**：选择器失效
4. **hCaptcha 出了新题型**：solver 抛 `unknown_prompt`
5. **代理被打标**：换 IP

按这个顺序排。1 和 4 在 issue 区开着的概率最大，先去看一下别人有没有遇到。

---

## 提 issue 之前

按 [`bug_report.yml` 模板](../.github/ISSUE_TEMPLATE/bug_report.yml) 准备好以下信息：

1. 完整 stack trace（脱敏后）
2. `output/logs/card.log` 最后 50 行
3. 出错前的最后一张截图（`/tmp/*.png`）
4. `pip freeze | grep -E "playwright|camoufox|curl_cffi|requests"`
5. 你的运行模式和命令行参数

**脱敏检查**：贴日志 / 截图前一定先打码，token、cookie、真实邮箱、IP、PII 全部要遮。
