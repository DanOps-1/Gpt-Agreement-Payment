# Architecture Explained

[← Back to README](../README.md)

## Top-Level Process```mermaid
flowchart LR
    A[pipeline.py] --> B[CTF-reg/<br/>browser_register.py<br/>Camoufox + Turnstile]
    B --> C[CTF-pay/card.py<br/>Stripe Checkout Replay]
    C --> D[Stripe confirm<br/>+ ChatGPT /approve]
    D --> E[Camoufox PayPal<br/>Protocol Authorization]
    E --> F[Stripe poll<br/>state=succeeded]
    F --> G[Camoufox Second Login<br/>Codex OAuth + PKCE]
    G --> H[refresh_token<br/>output/webui.db &#40;SQLite&#41;]
    H --> I[Optional: Push to gpt-team /<br/>CPA / Other Downstream]
```## File Organization```
Gpt-Agreement-Payment/
├── pipeline.py                     # Orchestrator: single/batch/daemon/self-dealer
├── CTF-pay/                        # Stripe + PayPal protocol replay
│   ├── card.py                     # Main program, ~8000 lines
│   ├── hcaptcha_auto_solver.py     # Vision solving (VLM + CLIP + Playwright)
│   ├── hcaptcha_bridge_helper.py   # Interactive debug tool
│   ├── local_mock_gateway.py       # Stripe state machine local mock
│   ├── retry_house_decline.py      # Card decline terminal state retry wrapper
│   └── config.*.json               # Template in repo, runtime config gitignored
├── CTF-reg/                        # ChatGPT registration subsystem
│   ├── browser_register.py         # Camoufox real browser registration
│   ├── auth_flow.py                # Pure HTTP registration (fallback)
│   ├── sentinel.py                 # OpenAI Sentinel PoW token
│   ├── mail_provider.py            # Generate catch-all mailbox + delegate cf_kv_otp_provider for OTP
│   ├── cf_kv_otp_provider.py       # Read OTP from Cloudflare KV (worker writes)
│   ├── http_client.py              # curl_cffi/requests factory
│   └── config.py                   # dataclass config definition
├── docs/                           # Detailed documentation
└── output/                         # Runtime artifacts (gitignored)
    ├── webui.db                    # SQLite runtime store
    ├── SQLite runtime_meta[daemon_state]
    └── logs/
```---

## Subsystems

### `CTF-pay/` —— Payment Protocol Replay Main Program

#### `card.py` (approximately 8000 lines single file)

Intentionally implemented as a single large program, organized by functional sections rather than split into modules. Reasons:

- The protocol pipeline is a single linear flow; splitting modules would increase cross-file navigation overhead
- Large amounts of local state are passed between stages; splitting would result in unwieldy parameter lists
- Single file enables easier holistic reading and quick location of code

Main sections:

| Section | Approximate Lines | Content |
|---|---|---|
| Config loading | 200–600 | `load_config()`, JSON validation, CLI parsing |
| HTTP client | 600–1100 | curl_cffi wrapper, TLS fingerprinting, proxy |
| Stripe protocol | 1100–3000 | init / lookup / confirm / 3DS / poll |
| ChatGPT auth | 3000–4500 | session management, access_token refresh |
| Camoufox | 4500–6000 | PayPal browser flow, secondary login OAuth |
| Exceptions + main entry | 6000–8000 | exception classification, daemon hooks, command entry |

#### `hcaptcha_auto_solver.py` (approximately 4000 lines standalone file)

**Communicates with `card.py` via subprocess, not import.** Reason is that ML dependencies (torch / CLIP / opencv) are installed in a separate venv, isolated from the main program's venv.

See [`hcaptcha-solver.md`](hcaptcha-solver.md) for details.

#### Others

- **`hcaptcha_bridge_helper.py`**: CLI tool that, after connecting to hCaptcha bridge, allows manual screenshot / click / submit operations for debugging
- **`local_mock_gateway.py`**: Local HTTP mock server that simulates Stripe state machine (challenge_pass_then_decline / challenge_failed / no_3ds_card_declined)
- **`retry_house_decline.py`**: Retry wrapper specialized for replaying "direct decline" rather than "entering challenge"

### `CTF-reg/` —— ChatGPT Registration Subsystem

Invoked by `card.py::auto_register`, registers ChatGPT account from scratch and obtains access_token.

| File | Responsibility |
|---|---|
| `browser_register.py` | Camoufox real browser registration main path, passes Cloudflare Turnstile |
| `auth_flow.py` | Pure HTTP registration path, fallback (incomplete coverage) |
| `sentinel.py` | OpenAI Sentinel PoW token generation (browser fingerprint simulation + SHA-3) |
| `mail_provider.py` | catch-all mailbox generation + delegate KV to fetch OTP |
| `cf_kv_otp_provider.py` | Read OTP from CF KV written by worker (replaces IMAP) |
| `http_client.py` | HTTP client factory, prioritizes curl_cffi for TLS fingerprinting |
| `config.py` | dataclass configuration definitions |

### `pipeline.py` —— Orchestrator

Chains `CTF-reg/` and `CTF-pay/` together, exposing four modes externally:

| Mode | Entry Function |
|---|---|
| Single run | `pipeline()` |
| Batch parallel | `batch()` |
| Self-dealing | `self_dealer()` |
| Daemon persistent | `daemon()` |

See [`operating-modes.md`](operating-modes.md) for details.

---

## Protocol Pipeline Details

### Stripe Checkout Complete Pipeline```
init
 → elements/sessions
   → consumers/sessions/lookup
     → address / tax_region update
       → confirm
         (inline_payment_method_data or shared_payment_method mode)
         → 3ds2/authenticate
           → poll
```# Easy pitfalls to avoid:

- `setatt_` / `source` having a value does not mean success, it only means we obtained the 3DS authenticate source
- `state = challenge_required` and `ares.transStatus = C` means **the browser side needs to continue completing the challenge**, not a dead card
- Only after the browser truly completes the challenge will the subsequent intent / setup_intent status progress

### PayPal billing agreement complete flow```
B1: Enter protocol authorization page (Stripe redirect)
 → B-DDC: Device fingerprint collection (including possible DataDome slider)
   → B2: Email + password login
     → B3: Protocol authorization consent
       → B6: hermes path
         → B7: funding selection
           → B8: redirect back to Stripe
```DataDome slider appears at B-DDC or B6. Daemon mode has automatic dragging (see [`daemon-mode.md`](daemon-mode.md)).

### Codex OAuth + PKCE Secondary Login

After successful payment, start a new Camoufox instance and open the Codex authorize URL:```
GET https://auth.openai.com/oauth/authorize
  ?client_id=YOUR_OPENAI_CODEX_CLIENT_ID
  &redirect_uri=http://localhost:1455/auth/callback
  &codex_cli_simplified_flow=true
  &code_challenge=<PKCE>
  &state=<random>
```# Process Flow:

1. Enter email + password
2. May trigger email OTP (CF Email Worker → KV, millisecond-level storage)
3. Click Continue on Codex consent page
4. Playwright route intercepts `localhost:1455` callback, extracts `code`
5. POST `/oauth/token` with `code_verifier` → obtain `refresh_token`

---

## Exception Classification```python
# Core exceptions defined in CTF-pay/card.py
CheckoutSessionInactive     # Stripe session became inactive
ChallengeReconfirmRequired  # hCaptcha result expired
FreshCheckoutAuthError      # ChatGPT side credential / account issue
DatadomeSliderError         # PayPal DataDome slider solving failed
WebshareQuotaExhausted      # Webshare proxy replacement quota exhausted
```For each exception's recovery strategy, see [`debugging.md`](debugging.md#common-exceptions).

---

## Data Flow

### `output/webui.db`

Runtime account / payment / OAuth status are stored in SQLite database `output/webui.db`. Main tables include:

- `registered_accounts`: Complete credentials of successfully registered accounts (`password` / `access_token` / `session_token` / `device_id` / cookies, etc.)
- `pipeline_results`: Result summary of pipeline single / batch / self-dealer runs
- `card_results`: Payment end state and field-filling results of `card.py`
- `oauth_status`: OAuth state machine during free-only / RT maintenance

> This is runtime data; JSONL is no longer used as primary storage.

### `SQLite runtime_meta[daemon_state]`

State snapshot of daemon mode (for resuming after restart):```json
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
  "current_zone": "zone-a.example",
  "zone_ip_rotations": 0,
  "last_stats": { "total_active": 44, "usable": 38, "no_invite_permission": 5 }
}
```## Boundaries with External Systems

| System | Function | Required |
|---|---|---|
| **OpenAI** | Register + login to ChatGPT, obtain OAuth refresh_token | ✅ Required |
| **Stripe** | Checkout session flow | ✅ Required |
| **PayPal** | Payment settlement | ✅ (unless using pure card payment) |
| **Cloudflare** | Catch-all email subdomains, Turnstile verification during registration | ✅ Required |
| **Captcha platform** (compatible with createTask/getTaskResult protocol) | Passive captcha + fallback | Optional (browser passive captcha takes priority, platform serves as fallback only) |
| **Webshare** (or custom proxy) | Exit IP | ✅ Required |
| **VLM endpoint** | hCaptcha solving | Optional (residential/pseudo-residential exit IPs typically don't trigger it; falls back to CLIP without VLM) |
| **gpt-team / CPA** | Push to downstream management system | Optional |

All boundaries can be toggled or swapped in config.