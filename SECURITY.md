# 安全策略

## 报告本项目的漏洞

如果你在 **`Gpt-Agreement-Payment` 自己**身上发现了安全问题 —— 凭证泄露、配置加载器的注入、不安全的反序列化、辅助脚本的 SSRF 之类 —— 请私下报，别公开提 issue。

**通道**（按优先级）：

1. GitHub 私密漏洞报告：仓库的 **Security → Report a vulnerability**
2. 通过仓库 owner 的 GitHub profile 找到维护者邮箱

请提供：

- 受影响的文件和行号
- 最小复现步骤
- 你估计的影响（auth bypass？RCE？凭证暴露？）
- 修复时是否要署名

**响应时效**：尽量 7 天内首回复，30 天内修或缓解。志愿者维护，慢一点请理解。

---

## 不在受理范围

本项目是协议研究工具。关于*目标服务*（Stripe、PayPal、ChatGPT、hCaptcha、Cloudflare、Webshare 等）的发现请直接报给**对应厂商**，走它们各自的 bug bounty：

- **OpenAI** —— https://openai.com/security/disclosure/
- **Stripe** —— https://stripe.com/.well-known/security.txt
- **PayPal** —— 通过 HackerOne：https://hackerone.com/paypal
- **Cloudflare** —— https://hackerone.com/cloudflare
- **hCaptcha (Intuition Machines)** —— https://www.hcaptcha.com/security

我们**不**代你向目标服务报告或 triage，也没法给厂商提供已认证的复现环境。这些是你跟厂商直接谈的事。

---

## 仅限授权使用

用本软件即表示你确认：

- 你测的是**你自己的**系统，或者你有**明确书面授权**测的系统（比如某个 bug bounty 项目里相关资产明确在 scope 内）
- 你不会拿这套工具搞欺诈、规避支付、批量造号、违反任何第三方平台的 ToS
- 你知道针对未授权目标跑这套工具可能违法，包括但不限于美国 **CFAA**、英国 **Computer Misuse Act**、**GDPR / CCPA** 隐私法规以及各国诈骗罪

维护者不协助、不建议、不接受任何意图未授权使用的贡献。这类 Issue / PR 直接关，不回复。情节严重的（公开炫耀违反 ToS、求帮忙诈骗某具体公司之类）封禁。

---

## 反欺诈研究的负责任披露

[`README.md` 反欺诈实证段](README.md#-反欺诈实证) 描述的是**生产环境观察到的防御机制**，抽象层级跟 CAPTCHA-breaking 论文、指纹关联攻击论文这类学术文献是一个量级的。具体说：

- 所有数字 IP 都是 RFC 5737 文档段占位符（`203.0.113.0/24`、`198.51.100.0/24`、`192.0.2.0/24`）
- 所有写到的域名都是 `*.example` 占位符
- 账号数、时间、推理是真的 —— 那才是研究本身的价值

如果 OpenAI / Cloudflare 之类公司的防守方觉得里面有什么材料越界到该删的运营细节，请提交私密漏洞报告（看上面）一起讨论。我们没兴趣发那些会有意义削弱防御态势的内容；但**有**兴趣让这些防御机制的*抽象结构*公开，让以后的系统设计能纳入考量。
