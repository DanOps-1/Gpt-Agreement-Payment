# Pull Request

> ⚠️ **维护者无法手动复现你的 PR**。所以这个模板里要求的"跑通证据"不是形式主义，是 PR 能不能合的硬条件。证据缺了，PR 直接关，不解释。
>
> 提 PR 之前请读完 [`CONTRIBUTING.md`](../CONTRIBUTING.md) 和 [`NOTICE`](../NOTICE)。

---

## 1. 改动类型（必填）

请勾选这个 PR 属于哪一类（**只能选一类**，不同类要求的证据不同）：

- [ ] 🐛 **Bug fix** — 修了某个会让现有用户配置失效的行为
- [ ] ✨ **New feature** — 加了不破坏现有 API 的新功能
- [ ] 💥 **Breaking change** — 改动会让现有配置失效（必须在最后一节说明迁移路径）
- [ ] 🧠 **hCaptcha solver** — 加了新题型 / 改进现有 solver
- [ ] 🔄 **协议适配** — Stripe / PayPal / OpenAI 出 breaking change 后的修补
- [ ] 🛡️ **Daemon 自愈环** — 加了新失败模式的自愈分支
- [ ] 🔬 **研究内容** — 向 `docs/anti-fraud-research.md` 加新数据 / 新结论
- [ ] 📚 **文档** — README / docs/* / inline 注释
- [ ] 🌍 **翻译** — 中英互译，不改行为
- [ ] 🔧 **工具链 / CI** — 不改运行时行为

---

## 2. 改动摘要（必填）

<!-- 一两句话说清楚做了什么 -->



**关闭 issue（如适用）**：Closes #

---

## 3. 详细说明 + 设计决策（必填，至少 100 字）

<!--
回答下面三件事：
1. 当前实现为什么不够？做这个 PR 想解决的具体问题是什么？
2. 为什么选这个方案而不是其他方案？考虑过哪些 trade-off？
3. 这个 PR 改了哪些行为？哪些没改？影响范围有多大？

如果是协议适配类：附 Stripe / PayPal / OpenAI 的具体行为变化，比如 endpoint 变了、字段名变了、新增了步骤等。
如果是 solver 题型：附 prompt 文本、出现频率、为什么现有 solver 兜不住。
-->



---

## 4. 跑通证据（**必填**，按你 PR 类型不同要求不同）

> 维护者只能根据这一节的内容判断 PR 是否合理。**证据不足的 PR 不会被合并。**

### A. 通用要求（所有 PR 都要）

- [ ] **本地跑过这个 PR 的代码**（不是只通过 review 改的字面）
- [ ] **没引入新的真实凭证 / IP / 域名 / PII** —— `git diff main...HEAD` 自查过

### B. 按类型补充证据（根据 §1 选的类型勾对应分支）

#### 🐛 Bug fix / ✨ New feature / 💥 Breaking change

- [ ] **复现命令**（reviewer 能直接 copy 跑的）：
  ```bash
  # 你修复 / 实现的功能怎么触发？
  ```
- [ ] **修复前的失败日志**（脱敏后贴 30–50 行）：
  <details><summary>失败日志</summary>

  ```
  贴这里
  ```
  </details>
- [ ] **修复后的成功日志**（同样脱敏）：
  <details><summary>成功日志</summary>

  ```
  贴这里
  ```
  </details>
- [ ] **描述边界情况**：哪些场景测了？哪些没覆盖？

#### 🧠 hCaptcha solver

- [ ] **题型 prompt 文本**（完整字符串）：`______`
- [ ] **典型 challenge 截图** 1 张（脱敏后，附在 PR 评论区拖图上传）
- [ ] **`/tmp/hcaptcha_auto_solver*/round_XX.json`** 内容（成功一轮，截最关键的字段）：
  <details><summary>round JSON</summary>

  ```json
  贴这里
  ```
  </details>
- [ ] **`checkcaptcha_pass_*.json`** 至少 1 份（证明真过了）
- [ ] **样本量**：你跑了多少次？通过率是多少？
  - 跑了 ___ 次，通过 ___ 次，通过率 ___%
- [ ] **VLM 路径 + 启发式路径都测过了**（如果新加了启发式 solver）

#### 🔄 协议适配（Stripe / PayPal / OpenAI 改了）

- [ ] **抓包对比**（mitmproxy / Burp 都行）：
  - 旧响应：附 1 段
  - 新响应：附 1 段
- [ ] **服务方变更通知 / 公告链接**（如果有）：______
- [ ] **本地完整跑一次 pipeline 的最后日志**（证明确实跑通到 `state=succeeded` 或类似终态）：
  <details><summary>pipeline 日志</summary>

  ```
  贴这里
  ```
  </details>

#### 🛡️ Daemon 自愈环

- [ ] **触发场景描述**：什么情况下会触发？多久触发一次？
- [ ] **触发那一刻的日志**（30 行）：
  <details><summary>触发日志</summary>

  ```
  贴这里
  ```
  </details>
- [ ] **自愈成功后的下一轮跑通日志**（证明恢复有效）：
  <details><summary>恢复后日志</summary>

  ```
  贴这里
  ```
  </details>
- [ ] **状态机变化**：`output/daemon_state.json` 在恢复前后的字段差异

#### 🔬 研究内容

- [ ] **数据采集时间窗口**：______
- [ ] **样本量 + 实验设置**：清楚说明对照组、变量
- [ ] **脱敏检查**（按 [`CONTRIBUTING.md`](../CONTRIBUTING.md#研究内容贡献的脱敏清单)）：
  - [ ] IP 全用 RFC 5737 段（`203.0.113.x` / `198.51.100.x` / `192.0.2.x`）
  - [ ] 域名全用 `*.example` / `*.example.com`
  - [ ] ASN / ISP 名用 `AS-XX` / `ISP-A`
  - [ ] 没有真实账号 / token / cookie
- [ ] **结论是否推翻 / 修正了 README 现有的某条结论？** 列出来

#### 📚 文档 / 🌍 翻译 / 🔧 工具链

- [ ] **改动后能正常渲染**（至少在 GitHub 网页 preview 看过）
- [ ] **不破坏现有锚点链接**（README 和 docs/ 之间的内部跳转还能用）
- [ ] **如果是 CI 改动**：本地跑通过；附 GitHub Actions URL 或本地输出

---

## 5. Reviewer 怎么本地验证（必填）

<!--
请写出 reviewer 拿到这个分支后，能照着复现的最短步骤。

不要写"按 README 跑就行了" —— 写具体到 reviewer 能 copy-paste 的命令。

例：
1. checkout 这个分支
2. cp CTF-pay/config.paypal.example.json CTF-pay/config.paypal.json
3. 填上 PayPal 凭证 + 代理 URL
4. 设环境变量 SKIP_HERMES_FAST_PATH=0
5. 跑：xvfb-run -a python pipeline.py --pay-only --paypal --debug
6. 预期看到日志里有 "[B6 hermes] hit fast path: status=200"
-->



---

## 6. 测了什么 / 没测什么（必填）

### 已测场景

- [ ] ______
- [ ] ______

### 已知没覆盖的边界

<!-- 诚实写出来，比假装"全部测过"靠谱 -->

- ______
- ______

### Breaking change 迁移路径（如果勾了 §1 的"💥 Breaking change"）

<!--
- 哪个 config 字段名改了？
- 现有用户怎么迁移？
- 是否需要在下一个 release 的 changelog 里高亮？
-->



---

## 7. 脱敏检查（**必填强制勾选**）

- [ ] 我搜过本 PR 所有改动里的真实 IP，全部用 `203.0.113.x` / `198.51.100.x` / `192.0.2.x` 替换了
- [ ] 我搜过所有改动里的真实域名，全部用 `*.example` 替换了
- [ ] 我搜过所有改动里的真实邮箱、token、cookie、卡号，全部脱敏或删除了
- [ ] 我跑过 `git diff --cached` 检查没有真实凭证带进 stage
- [ ] 本 PR 描述里贴的所有日志 / 截图都已脱敏

---

## 8. 授权与 License 确认（**必填强制勾选**）

- [ ] 我贡献的内容是我自己写的，或者来自符合 MIT 兼容许可证的来源
- [ ] 我同意贡献按本项目的 [MIT License](../LICENSE) 发布
- [ ] 我已经读过并接受 [`NOTICE`](../NOTICE) 的全部条款
- [ ] 我已经读过 [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md)
- [ ] 这个 PR **不是**为了协助任何针对未授权目标的使用
- [ ] 这个 PR 涉及的研究 / 数据 / 实验都是在我自己拥有或明确授权的目标上做的

---

## 9. 其他（可选）

<!-- 任何想让 reviewer 注意的点：未来计划、相关 PR 链接、引用的论文等 -->



---

> 提交前最后检查：上面所有 **必填** 项都填了？所有 **强制勾选** 项都勾了？跑通证据按你的 PR 类型给齐了？没填全的 PR 会被打 `needs-info` 标签，超过 14 天没补全自动关闭。
