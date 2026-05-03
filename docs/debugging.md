# 调试手册

[← 回到 README](../README.md)

跑出问题时按这里的清单一项项排。

---

## 日志位置

```bash
# 完整 pipeline 日志
tail -f output/logs/card.log

# Daemon 主日志
tail -f output/logs/daemon-*.log

# hCaptcha solver 每轮产物
ls -lah /tmp/hcaptcha_auto_solver_live/

# PayPal 浏览器各阶段截图
ls -lah /tmp/paypal_*.png

# 二次 OAuth 登录截图
ls -lah /tmp/rt_*.png

# Daemon 状态
cat SQLite runtime_meta[daemon_state] | jq .
```

---

## 常见异常

### `CheckoutSessionInactive`

Stripe session 失活了。Stripe 的 checkout session 默认 24 小时过期，长跑流程或机器睡了一觉再跑就会撞上。

**自动恢复**：config 里设 `auto_refresh_on_inactive: true`，`card.py` 会自动重新生成 fresh checkout 续跑。

```json
"fresh_checkout": {
  "auto_refresh_on_inactive": true
}
```

### `ChallengeReconfirmRequired`

hCaptcha 结果失效。hCaptcha token 有 TTL（约 2 分钟），在 confirm 之前耽搁太久就会过期。

**手动恢复**：重跑 confirm 阶段。

**根本解法**：调 daemon 的 `jitter_before_run_s` 不要太长，或者在 confirm 之前不要做其他耗时操作。

### `FreshCheckoutAuthError`

ChatGPT 侧拒绝你的 auth 凭证。可能原因：

- `access_token` 过期
- `session_token` 失效
- 账号被 ban / 被禁用
- 账号触发 `add-phone` 墙

**排错**：

```python
# 直接调一次 /api/auth/session 看响应
import requests
r = requests.get(
    "https://chatgpt.com/api/auth/session",
    headers={"Cookie": "__Secure-next-auth.session-token=..."}
)
print(r.status_code, r.json())
```

如果是 401 / token_invalidated → 重新注册或刷新 session_token。
如果是 401 / account_deactivated → 这号死了，换号。

### `DatadomeSliderError`

PayPal 的 DataDome 滑块解算失败。

**排错**：

```bash
# 看最近一次失败的截图
ls -lt /tmp/paypal_ddc_*.png | head -1

# 看 solver 决策（如果 daemon 模式）
grep "DatadomeSliderError" output/logs/daemon-*.log | tail -5
```

**daemon 行为**：daemon 会重跑当前轮，**不**消耗 IP burn 配额。

**手动调试**：

```python
# 在 card.py::_try_solve_ddc_slider 里临时加 page.pause() 暂停浏览器
# 然后 headless=False 跑一次看 DOM 长啥样
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
