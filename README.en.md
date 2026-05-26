> [简体中文](README.md) | **English**

<p align="center">
  <img src="docs/images/logo-light.png#gh-light-mode-only" width="120" alt="Gpt-Agreement-Payment logo">
  <img src="docs/images/logo-dark.png#gh-dark-mode-only"   width="120" alt="Gpt-Agreement-Payment logo">
</p>

# Gpt-Agreement-Payment

End-to-end replay tool for ChatGPT Plus / Team subscription agreements: reverse-engineered from packet capture the entire chain `Stripe Checkout → PayPal / GoPay / QRIS → ChatGPT manual-approval → Codex OAuth + PKCE` and implemented as a runnable client. Includes a from-scratch hCaptcha visual solver, PayPal fraud detection early-exit branch, and a set of empirical anti-fraud mechanism data collected from real execution.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![CI](https://img.shields.io/github/actions/workflow/status/DanOps-1/Gpt-Agreement-Payment/ci.yml?label=CI)](https://github.com/DanOps-1/Gpt-Agreement-Payment/actions)
[![Use](https://img.shields.io/badge/use-CTF%20%2F%20bug%20bounty-red)](#legal-boundary)

---

## Author-run API Relay Station

| Logo | Name | Description | Website |
| --- | --- | --- | --- |
| <a href="https://api.lukyface.com/" target="_blank"><img src="docs/images/sponsors/lukyface.png" alt="lukyface API" width="140" /></a> | lukyface API (Author-run Relay Station) | Unified AI model aggregation / distribution gateway (based on new-api), OpenAI / Claude / Gemini protocol interconversion, self-use surplus sharing. **Author-run, long-term stable**.<br><br>**Open only to [LINUX DO](https://linux.do/) users** · **Payment in LDC (Linux Do in-site currency) only** · Author-run relay station exchange group: **`1104565621`** | [https://api.lukyface.com/](https://api.lukyface.com/) |

---

> [!CAUTION]
> **Using this project constitutes acceptance of all terms in [`NOTICE`](NOTICE).** The project is provided AS IS, with no warranties, and the maintainers assume no responsibility. Authorized use only on systems you own / legitimate CTF / authorized bug bounty in-scope assets / security research. **Strictly prohibited** for fraud, payment evasion, bulk account creation for resale, violation of third-party ToS, unauthorized targets. All legal responsibility rests with the user. If you do not accept these terms, **do not use**.

---

## What This Is

Supports four subscription activation paths:

| Path | Entry Point | Use Case |
|---|---|---|
| **Team / Plus (PayPal billing agreement)** | `pipeline.py --paypal` | Stripe Checkout → PayPal billing agreement → ChatGPT manual approval |
| **Plus (promo long link + PayPal agreement auth)** | `scripts/no_card_paypal_plus.py` | OpenAI official promo campaign long link + PayPal guest checkout completing billing agreement protocol |
| **Plus / Team (GoPay Indonesia)** | `pipeline.py --gopay` | Midtrans linking + GoPay wallet binding, IDR region exclusive |
| **Plus / Team (QRIS scan)** | `pipeline.py --qris` | Midtrans QRIS, remote preview + reference polling settlement |

Given a clean proxy + payment credential, run the command to completion and get the OAuth `refresh_token`.

Four points worth examining:

- **N-worker concurrency + phone-lock OTP critical section mutual exclusion** (`webui/backend/parallel_runner.py`). Same phone across multiple workers serializes OTP phase using advisory lock, pre/post-OTP fully parallelized; DB atomic claim + placeholder INSERT prevents multiple workers contending for same promo_link / same inventory email. Frontend configures phone pool (M rows) + concurrency N (can exceed M), worker maps to phone pool via `i % M` round-robin.
- **hCaptcha visual solver** (`CTF-pay/hcaptcha_auto_solver.py`, ~4000 lines, independently usable). VLM primary path + CLIP/OpenCV heuristic fallback + Playwright human action synthesis, covers 12 known hCaptcha question types.
- **Anti-fraud mechanism empirical data**. IP string-level exact fingerprinting, batch correlation delayed banning, probe layer vs ban layer separation. Measured sample of ~2% 24-hour survival rate across 45 accounts, including corrected models. See [`docs/anti-fraud-research.md`](docs/anti-fraud-research.md).
- **Twelve-path self-healing daemon loops** (`pipeline.py::daemon()`). Webshare API auto IP rotation (webshare jitter fallback to `/tmp/gost_last.json` cache), CF DNS quota cleanup, tmpfs orphan recovery, gost relay watchdog, DataDome slider auto-drag. Design goal is unattended operation for weeks.

---

## Architecture

```mermaid
flowchart LR
    A[pipeline.py / webui] --> B[CTF-reg/<br/>browser_register.py<br/>Camoufox + Turnstile]
    B --> C[CTF-pay/card.py<br/>Stripe Checkout replay]
    C --> D{Payment path}
    D -->|paypal| E1[Camoufox / Node RPA<br/>PayPal billing agreement]
    D -->|gopay| E2[Midtrans linking<br/>+ WhatsApp OTP]
    D -->|qris| E3[QRIS QR<br/>+ remote preview]
    E1 --> F[Stripe poll<br/>state=succeeded]
    E2 --> F
    E3 --> F
    F --> G[Camoufox second login<br/>Codex OAuth + PKCE]
    G --> H[refresh_token<br/>output/webui.db &#40;SQLite&#41;]
```

For detailed subsystem decomposition, file organization, and protocol chain details, see [`docs/architecture.md`](docs/architecture.md).

---

## Current Status and Entry Barriers

To be frank, this is not an out-of-the-box tool. Running through the entire pipeline requires at least:

- A real, login-capable PayPal account (or go through PayPal guest checkout authorization flow)
- A proxy with exit in EU / US / ID (region locked based on selected payment path)
- A Cloudflare zone (optional, for enabling catch-all subdomain email registration; also supports Outlook code pool)
- A Linux machine capable of running Camoufox + Playwright (approximately 5 GB disk + 2 GB memory)
- (Required for PayPal guest checkout) An SMS code gateway API key for PayPal signup
- (Required for GoPay) A WhatsApp online number + WhatsApp code verification service
- (Optional) An OpenAI-compatible VLM API key for hCaptcha solving; residential / pseudo-residential exits typically don't trigger hCaptcha, and CLIP degradation is available without VLM
- (Optional) A captcha solving platform API key compatible with createTask/getTaskResult protocol as a fallback for browser passive captcha

Full end-to-end setup typically takes 1–3 hours for initial configuration tuning. Once daemon mode runs stably, a single pipeline takes ~5 minutes; concurrent mode with 2 workers on the same phone generates 2 accounts in ~3 minutes.

Code is research-oriented, organized by protocol stage, and doesn't prioritize maximum readability.

---

## Getting Started

### Beginner Path: WebUI Configuration Wizard (Recommended)

Compress 1–3 hours of manual configuration into ~15 minutes. 14-step wizard + real-time preflight self-checks + built-in runtime controller (SSE log stream + concurrency panel) generate `CTF-pay/config.auto.json` + `CTF-reg/config.paypal-proxy.json` configurations.

![WebUI screenshot](docs/images/webui.png)

#### Docker Deployment (Fastest Path, One-Click Start)

The repo comes with `Dockerfile` (multi-stage build: Node frontend + Ubuntu 24.04 runtime) + `docker-compose.yml`, packing all system dependencies / Playwright Chromium+Firefox / Camoufox / gost SOCKS5 relay / Node QuickJS (for OpenAI Sentinel) into the image. The git working tree on the host serves as the single source of truth, bind-mounted into the container; restart with `docker compose restart` for instant effect after modifying Python code, no rebuild needed.

```bash
git clone https://github.com/DanOps-1/Gpt-Agreement-Payment
cd Gpt-Agreement-Payment
docker compose up -d --build
# Default listening on 127.0.0.1:8765 (host port), open http://127.0.0.1:8765/ in browser
# First visit redirects to /setup to create admin
```

Common Maintenance Commands:

```bash
# Real-time logs
docker compose logs -f webui

# Debug inside container (pip list / run tests / inspect SQLite)
docker compose exec webui bash

# Reload uvicorn after Python code changes (bind mount already syncs source code, just restart process)
docker compose restart webui

# Rebuild dist in container after frontend code changes
docker compose exec webui sh -c "cd /app/webui/frontend && npm run build"

# Completely stop + clean containers
docker compose down

# Image upgrade (pull new base / upgrade Python packages)
docker compose build --no-cache && docker compose up -d
```

# Data Persistence

The `output/` directory is a host directory bind mount containing `webui.db` (SQLite) + run results / logs visible and backupable to the host. `webui/frontend/dist` and `node_modules` use anonymous volumes, which are overridden by the baked image version (not overridden by an empty host directory).

## Public Access (nginx Reverse Proxy + HTTPS)

See [`webui/README.md`](webui/README.md). By default, `docker-compose.yml` binds the port to `127.0.0.1:8765`; to expose directly to `0.0.0.0`, modify the `ports` section (not recommended without an authentication layer in front).

#### Manual Installation (without Docker)

```bash
# 1. Backend dependencies
pip install -r webui/requirements.txt

# 2. Frontend build (one-time)
cd webui/frontend && pnpm i && pnpm build && cd ../..

# 3. Start
python -m webui.server
# Open http://127.0.0.1:8765 in browser, first visit redirects to /setup to create admin
```

Support single run (PayPal billing agreement / GoPay / QRIS / promo long links) + concurrent mode (frontend configured with phone pool + concurrency N), public network access via nginx reverse proxy see [`webui/README.md`](webui/README.md).

### Installation

```bash
git clone https://github.com/DanOps-1/Gpt-Agreement-Payment
cd Gpt-Agreement-Payment
pip install requests curl_cffi playwright camoufox browserforge mitmproxy pybase64
playwright install firefox
camoufox fetch
```

hCaptcha solver ML dependencies (torch / transformers / opencv) are recommended to be installed separately in a venv, approximately 4 GB:

```bash
python -m venv ~/.venvs/ctfml
~/.venvs/ctfml/bin/pip install torch transformers opencv-python pillow numpy
```

# Complete dependency list and system packages, see [`docs/installation.md`](docs/installation.md).

### Configuration

Copy the template and fill in values:

```bash
cp CTF-pay/config.paypal.example.json     CTF-pay/config.paypal.json
cp CTF-reg/config.paypal-proxy.example.json   CTF-reg/config.paypal-proxy.json
```

For field meanings and schema, see [`docs/configuration.md`](docs/configuration.md). The Docker deployment entrypoint will automatically bootstrap these two config files on first startup. After editing on the host, run `docker compose restart webui` to take effect.

### Running

> Docker users only need to use webui to run. The CLI commands below are for users running natively locally; to run the same commands in Docker, first `docker compose exec webui bash` to enter the container.

```bash
# 1) Single complete workflow (PayPal billing agreement)
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal

# 2) Plus protocol authorization + promo long link (PayPal guest checkout)
xvfb-run -a python scripts/no_card_paypal_plus.py \
    --config CTF-pay/config.paypal.json --paypal-node-rpa \
    --phone <your_phone> --otp-timeout 240

# 3) N worker concurrency (same phone is fine, OTP phase auto-queues via advisory lock)
# Recommended to start via webui: select concurrency mode, frontend configures phone pool + concurrency N

# 4) Continuous account pool maintenance
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal --daemon
```

# Four Operating Modes (Single / Batch / Self-Dealer / Daemon) + Concurrency Mode Differences and Parameters

See [`docs/operating-modes.md`](docs/operating-modes.md).

---

## Documentation

| Documentation | Content |
|---|---|
| [`docs/installation.md`](docs/installation.md) | System dependencies, Python packages, ML venv, gost relay, first PayPal login |
| [`docs/configuration.md`](docs/configuration.md) | All JSON fields, environment variables, CF API token application |
| [`docs/architecture.md`](docs/architecture.md) | Subsystems, file organization, protocol chain details |
| [`docs/operating-modes.md`](docs/operating-modes.md) | Single / Batch / Self-Dealer / Daemon / Concurrency detailed parameters |
| [`docs/hcaptcha-solver.md`](docs/hcaptcha-solver.md) | Three-layer decision-making, 12 question types, standalone invocation, extending new question types |
| [`docs/daemon-mode.md`](docs/daemon-mode.md) | 12-path self-healing loop trigger conditions and state machine |
| [`docs/anti-fraud-research.md`](docs/anti-fraud-research.md) | Complete anti-fraud empirical data and corrected models |
| [`docs/debugging.md`](docs/debugging.md) | Common exceptions, artifact paths, troubleshooting commands |

---

## Known Limitations

- **PayPal billing agreement only available in EU**. Stripe account restrictions; can only place orders as EU identities like IE.
- **PayPal guest checkout path subject to multiple risk controls**. `INSTRUMENT_SHARING_LIMIT_EXCEEDED` / `CC_LINKED_TO_FULL_ACCOUNT` / `CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR` / DataDome captcha etc. will all trigger; the script has early exit + automatic retry branches for each known error.
- **Batch registration next-day survival rate ~2%**. This is caused by batch correlation effects in ChatGPT's anti-fraud mechanism, not the tool itself. See [`docs/anti-fraud-research.md`](docs/anti-fraud-research.md).
- **Free account path currently not working**. OpenAI changed the free account secondary login flow; redirect to `/add-phone` cannot be bypassed without a real phone number; ChatGPT-Web client's access_token calling Codex API has audience mismatch.
- **Stripe runtime fingerprint drifts**. `runtime.version` / `js_checksum` / `rv_timestamp` need realignment approximately every few weeks.
- **hCaptcha question type coverage incomplete**. Current 12 common types; when uncovered, VLM directly outputs coordinates as fallback, success rate not guaranteed.
- **Concurrency shares single exit IP**. Webshare pool currently can only provide a single IP; N workers traverse the same IP; recommended concurrency ≤ 3 to avoid PayPal/DataDome risk control joint punishment. Multi-IP pool support on roadmap.
- **Code style somewhat rough**. `_monolith.py` arranged sequentially by protocol stage, comments mix Chinese and English, not suitable as a Python engineering example.

---

## Contribution

Most valuable contributions ranked by impact:

1. New hCaptcha question type solvers
2. Protocol adaptation when Stripe / PayPal / OpenAI introduces breaking changes
3. New failure mode daemon self-healing branches observed in practice (with logs)
4. Anti-fraud empirical data supplements (desensitization format per existing examples)
5. Multi-IP pool / proxy rotation implementation to free concurrency from single IP limitation
6. Documentation improvement / translation

> ⚠️ **Maintainers cannot manually reproduce PRs**. When submitting PRs, please follow the [PR template](.github/PULL_REQUEST_TEMPLATE.md) providing **detailed description + running evidence** (requirements vary by change type: solver question types need round JSON, protocol adaptation needs packet capture comparison, daemon self-healing needs trigger logs and recovery logs). PRs lacking evidence will be closed without explanation.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for complete workflow, code style, and desensitization checklist for research contributions.
See [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) for community guidelines.
Do not open issues for security problems; see [`SECURITY.md`](SECURITY.md).

---

## Acknowledgments

Key dependencies in the tool chain:

- [Camoufox](https://github.com/daijro/camoufox) — antidetect Firefox build, foundation of the entire browser automation layer
- [mitmproxy](https://mitmproxy.org/) — protocol packet capture
- [Playwright](https://playwright.dev/) — browser automation
- [curl_cffi](https://github.com/lexiforest/curl_cffi) — TLS fingerprint simulation
- [OpenAI CLIP](https://github.com/openai/CLIP) — visual backbone for heuristic solver
- [gost](https://github.com/go-gost/gost) — SOCKS5 relay

### Code Contributors

Thanks to the following friends for code contributions (by PR time):

- [@Lium-7768](https://github.com/Lium-7768) — [#12](https://github.com/DanOps-1/Gpt-Agreement-Payment/pull/12) Align GoPay step visibility to hide 06 and 13
- [@DragonBaiMo](https://github.com/DragonBaiMo) — [#15](https://github.com/DanOps-1/Gpt-Agreement-Payment/pull/15) Algorithmicized persona generator + email name co-sourcing
- [@laochendeai](https://github.com/laochendeai) — [#21](https://github.com/DanOps-1/Gpt-Agreement-Payment/pull/21) detect blocking challenge pages

## Community

| Channel | Purpose |
|---|---|
| [**LINUX DO**](https://linux.do/) | Main technical discussions, protocol research feedback, long-term records |
| QQ Group **`1028722105`** | Real-time Chinese community exchange |
| GitHub Issues | Bug reports and PRs (main entry point) |

Special thanks to the LINUX DO community — the earliest feedback sources, testers, and protocol change reporters all come from here.

---

## Sponsorship

If this project has been helpful to you, you're welcome to buy the author a coffee ☕

<p align="center">
  <img src="goodgood.jpg" width="280" alt="Sponsorship QR code">
</p>

### Donation Thanks

Thanks to the following friends for their support (no particular order):

| Sponsor | Amount |
|---|---|
| Galaxy-n | ¥101 |
| 两岁 | ¥100 |
| 朴朴配送员 | ¥66 |
| Ka | ¥28.88 |
| 追Mou | ¥20 |
| 原昊 | ¥20 |
| A. | ¥10 |
| acedia | ¥9.1 |
| 至上松一 | ¥6.66 |
| 书忆江南 | ¥5 |
| 辛昊 | ¥5 |
| bensema | ¥0.66 |
| Earth NPC | ¥0.01 |
| 小水獭 | ¥0.01 |
| 钟 | ¥0.01 |

The gesture matters more than the amount; every bit of support is motivation to continue maintaining the project 🙏

---

## Star History

<a href="https://star-history.com/#DanOps-1/Gpt-Agreement-Payment&Date">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=DanOps-1/Gpt-Agreement-Payment&type=Date&theme=dark" />
    <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=DanOps-1/Gpt-Agreement-Payment&type=Date" />
    <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=DanOps-1/Gpt-Agreement-Payment&type=Date" />
  </picture>
</a>

---

## Disclaimer

> [!IMPORTANT]
> **Using this project constitutes your acceptance of having fully read, completely understood, and explicitly agreed to all terms in [`NOTICE`](NOTICE).** If you cannot accept — do not use this project, delete all copies.

License is [MIT](LICENSE), but the License itself is not the complete disclaimer. Full disclaimer terms are in [`NOTICE`](NOTICE); below is the key summary:

**This project is provided "AS IS" without warranties of any kind.** Including but not limited to merchantability, fitness for a particular purpose, non-infringement, safety, stability, and continued compatibility with third-party services. You assume all risks of using this project.

**Use only within authorized scope.** Permitted: systems you own, legal CTF, authorized bug bounty projects on in-scope assets, security research. **Prohibited**: fraud, payment evasion, bulk account creation for resale, violating third-party ToS, unauthorized targets.

**You bear all legal responsibility.** Including but not limited to account suspension, payment loss, criminal liability, civil damages, administrative penalties, third-party claims, reputation loss, business loss. Applicable laws may include US CFAA, EU GDPR, UK CMA, China Criminal Law Articles 285/286/287, etc. See section 4 of [`NOTICE`](NOTICE).

**Maintainers have no obligation to reply to issues, review PRs, fix bugs, maintain availability, or adapt to protocol changes.** Reserved the right to archive, delete, rename, or stop maintaining this project at any time without prior notice.

Uncertain whether your use is legal — **do not run**. Ask a lawyer first, or talk to the target platform's security team.
