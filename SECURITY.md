# Security Policy

## Reporting Vulnerabilities

If you discover a security issue with **`Gpt-Agreement-Payment` itself**—such as credential leaks, injections in configuration loaders, unsafe deserialization, SSRF in helper scripts, etc.—please report it privately. Do not open a public issue.

**Reporting Channels** (in order of priority):

1. GitHub Private Vulnerability Reporting: In the repository under **Security → Report a vulnerability**
2. Email the maintainer found on the repository owner's GitHub profile

Please include:

- Affected files and line numbers
- Minimal reproduction steps
- Estimated impact (Auth bypass? RCE? Credential exposure?)
- Whether you'd like to be credited for the fix

**Response SLA**: We aim for an initial response within 7 days and a fix or mitigation within 30 days. This is a volunteer-maintained project; please be patient.

---

## Out of Scope

This project is a protocol research tool. Discoveries regarding *target services* (Stripe, PayPal, ChatGPT, hCaptcha, Cloudflare, Webshare, etc.) should be reported directly to the **respective vendors** via their bug bounty programs:

- **OpenAI** — https://openai.com/security/disclosure/
- **Stripe** — https://stripe.com/.well-known/security.txt
- **PayPal** — via HackerOne: https://hackerone.com/paypal
- **Cloudflare** — https://hackerone.com/cloudflare
- **hCaptcha (Intuition Machines)** — https://www.hcaptcha.com/security

We **do not** report or triage issues on your behalf to target services, nor can we provide authenticated reproduction environments for vendors. These matters are between you and the vendor.

---

## Authorized Use Only

By using this software, you confirm that:

- You are testing **your own** systems, or systems for which you have **explicit written authorization** (e.g., assets clearly in-scope for a bug bounty project).
- You will not use this tool for fraud, payment evasion, bulk account creation, or violating any third-party platform's ToS.
- You understand that running this tool against unauthorized targets may be illegal, including but not limited to the US **CFAA**, the UK **Computer Misuse Act**, **GDPR / CCPA** privacy regulations, and fraud laws in various countries.

The maintainers do not support, recommend, or accept any contributions intended for unauthorized use. Such Issues / PRs will be closed without response. Severe violations (publicly boasting about ToS violations or asking for help with fraud) will result in a ban.

---

## Responsible Disclosure of Anti-Fraud Research

The [`README.md` Anti-Fraud Evidence section](README.md#-anti-fraud-evidence) describes **defense mechanisms observed in production environments**, at an abstraction level comparable to academic papers on CAPTCHA-breaking or fingerprinting attacks. Specifically:

- All numeric IPs are RFC 5737 documentation range placeholders (`203.0.113.0/24`, `198.51.100.0/24`, `192.0.2.0/24`).
- All mentioned domains are `*.example` placeholders.
- Account counts, times, and logic are real—that's the research value.

If defenders from companies like OpenAI or Cloudflare feel that any material crosses the line into operational details that should be removed, please submit a private vulnerability report (see above) for discussion. We have no interest in publishing content that would meaningfully weaken defenses; we **do** have an interest in making the *abstract structure* of these mechanisms public so they can be considered in future system designs.
