# 反欺诈实证研究

[← 回到 README](../README.md)

> 本文所有数据均来自真实生产运行。**IP 用 RFC 5737 文档段（`203.0.113.0/24`、`198.51.100.0/24`、`192.0.2.0/24`）占位，域名用 `*.example` 占位**，账号数和时间未做修改 —— 那是研究本身的核心。

---

## 摘要

ChatGPT Team 的反欺诈机制有两个独立的层面，行为差异很大：

1. **Probe 层**（即时）：账号级 / 域级信号，请求时即时评估，按**精确 IP 字符串**和**域字符串**打标
2. **Ban 层**（延迟批次）：批次关联信号，由后端定时任务跑（典型时间窗 `00:00 UTC` 和 `12:00 UTC`），按 `(时间窗口, 支付账号, 出口 IP, 指纹)` 元组打包审查

**单 PayPal + 单 IP + 密集注册 24 小时存活率约 2%。** 这不是技术问题，是批次关联本身的 hard limit。要把存活率显著拉起来，必须降低批次关联：多 PayPal、多 ISP/城市 IP、时间散开、指纹多样化。

---

## 实验 1：45 账号批次，24 小时存活率

### 实验设置

- 单日窗口（约 14 小时跨度）
- **同一个 PayPal 账号**（PayPal-A）
- **同一个出口 IP**（`203.0.113.10`，记为 IP-A）
- **七个相邻子域**（5 个 `*.zone-a.example` + `zone-b.example` + `zone-c.example`）
- 创建总数 **45 个**

### Probe 时统计

| 结果 | 次数 |
|---|---|
| `probe=ok` | 32 |
| `probe=no_permission` | 1（`zone-c.example`，已知持久打标的域） |
| 其他错误 | 12 |

按 probe 时的视角看，似乎"32 个号生效，1 个域有问题"。

### 24 小时存活率

直接查 `gpt_accounts` 表：

| 域 | 创建数 | BAN | 存活 | 平均 `user_count` |
|---|---:|---:|---:|---:|
| `subA1.zone-a.example` | 15 | 15 | 0 | 2.3 |
| `subA2.zone-a.example` | 8 | 8 | 0 | 4.0 |
| `subA3.zone-a.example` | 6 | 5 | **1** | 4.0 |
| `subA4.zone-a.example` | 5 | 5 | 0 | 4.6 |
| `subA5.zone-a.example` | 5 | 5 | 0 | 3.6 |
| `zone-b.example` | 5 | 5 | 0 | 3.6 |
| `zone-c.example` | 1 | 1 | 0 | 1.0 |
| **合计** | **45** | **44** | **1** | — |

**24 小时后存活 1/45 ≈ 2.2%。**

### 座位填充率分布（被封号的）

下游真有用户填进去的号也照样被封：

| user_count | 被封号数 |
|---|---:|
| 5 / 5 | 17 |
| 4 / 5 | 13 |
| 3 / 5 | 4 |
| 2 / 5 | 1 |
| 1 / 5 | 8 |
| 满座 + 1 邀请中 | 1 |

**座位填充率跟存活无关。** 封禁不看下游用没用，看的是创建时刻的关联性。

### 批量 BAN 时间戳

`updated_at` 字段聚合（按 banned 状态变化时间）：

| 时间窗口 | 集中封禁数 |
|---|---:|
| 2026-04-19 12:xx UTC | 29 个 |
| 2026-04-19 07:xx UTC | 4 个 |
| 其他零散 | 11 个 |

**审查跑在固定 cron 上**（推测 `00:00 UTC` 和 `12:00 UTC` 附近）。`07:xx` 那波可能是另一个分批审查窗口。

### 幸存对照

只有一个号活了下来（`subA3.zone-a.example` 4/18 19:03 创建，5 座满）。**这极可能是审查漏网，不是真的稳。** 一个早 5 天创建的号（`subA4.zone-a.example` 4/14 23:19，5+1 座位）至今仍活，说明早批次过了某个观察期反而稳定。

---

## 实验 2：IP 维度对照（双代理）

### 设置

- 同一批 `*.zone-a.example` 子域候选（10 个）
- 两个代理出口对照：
  - 代理 X：`203.0.113.10`（NY，ISP-A，AS-XX）—— **同实验 1 的 IP-A**
  - 代理 Y：`203.0.113.20`（VA，ISP-B，AS-YY）—— 干净 IP
- 同一个 PayPal-A，同一个 Camoufox 指纹

### 结果

| 代理 | 出口 IP | Probe `ok` 次数 | 完成数 | `ok` 率 |
|---|---|---:|---:|---:|
| X（NY） | `203.0.113.10` | 1 | 7 | **14%** |
| Y（VA） | `203.0.113.20` | 3 | 3 | **100%** |

**同一个子域 `subA3.zone-a.example`：从 X 出去 `no_perm`，从 Y 出去 `ok`。**

### 结论

**域不是主因，IP 是主因。** Probe 层的打标针对 IP。

---

## 实验 3：同 ASN 不同 IP

### 设置

切换到代理 Z：`203.0.113.30`，**和 IP-A 同 ISP（ISP-A）、同城市（NY）、同 ASN（AS-XX）**，但 IP 字符串不同。

立即复测同一批子域。

### 结果

| 代理 | 出口 IP | Probe `ok` | 完成数 | `ok` 率 | 触发 `no_perm` 时机 |
|---|---|---:|---:|---:|---|
| X（旧 NY，已打标） | `203.0.113.10` | 1 | 7 | 14% | 第 2 次就拒 |
| **Z（新 NY，干净）** | `203.0.113.30` | **4** | **5** | **80%** | **第 5 次才拒** |

### 结论

**换同 ISP / 同城市 / 同 ASN 内的另一个 IP，立刻恢复"干净状态"。**

→ ChatGPT 打标粒度是**精确 IP 字符串**，不是 ASN / 城市 / ISP。

### IP 寿命

每个 IP 大约能跑 **4–5 次**注册就会翻到 `no_perm` 状态（第 5 次 probe 开始拒）。恢复办法：

- **换 IP**：立即恢复（同 ASN 也行，只要 IP 字符串不同）
- **等几小时**：自然恢复（参考 `subA1.zone-a.example` 短暂 2h 恢复的观察）

---

## 推翻的早期假设

| 早期假设 | 数据 | 推翻 |
|---|---|---|
| 反欺诈粒度是"注册邮箱域" | 五个干净子域同日被清 | ❌ 是批次关联，不按域 |
| `*.zone-a.example` 五个子域全稳 | 五个批次同日被清 | ❌ 这五个域的号同批被清，没被封之前看着都"ok" |
| 同 IP + 同 PayPal + 集中注册不会触发反欺诈（连续 12 次 ok） | 24h 存活 ≈ 2% | ❌ probe=ok 只是即时态 |
| 单日 30+ 不会触发新 burn | 45 个号集中被封 | ❌ 会，只是延迟 12 小时 |
| `probe=no_perm` 是域级打标 | 同域在不同 IP 下结果相反 | ❌ 主导信号是 IP |

---

## 修正后的反欺诈模型

### 两层独立机制

```
┌─────────────────────────────────────────────────────┐
│                Probe 层（即时）                     │
│                                                     │
│  signal = (egress_ip_string, email_domain_string?)  │
│  evaluation = at request time                       │
│  IP 寿命 = ~4-5 次注册                              │
│  域级打标 = 少数域永久不可用                        │
└─────────────────────────────────────────────────────┘
                       ↓
                  注册可能 ok
                       ↓
┌─────────────────────────────────────────────────────┐
│                Ban 层（延迟批次）                   │
│                                                     │
│  signal = (time_window, payment_account,            │
│            egress_ip, fingerprint, ...)             │
│  evaluation = scheduled cron @ ~00:00 / 12:00 UTC  │
│  整批关联 → 整批被封                                │
└─────────────────────────────────────────────────────┘
                       ↓
                次日凌晨 95% 死亡
```

### 关键认知

1. **Probe=ok 不代表稳定。** 只代表 cohort 还没被审到
2. **域级打标是少数情况**（如 `zone-c.example`），适用范围比想象中小
3. **IP 字符串是 probe 层的主导信号**
4. **批次关联是 ban 层的主导信号**，需要降低 cohort 内的维度共享才能改善

---

## 工程层面的影响

### 现有工具能管的

- ✅ DomainPool + permanent_burned 处理 probe 层的"持久打标域"
- ✅ Webshare API + gost 中继处理 IP 寿命问题
- ✅ 多 zone 域池处理 CF 配额 + zone 级风险

### 现有工具管不了的

- ❌ **批次关联导致的次日大灭绝**：daemon 模式 target=20 的库存在一次批量 BAN 后可能只剩 0–1 个，相当于每晚重新起步
- ❌ 域轮换 / 按需开通子域对"次日大灭绝"无效

### 提高 24 小时存活率的方法

要显著降低批次关联（按效果排序）：

1. **多 PayPal 账号池**：每个账号 1–3 单后轮换，拉开时间。这是最强信号
2. **多代理出口 IP，跨 ISP / 跨城市**：不是同一代理商的多个 slot，而是真的不同 ISP
3. **时间分散**：单日 2–3 个，错开几小时，而不是一小时密集 30+
4. **Camoufox 指纹多样化**：`humanize` + 不同 OS/屏幕 profile
5. **降低单 PayPal 复用率**：同一 PayPal 短时间内多次 Team 订阅本身就是强欺诈信号

目前 pipeline 只做到"错开 60–180s 抖动"和"多域轮换"，**其他维度全是同参数**。这是 2% 存活率的根因。

---

## 待验证的方向

下面这几条没有充分实验数据，留给后来人做：

1. **Camoufox 指纹多样化的真实 ROI**：不同 OS/屏幕 profile 是否真能拉开 cohort 距离？
2. **PayPal 信任度衰减**：一个 PayPal 跑过 N 单后存活率变化曲线
3. **时间分散的最佳间隔**：不同 cohort 之间间隔多久才足够"独立"
4. **指纹复用 vs IP 复用 哪个权重更高**：固定其他维度，对照测试
5. **是否存在 "warm-up" 路径**：一个号在 ban 之前先做一些"正常用户"的行为是否能降低被封概率

---

## 复现指引

要复现这些实验：

1. 准备至少 2 个不同 ASN 的代理出口
2. 配 daemon 模式跑一周以上
3. 每天用 sqlite3 直查 `gpt_accounts` 表的 `is_banned` 状态变化
4. 关键查询：

```sql
-- 24h 内被封号的批次特征
SELECT
    DATE(created_at) AS create_day,
    HOUR(updated_at) AS ban_hour,
    proxy_ip,
    payment_account,
    COUNT(*) AS cnt
FROM gpt_accounts
WHERE is_banned = 1
  AND updated_at > created_at + INTERVAL '12 hours'
GROUP BY 1, 2, 3, 4
ORDER BY 1 DESC, 2 DESC;
```

5. 对照组按维度差异分组（同 PayPal vs 不同 PayPal、同 IP vs 不同 IP 等等）
6. 数据脱敏方式参考本文（RFC 5737 IP、`*.example` 域名）

如果你跑出来新数据，欢迎按 [`CONTRIBUTING.md`](../CONTRIBUTING.md#研究内容贡献的脱敏清单) 提 PR 加到本文。

---

## 引用本文

如果你的研究 / 论文 / 博客引用本文中的数据，请引用：

```
Gpt-Agreement-Payment — 反欺诈实证研究. (2026).
https://github.com/DanOps-1/Gpt-Agreement-Payment/blob/main/docs/anti-fraud-research.md
```

或者 BibTeX：

```bibtex
@misc{Gpt-Agreement-Payment-antifraud,
  title  = {Empirical Anti-Fraud Research on ChatGPT Team Subscription},
  author = {Gpt-Agreement-Payment contributors},
  year   = {2026},
  howpublished = {\url{https://github.com/DanOps-1/Gpt-Agreement-Payment}},
  note   = {Licensed under MIT, IP addresses use RFC 5737 placeholders}
}
```
