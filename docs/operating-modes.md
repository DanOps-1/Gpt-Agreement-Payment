# 运行模式

[← 回到 README](../README.md)

四种模式都用同一个 `pipeline.py` 入口，靠命令行参数切换。

---

## 单次

```bash
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal
```

注册 → 支付 → OAuth → 写 SQLite 运行时库（`output/webui.db`）。整体五分钟左右。

调试时最常用的模式。每次跑一遍完整链路看哪里挂。

---

## 批量并行

```bash
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal \
    --batch 10 --workers 3
```

| 参数 | 含义 |
|---|---|
| `--batch N` | 跑 N 个完整 pipeline |
| `--workers M` | 并行 M 个 worker |

> ⚠️ **并行不是免费的**。同一时间窗口内多个号注册会触发批次关联，反欺诈层面看就是一个 cohort。建议 `--workers` 不超过 3，且每个 worker 用不同的代理 / 域 / 时间抖动。详见 [`anti-fraud-research.md`](anti-fraud-research.md)。

---

## 自产自销（self-dealer）

最省钱的玩法。一次 PayPal 扣款换来 1 owner + N member 的完整 Team workspace。每个 member 都是独立 ChatGPT 账号，可以单独换 OAuth `refresh_token`。

```bash
# 1 owner + 4 member
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal --self-dealer 4

# 复用已付费 owner，跳过 Step 1 省一次扣款
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal \
    --self-dealer 4 --self-dealer-resume <owner_email>
```

### 内部时序

| 阶段 | 次数 | 复用 | 产出 |
|---|---|---|---|
| 1. 注册 → 支付 → 推下游 | 1 (owner) | `pipeline()` / `card.py` | `team_id` + owner refresh_token |
| 2. 注册 → 邀请 → 接受邀请 → 推下游 | N (member) | `register()` + invites API + `_exchange_refresh_token_with_session` | N 个 member refresh_token，全绑同一 `team_id` |

### Member 循环单次时序（约 3 分钟稳态）

1. 挑代理 + 挑子域 + 写临时 `cardw` 配置（跟单 owner pipeline 一致）
2. `register()` —— Camoufox 过 Turnstile + CF KV 取 OTP（≈ 1 分钟）
3. owner 的 Bearer 调 `POST /backend-api/accounts/{team_id}/invites`（< 1 秒）
4. member 的 Bearer 调 `POST /backend-api/accounts/{team_id}/invites/accept`（< 1 秒）
5. `card._exchange_refresh_token_with_session` —— Camoufox 重登（email + password + consent）拿 refresh_token（约 30 秒）
6. 按 SQLite runtime 追加记录 → 推下游

### 关键 API（从 chatgpt.com 前端 JS 逆出来的）

```
POST https://chatgpt.com/backend-api/accounts/{team_id}/invites
   ↓ owner Bearer，body: {emails: ["target@..."]}

POST https://chatgpt.com/backend-api/accounts/{team_id}/invites/accept
   ↓ invitee Bearer，前端 JS `4813494d-*.js` 里 /accounts/{account_id}/invites/accept
```

### 安全机制（复用 owner pipeline）

- 每个 member 单独挑 `proxy_pool.pick()` + `domain_pool.pick()` + 临时 cardw 配置，不关联
- 任何单个 member 任何一步失败（注册 / 邀请 / 接受 / 重登 / CPA）都被 try/except 捕获，继续下一个
- `--self-dealer-resume` 读 SQLite 里既有 owner（已付费）的 `team_account_id` + `refresh_token`，避免重复扣款

### 每个 member 两次 Codex OAuth（第一次必失败）

- **第一次**：`browser_register.py` 注册流程末尾的 signup 态 hydra session。**必失败**（`token_exchange_user_error`）。默认 `SKIP_SIGNUP_CODEX_RT=1` 跳过；想看旧路径设 `0`
- **第二次**：Camoufox 重登（login 态 full user session）—— 成功拿 RT

---

## Daemon 模式 —— 持续维护补号池

让 pipeline 作为常驻后台服务，自动维护外部 [gpt-team](https://github.com/DanOps-1/gpt-team) **补号池**（`account_usage='recovery'`）的可用账号数始终 ≥ 目标值。

```bash
# 后台跑（推荐，-u 关 buffer 让日志实时写）
(nohup xvfb-run -a python3 -u pipeline.py --config CTF-pay/config.paypal.json --paypal --daemon \
    > output/logs/daemon-$(date +%Y%m%d-%H%M%S).log 2>&1 &)

# 跟日志
tail -f output/logs/daemon-*.log

# 前台跑（调试用，Ctrl+C 优雅退出）
xvfb-run -a python3 -u pipeline.py --config CTF-pay/config.paypal.json --paypal --daemon

# 看状态
cat SQLite runtime_meta[daemon_state] | jq .

# 优雅停（跑完当前周期再退）
pkill -TERM -f "pipeline.*--daemon"

# 强杀（连 Camoufox + Xvfb 残留一起清）
pkill -9 -f "pipeline.*--daemon"
pkill -9 -f camoufox-bin
pkill -9 -f browser_register
for pid in $(pgrep Xvfb); do kill -9 $pid; done
```

### 工作循环

```
loop:
    sleep poll_interval_s

    if rate_limit 卡了:
        continue

    usable = 查 gpt-team DB（!isBanned && !isDisabled && !noInvitePermission && !expired && seat 未满）

    if usable < target_ok_accounts:
        try:
            ensure_gost_alive()       # 看门狗
            cleanup_temp_leftovers()  # /tmp 孤儿
            if 该清 CF 了:
                cleanup_dead_cf()
            run_pipeline()
            清状态机标志（如果 invite=ok）
        except:
            按异常类型走对应自愈分支
```

### 12 路自愈环

详见 [`daemon-mode.md`](daemon-mode.md)。

---

## 仅注册 / 仅支付

调试用：把流程拆开跑：

```bash
# 只注册
python pipeline.py --register-only --cardw-config CTF-reg/config.paypal-proxy.json

# 仅支付（用 SQLite 里最新账号）
xvfb-run -a python pipeline.py --pay-only --config CTF-pay/config.paypal.json --paypal
```

---

## 直接调 card.py

跳过 pipeline 编排器，直接调 card.py：

```bash
# 标准卡支付（auto_register 模式）
python CTF-pay/card.py auto --config CTF-pay/config.auto.json

# 从已有 checkout session 续跑
python CTF-pay/card.py cs_live_xxx --config CTF-pay/config.auto.json

# 用第 N 张卡（0-based）
python CTF-pay/card.py auto --card 1 --config CTF-pay/config.auto.json

# 离线回放（不发外部请求）
python CTF-pay/card.py auto --config CTF-pay/config.offline-replay.json --offline-replay

# 本地 mock gateway
python CTF-pay/card.py auto --config CTF-pay/config.local-mock.json --local-mock

# 重复刷拒卡终态
python CTF-pay/retry_house_decline.py cs_live_xxx --attempts 5
```

---

## 实测耗时优化开关

基于近期 daemon + self-dealer 全量日志，两条 100% 失败的路径已加开关默认跳过：

| 环境变量 | 默认 | 节省 | 说明 |
|---|---|---|---|
| `SKIP_SIGNUP_CODEX_RT` | `1` | ~30s/注册 | signup 态 hydra session 无法换 Codex RT（`token_exchange_user_error`），refresh_token 由后续 `_exchange_refresh_token_with_session`（pay / self-dealer 重登）拿 |
| `SKIP_HERMES_FAST_PATH` | `1` | 5–10s/支付 | PayPal 对非浏览器 cookied session 返 `/checkoutweb/genericError?code=REVGQVVMVA`（"DEFAULT"），所有支付实际都走浏览器路径 |

两个开关默认开启（跳过必失败路径），想对比或 PayPal 改协议时可设 `SKIP_*=0` 恢复旧行为。
