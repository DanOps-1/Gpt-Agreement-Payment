<p align="center">
  <img src="docs/images/logo-light.png#gh-light-mode-only" width="120" alt="Gpt-Agreement-Payment logo">
  <img src="docs/images/logo-dark.png#gh-dark-mode-only"   width="120" alt="Gpt-Agreement-Payment logo">
</p>

Gpt-Agreement-Payment

An end-to-end replay tool for the ChatGPT Team subscription agreement flow, including a from-scratch implementation of an hCaptcha visual solver and a dataset of empirically collected anti-fraud behavior.

[!CAUTION]
Using this project implies full agreement with all terms in NOTICE￼. The project is provided AS IS, without any warranty, and the maintainers bear no responsibility. Use is strictly limited to systems you own, legitimate CTFs, authorized bug bounty in-scope assets, and security research. Strictly prohibited: fraud, payment circumvention, mass account creation/resale, violating third-party ToS, or targeting unauthorized systems. All legal liability lies with the user. If you do not accept these terms, do not use this project.

⸻

What is this?

This project reconstructs and implements the full chain:

Stripe Checkout → PayPal billing agreement → ChatGPT manual approval → Codex OAuth + PKCE

It runs as a client: given a clean proxy and a payment credential, it executes the pipeline and outputs an OAuth refresh_token.

Three notable aspects:

* hCaptcha visual solver (CTF-pay/hcaptcha_auto_solver.py, ~4000 lines, standalone).
    Uses a VLM primary path, with CLIP/OpenCV heuristic fallback, plus Playwright human-like interaction simulation. Covers 12 known hCaptcha challenge types.
* Empirical anti-fraud dataset
    Includes IP-level fingerprinting, batch correlation delayed bans, and separation between probe layer vs ban layer. Based on real-world runs: ~2% 24-hour survival rate across 45 accounts. Includes corrected modeling. See docs/anti-fraud-research.md￼.
* 12-loop self-healing daemon (pipeline.py::daemon())
    Automates IP rotation via Webshare API, Cloudflare DNS quota cleanup, tmpfs orphan cleanup, gost relay watchdog, DataDome slider automation, etc. Designed for unattended multi-week operation.

⸻

Architecture

flowchart LR
    A[pipeline.py] --> B[CTF-reg/<br/>browser_register.py<br/>Camoufox + Turnstile]
    B --> C[CTF-pay/card.py<br/>Stripe Checkout replay]
    C --> D[Stripe confirm<br/>+ ChatGPT /approve]
    D --> E[Camoufox PayPal<br/>agreement authorization]
    E --> F[Stripe poll<br/>state=succeeded]
    F --> G[Camoufox second login<br/>Codex OAuth + PKCE]
    G --> H[refresh_token<br/>output/results.jsonl]

See docs/architecture.md￼ for subsystem breakdowns and protocol details.

⸻

Current Status & Requirements

This is not plug-and-play. To run the full pipeline, you need:

* A real, usable PayPal account (first run requires manual email OTP 2FA)
* A proxy with EU/US egress (PayPal is region-locked, Stripe enforces country)
* A Cloudflare zone (for catch-all email registration)
* A Linux machine capable of running Camoufox + Playwright (~5 GB disk, ~2 GB RAM)
* (Optional) OpenAI-compatible VLM API key for hCaptcha solving
    (Residential IPs often avoid hCaptcha; fallback to CLIP is supported)
* (Optional) A captcha-solving service API (createTask/getTaskResult compatible)

Initial setup typically takes 1–3 hours. Once stable, each pipeline run takes ~5 minutes.

Code is research-oriented; readability is not prioritized.
CTF-pay/card.py is a single ~8000-line file organized by protocol phase.

⸻

Getting Started

Beginner Path: WebUI Wizard (Recommended)

Reduces setup from 1–3 hours to ~15 minutes.
Includes a 14-step wizard, real-time preflight checks, and a built-in controller (SSE log stream).

Generates:

* CTF-pay/config.auto.json
* CTF-reg/config.paypal-proxy.json

# 1. Backend dependencies
pip install -r webui/requirements.txt
# 2. Frontend build (one-time)
cd webui/frontend && pnpm i && pnpm build && cd ../..
# 3. Start
python -m webui.server
# Open http://127.0.0.1:8765 (first visit redirects to /setup)

Supports both Plus and Team subscription flows. See webui/README.md￼ for nginx reverse proxy setup.

⸻

Installation

git clone https://github.com/DanOps-1/Gpt-Agreement-Payment
cd Gpt-Agreement-Payment
pip install requests curl_cffi playwright camoufox browserforge mitmproxy pybase64
playwright install firefox
camoufox fetch

Optional ML dependencies (~4 GB, recommended separate venv):

python -m venv ~/.venvs/ctfml
~/.venvs/ctfml/bin/pip install torch transformers opencv-python pillow numpy

See docs/installation.md￼ for full details.

⸻

Configuration

cp CTF-pay/config.paypal.example.json     CTF-pay/config.paypal.json
cp CTF-reg/config.paypal-proxy.example.json   CTF-reg/config.paypal-proxy.json

Field definitions: docs/configuration.md￼

⸻

Run

# Single run
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal
# Continuous daemon
xvfb-run -a python pipeline.py --config CTF-pay/config.paypal.json --paypal --daemon

See docs/operating-modes.md￼ for modes and parameters.

⸻

Documentation

Doc	Description
installation.md	System deps, Python packages, ML env, gost relay
configuration.md	JSON schema, env vars, CF API tokens
architecture.md	System design and protocol chain
operating-modes.md	Modes and runtime parameters
hcaptcha-solver.md	Solver architecture and extensions
daemon-mode.md	Self-healing loop logic
anti-fraud-research.md	Dataset and modeling
debugging.md	Troubleshooting

⸻

Known Limitations

* PayPal EU-only support
* ~2% next-day survival rate (due to anti-fraud batch correlation)
* Free-tier flow currently broken (phone verification required)
* Stripe runtime fingerprint drift (needs periodic updates)
* Incomplete hCaptcha coverage (fallback not guaranteed)
* Code quality is not production-grade

⸻

Contributing

High-impact contributions:

1. New hCaptcha solver types
2. Protocol updates for Stripe/PayPal/OpenAI changes
3. New daemon recovery branches with logs
4. Anti-fraud dataset contributions (properly anonymized)
5. Documentation improvements / translations

PRs must include reproducible evidence per template.

⸻

Disclaimer

[!IMPORTANT]
Using this project means you have fully read and accepted NOTICE￼.

* Provided AS IS, no warranty
* Use only in authorized contexts
* All legal responsibility lies with the user
* Maintainers have no obligation to maintain or support
* Not affiliated with OpenAI, Stripe, PayPal, Cloudflare, or hCaptcha

Full terms: NOTICE￼
License: MIT