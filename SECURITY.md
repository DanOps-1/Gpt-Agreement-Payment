# Security Policy

## Reporting Vulnerabilities in This Project

If you discover a security issue in **`Gpt-Agreement-Payment` itself** — credential leaks, configuration loader injection, unsafe deserialization, SSRF in helper scripts, etc. — please report it privately and do not open a public issue.

**Channels** (by priority):

1. GitHub private vulnerability report: **Security → Report a vulnerability** in the repository
2. Find the maintainer's email through the repository owner's GitHub profile

Please provide:

- Affected files and line numbers
- Minimal reproduction steps
- Your estimated impact (auth bypass? RCE? credential exposure?)
- Whether you should be credited in the fix

**Response timeline**: We aim to reply within 7 days and fix or mitigate within 30 days. Community-maintained, so please understand if it's slower.

---

## Out of Scope

This project is a protocol research tool. Findings about *target services* (Stripe, PayPal, ChatGPT, hCaptcha, Cloudflare, Webshare, etc.) should be reported directly to **the respective vendors** through their bug bounty programs:

- **OpenAI** — https://openai.com/security/disclosure/
- **Stripe** — https://stripe.com/.well-known/security.txt
- **PayPal** — via HackerOne: https://hackerone.com/paypal
- **Cloudflare** — https://hackerone.com/cloudflare
- **hCaptcha (Intuition Machines)** — https://www.hcaptcha.com/security

We do **not** report or triage findings to target services on your behalf, and cannot provide vendors with authenticated reproduction environments. That's a conversation between you and the vendor.

---

## Authorized Use Only

By using this software, you confirm that:

- You are testing **your own** systems, or you have **explicit written authorization** to test the systems in question (e.g., assets explicitly in-scope in a bug bounty program)
- You will not use this toolkit for fraud, payment evasion, bulk account creation, or violation of any third-party platform's ToS
- You understand that running this against unauthorized targets may be illegal, including but not limited to the US **CFAA**, UK **Computer Misuse Act**, **GDPR / CCPA** privacy laws, and fraud statutes in various jurisdictions

Maintainers will not assist, recommend, or accept contributions intended for unauthorized use. Such Issues / PRs are closed without response. Serious cases (publicly bragging about ToS violations, asking for help defrauding a specific company, etc.) result in bans.

---

## Responsible Disclosure for Fraud Research

The anti-fraud empirical section in [`README.md`](README.md#-anti-fraud-empirical) describes **defense mechanisms observed in production**, at the same level of abstraction as CAPTCHA-breaking papers and fingerprinting attack papers in academic literature. Specifically:

- All numerical IPs are RFC 5737 placeholder ranges (`203.0.113.0/24`, `198.51.100.0/24`, `192.0.2.0/24`)
- All domains written are `*.example` placeholders
- Account counts, timelines, and reasoning are authentic — that's the research's actual value

If OpenAI / Cloudflare or similar defense operators believe any material crosses into operational details that should be redacted, please submit a private vulnerability report (see above) to discuss. We have no interest in publishing content that meaningfully undermines defense posture; but we **do** care about making the *abstract structure* of these defense mechanisms public so future system designs can account for them.