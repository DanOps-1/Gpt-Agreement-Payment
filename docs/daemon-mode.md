# Daemon 自愈链

[← 回到 README](../README.md)

Daemon 是按"无人值守跑数周"的目标设计的。十二路自愈环全是实战里遇到过的失败模式，不是想出来的。

---

## 启动 / 停止 / 看状态

```bash
# 后台跑（推荐）
(nohup xvfb-run -a python3 -u pipeline.py --config CTF-pay/config.paypal.json --paypal --daemon \
    > output/logs/daemon-$(date +%Y%m%d-%H%M%S).log 2>&1 &)

# 跟日志
tail -f output/logs/daemon-*.log

# 看状态
cat SQLite runtime_meta[daemon_state] | jq .

# 优雅停（跑完当前周期再退）
pkill -TERM -f "pipeline.*--daemon"

# 强杀
pkill -9 -f "pipeline.*--daemon"
pkill -9 -f camoufox-bin
pkill -9 -f browser_register
for pid in $(pgrep Xvfb); do kill -9 $pid; done
```

---

## 工作循环

```
loop:
    sleep poll_interval_s

    if rate_limit 卡了:
        continue

    usable = 查 gpt-team DB（!isBanned && !isDisabled && !noInvitePermission && !expired && seat 未满）

    if usable >= target_ok_accounts:
        continue   # 满了，下一轮再看

    try:
        ensure_gost_alive()         # 看门狗
        cleanup_temp_leftovers()    # /tmp 孤儿
        if 该清 CF 了:
            cleanup_dead_cf()
        run_pipeline()
        清自愈标志（如果 invite=ok）
    except:
        按异常类型走对应自愈分支
        累计 consecutive_failures，超阈值进冷却
```

---

## 12 路自愈环

| 触发 | 检测 | 恢复 |
|---|---|---|
| **DataDome 滑块** | `[B-DDC]` / `[B6]` iframe 出现 slider DOM | Playwright 用 `smoothstep` 缓动 + 随机抖动模拟人类拖 |
| **PayPal 预填邮箱** | 邮箱步骤 `<input disabled value="...">` | 跳过填写，直接点 Next 进密码步骤 |
| **连续 `no_invite_permission`** | `webshare.refresh_threshold`（默认 2） | Webshare API 换 IP → 切 gost upstream → 同步下游代理 |
| **zone 内 IP 轮换耗尽** | `zone_rotate_after_ip_rotations`（默认 2） | 切 active CF zone，下次 provision 走另一个 zone |
| **Webshare 配额耗尽** | HTTP 402 / 429 | 标 `webshare_rotation_disabled`，进 `no_rotation_cooldown_s`（默认 3h）冷却 |
| **`invite=ok` 自愈** | 一次成功的注册 | 清 `ip_no_perm_streak` + `zone_ip_rotations` + `webshare_rotation_disabled` |
| **`/tmp` 孤儿清理** | daemon 启动 + 每轮 pipeline 跑完 | 回收 30 分钟以上的 `chatgpt_reg_*` Camoufox profile / `xvfb-run.*` 目录 |
| **CF DNS 配额（Free 200/zone）** | 每 `cf_cleanup_every_n_runs` 轮（默认 30） | 拿 `gpt-team` DB 跟 CF 当前记录 diff，删 banned/disabled/expired/孤儿记录 |
| **多 zone 配额 fallback** | CF 返 `400 quota exceeded` | 自动试 `zone_names` 的下一个 zone |
| **CPA 自动导入** | 支付成功 | 刷 OAuth token → `POST /v0/management/auth-files` 推到 CPA 服务器 |
| **gost 中继挂掉** | 监听端口没绑 | 杀进程，重新拉 Webshare 代理，重启中继 |
| **OAuth `add-phone` 墙** | 免费账号二次登录被重定向到 `/add-phone` | 记日志跳过 |

---

## 详细分支

### 1. DataDome 滑块自动拖拽

PayPal 在 B-DDC 阶段（设备指纹采集）和 B6 阶段（hermes）有概率出现 DataDome 滑块。Daemon 检测到 slider DOM 后自动拖：

```python
# 简化版逻辑
def _try_solve_ddc_slider(page):
    # 找 slider iframe（src 含 ddc/captcha/datadome）
    for frame in page.frames:
        if any(k in frame.url for k in ("ddc", "captcha", "datadome")):
            slider = frame.query_selector('[class*="slider"]')
            if slider:
                box = slider.bounding_box()
                # smoothstep 缓动 + 抖动
                animate_drag(box, distance=240, duration_ms=800, jitter=True)
                return True
    return False
```

不消耗 IP burn 配额。失败抛 `DatadomeSliderError`，daemon 重跑当前轮（不切 IP）。

### 2. Webshare API 自动换 IP

```python
def _rotate_webshare_ip():
    # 1. 调 Webshare API 换一个新代理
    new_proxy = webshare.refresh_pool(country="US")

    # 2. 杀掉本地 gost，新起一个指向新 upstream
    _swap_gost_relay(port=18898, upstream=new_proxy)

    # 3. 同步 gpt-team 全局代理设置
    if cfg.webshare.sync_team_proxy:
        team_system.update_global_proxy(new_proxy)
```

触发条件：连续 N 次（`refresh_threshold`，默认 2）注册返 `no_invite_permission`。

### 3. 多 zone 域池 fallback

CF Free plan 每 zone 200 条 DNS 记录上限。一个 zone 用满后：

```python
try:
    provisioner.set_active_zone(current_zone).provision()
except CloudflareQuotaExceeded:
    # 切下一个 zone
    next_zone = zone_pool.next(current_zone)
    state["current_zone"] = next_zone
    state["zone_ip_rotations"] = 0
    state["total_zone_rotations"] += 1
    provisioner.set_active_zone(next_zone).provision()
```

也可以是 IP 维度触发：同 zone 内换够 N 个 IP 还是不行（说明这 zone 整个被打标），切下一个 zone。

### 4. CF DNS 死子域清理

CF Free plan 200 record/zone 上限。daemon 跑一段时间后会积累大量"已封号但 DNS 记录还在"的子域，吃光配额。

每 `cf_cleanup_every_n_runs` 轮（默认 30）跑一次清理：

```python
def _cleanup_dead_cf_subdomains():
    # 1. 拿 gpt-team DB 里 banned/disabled/expired/no_invite_permission 的账号邮箱
    dead_emails = query_dead_accounts(gpt_team_db_path)
    dead_subdomains = {e.split("@")[1] for e in dead_emails}

    # 2. 列 CF 当前所有 catch-all 子域记录
    cf_records = provisioner.list_subdomains()

    # 3. 找孤儿（CF 上有但 gpt-team DB 里没记录的）
    orphans = cf_records - all_known_subdomains_from_db()

    # 4. 删交集 + 孤儿
    to_delete = (cf_records & dead_subdomains) | orphans
    for sub in to_delete:
        provisioner.delete_record(sub)
```

某次跑这一步释放了 154 条记录。

### 5. tmpfs 孤儿回收

`/tmp` 在大多数发行版是 tmpfs（RAM 后端，~1 GB）。Camoufox profile 跑完没退干净就会留孤儿目录。daemon 启动 + 每轮跑完都清一次：

```python
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
```

### 6. gost 中继看门狗

gost 偶尔会自己挂（OOM / 网络异常）。daemon 启动时 + 每轮 pipeline 前都检查：

```python
def _ensure_gost_alive(port=18898):
    if not is_port_listening(port):
        # 重新从 Webshare 拉一个代理
        proxy = webshare.refresh_pool()
        _swap_gost_relay(port, proxy)
        if cfg.webshare.sync_team_proxy:
            team_system.update_global_proxy(proxy)
```

### 7. RegistrationError 分类

注册失败时区分**基础设施失败**和**反欺诈失败**：

```python
INFRA_KEYWORDS = (
    "InvalidIP", "geoip", "cannot open display",
    "proxy", "socks5", "camoufox", "connection refused"
)

def classify_registration_error(exc):
    msg = str(exc).lower()
    if any(k in msg for k in INFRA_KEYWORDS):
        return "infra"  # 不计入 zone_reg_fail_streak
    return "domain_risk"
```

只有 `domain_risk` 类失败才累计 `zone_reg_fail_streak`，避免基础设施抖动误触发 zone 切换。

### 8. CPA 自动导入

支付成功后自动推到外部 CPA 系统：

```python
def _cpa_import_after_team(record):
    rt = record["refresh_token"]
    # 用 RT 换一次 access_token
    at = exchange_codex_refresh_token(rt, client_id=cfg.cpa.oauth_client_id)
    # 推 CPA
    requests.post(
        f"{cfg.cpa.base_url}/v0/management/auth-files?name={record['email']}",
        headers={"Authorization": f"Bearer {cfg.cpa.admin_key}"},
        json={"access_token": at, "refresh_token": rt, "plan_tag": cfg.cpa.plan_tag},
    )
```

CPA host 在 CF 后面时用 `curl_cffi` 而不是 `requests`，绕 CF WAF。

---

## 状态持久化

`SQLite runtime_meta[daemon_state]` 重启续跑用：

```json
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
```

进程重启时 `started_at` 会被重置，其他字段全部保留。

---

## 限流保护

```json
"daemon": {
  "rate_limit": { "per_hour": 0, "per_day": 0 },
  "max_consecutive_failures": 5,
  "consecutive_fail_cooldown_s": 1800,
  "jitter_before_run_s": [60, 180]
}
```

| 字段 | 含义 |
|---|---|
| `rate_limit.per_hour / per_day` | 0 = 无限。设了之后超额就 sleep 到下一窗口 |
| `max_consecutive_failures` | 连续失败 N 次进冷却 |
| `consecutive_fail_cooldown_s` | 冷却时长 |
| `jitter_before_run_s [min, max]` | 每次跑前的随机抖动，避免精确间隔被识别 |

---

## 调试 daemon

```bash
# 实时跟日志
tail -f output/logs/daemon-*.log

# 找特定关键词
grep -E "ROTATE|FAILED|SUCCEEDED" output/logs/daemon-*.log | tail -50

# 看每个 IP 的命中率
grep "current_proxy_ip" output/logs/daemon-*.log | sort | uniq -c

# 看哪个 zone 用得多
grep "current_zone" output/logs/daemon-*.log | sort | uniq -c

# 看反欺诈触发次数
grep -c "no_invite_permission" output/logs/daemon-*.log
```

---

## 跑数周的实测建议

- 跟日志写到磁盘文件，不是 stdout（journal 会被 systemd 截断）
- 配 logrotate 切日志：

```
# /etc/logrotate.d/Gpt-Agreement-Payment
/path/to/Gpt-Agreement-Payment/output/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
```

- 配监控（Prometheus / Telegram bot 拉 `SQLite runtime_meta[daemon_state]`），关键指标：
  - `total_succeeded` / `total_attempts` 比例
  - `consecutive_failures` 走高
  - `webshare_rotation_disabled = true` 持续超过 1 小时
  - `ip_no_perm_streak` 频繁跳到 2

- 周期性 ssh 上去看 `pgrep camoufox` —— 应该 ≤ 1 个，多了说明有进程没退干净
- `df -h /tmp`：tmpfs 用量超 50% 就是孤儿没清干净，手动 `rm -rf /tmp/chatgpt_reg_*`
