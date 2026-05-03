# 贡献指南

谢谢愿意贡献。这是个研究项目，"有用"比"完全合规则"更重要，但下面这些事能让你的 PR 更容易被合。

> ⚠️ **重要：维护者无法在本地手动复现你的 PR。**
>
> 这意味着 PR 能不能合，**完全取决于你提供的"跑通证据"够不够**。这不是形式主义：
>
> - **solver 题型的 PR**：必须附 prompt 文本、`round_XX.json`、`checkcaptcha_pass_*.json`、通过率统计
> - **协议适配的 PR**：必须附抓包对比（before / after）+ 完整跑通到终态的 pipeline 日志
> - **daemon 自愈的 PR**：必须附触发那一刻的日志 + 自愈后下一轮成功的日志 + `SQLite runtime_meta[daemon_state]` 字段差异
> - **bug fix / new feature**：必须附复现命令 + 修复前失败日志 + 修复后成功日志
>
> 详细要求看 [PR 模板](.github/PULL_REQUEST_TEMPLATE.md)，所有"必填"项都得填，所有"强制勾选"项都得勾。**证据缺了，PR 会被打 `needs-info` 标签，14 天没补全自动关闭，不解释。**

## 想要的贡献

按影响力大致排序：

1. **新的 hCaptcha 题型 solver。** 见过 solver 没覆盖的题型就加上去。集成方式在 [`README.md`](README.md#加新题型) —— 三个小函数 + 一个 dispatcher 注册。如果你有公开的 prompt → label 数据集，那更好，PR 里说一下
2. **协议阶段适配。** Stripe / PayPal / OpenAI 大概几周一次 breaking change。打了补丁、跑过几次稳了之后，提个 PR 顺手在 `pipeline.py` changelog 注释里加一行
3. **Daemon 自愈环。** 你**真**遇到过的失败模式，附一段失败日志，别只是脑补。加上检测 + 恢复 + 状态标志清理
4. **逆向笔记。** 逆出新端点（新邀请机制、新管理 API surface 之类），想把文档加到 README 研究段，欢迎。脱敏照现在的写法做：RFC 5737 IP、`*.example` 域名
5. **翻译 / 文档润色。** 大量内联注释还是中文，翻成英文的 PR 接受，前提是不改行为
6. **`flows/` 测试 fixture。** 真实抓包 ship 不了（含 cookies / PII），但脱敏过的 fixture 或者生成 fixture 的工具有用

## 不想要的

- "我针对 $TARGET 跑了下被封了"。读一下 [README 实证段](README.md#-反欺诈实证) —— 被封是预期，研究的就是这个
- 求帮忙针对未授权目标跑工具集。直接关，可能封禁。看 [`SECURITY.md`](SECURITY.md)
- 只是 refactor、没加功能的 PR。`card.py` 故意做成 8000 行单文件，拆完之后 diff 更难追，incidental complexity 也没下降
- 不带正当理由就引入新 ML 模型依赖。ML venv 已经 4 GB 了

## 工作流

1. **改东西大一点先开 issue。** 5 行讨论能省 500 行走错方向的 PR
2. **从 `main` 拉分支**，命名 `feat/<thing>` 或 `fix/<thing>`
3. **commit message 写人话。** 命令式语气、首字母小写、首行 ≤72 字符、需要解释就写正文。看 `git log --oneline | head` 跟着风格走
4. **能测就测。** 项目重度依赖在线服务，不强制覆盖率，但能用 offline-mock 或 local-mock（`config.local-mock.json`）模拟的就模拟
5. **提交前对 diff 做一次脱敏。** `.gitignore` 已经排了 `output/` / `flows/` / `paypal_cf_persist/` / 运行时配置之类，但 `git diff --cached` 看一眼，确认没有真实 cookie / token / IP / 邮箱混进 stage
6. **PR 标题** 跟 commit 同格式。描述回答三件事：改了什么、为什么、怎么测的

## 代码风格

- Python：基本 PEP-8，4 空格缩进，不强制行长，不强制 auto-formatter（现有代码有自己的节奏，本地匹配就行）
- 注释：中英文都行。预计会被非中文读者读到的新代码用英文更友好
- 日志：留够上下文，能从 `tail` 上看出问题。现在的 pattern：`[STAGE] something something detail=...`
- 配置：优先在已有 JSON section 里加 flag，少新建顶级 section。用户能看见的 flag 在 `README.md` 文档化

## 研究内容贡献的脱敏清单

要给 [实证段](README.md#-反欺诈实证) 加内容：

- IP：用 `203.0.113.x`（TEST-NET-3）、`198.51.100.x`（TEST-NET-2）、`192.0.2.x`（TEST-NET-1）。**永远别**发真实 IP，哪怕只是短暂用过的
- 域名：用 `*.example`、`*.example.com`、`*.test`、`*.invalid`。**永远别**发真实域名
- 邮箱：`you@example.com`、`tester@example.com`
- 账号数和时间：**保持真实**，那是研究本身
- 内部 ASN / 组织名：替成 `AS-XX`、`ISP-A`
- ChatGPT 账号 ID / token / cookie：**永远别**发，截断都不行

## 问题

开 discussion 或 issue。没有 chat、没有 Discord、没有 mailing list。
