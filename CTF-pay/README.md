# CTF Scenario Current Protocol Main Chain

Based on real packet captures, organized as follows:

- `init`
- `elements/sessions`
- `consumers/sessions/lookup`
- `payment_pages/<session>` address / tax_region update
- `confirm`
  - Prioritize `inline_payment_method_data`
  - Compatible with `shared_payment_method`
- `3ds2/authenticate`
- `poll`

## Several Easy-to-Misinterpret Points:

- `setatt_` / `source` having values only means 3DS authenticate source was obtained, not that it succeeded.
- `state = challenge_required` and `ares.transStatus = C` means **browser side needs to continue completing the challenge**, not a "dead card".
- Only after the browser truly completes the challenge will the intent / setup_intent status continue to advance.

## Current Script Behavior:

- Will record in `three_ds_result`:
  - `state`
  - `trans_status`
  - `source`
  - `acs_url`
  - `creq`
  - `three_ds_server_trans_id`
- If entering `challenge_required`, the script will pause here, waiting for subsequent browser-side challenge.

## Challenge Debugging in Headless Environment:

- `card.py` now writes the latest bridge info to `/tmp/stripe_hcaptcha_bridge_latest.json`
- If the machine has no `DISPLAY`, `card.py` no longer crashes directly; can fall back to headless Playwright or only output bridge URL
- Local helper provided: `CTF-pay/hcaptcha_bridge_helper.py`
  - Usage:
    - `python hcaptcha_bridge_helper.py http://127.0.0.1:PORT/index.html`
  - Common commands:
    - `TEXT`
    - `SHOT /tmp/bridge.png`
    - `CHSHOT /tmp/challenge.png`
    - `ECLICK x y`
    - `VERIFY`
    - `STATE`

## Most Relevant Fields in `config.auto.json`:

- `runtime.confirm_mode`
  - `inline_payment_method_data`: Closer to current real frontend flow
  - `shared_payment_method`: Compatible with older flow of creating `payment_method` then confirming
- `runtime.version`
- `runtime.js_checksum`
- `runtime.rv_timestamp`

**Note:**

- `runtime.js_checksum` and `runtime.rv_timestamp` must align with current checkout runtime.
- `top_checkout_config_id` / `payment_method_checkout_config_id` can be left empty; script will auto-fill from current session context.
- Default `load_config()` prioritizes reading the passed path; if not found, automatically falls back to `CTF-pay/config.auto.json`.

## Fresh Checkout Auto-Generation

`card.py` now supports regenerating a new checkout from ChatGPT side before session expires:

- Commands:
  - `python card.py fresh --fresh-only`
  - `python card.py auto`
  - `python card.py --fresh`
  - Or directly use preset scheme 1 config:
    - `python card.py auto --config config.auto-register.json`
- **ABCard / access_token mode recommended by default**, no longer relies on extracting login state from `flows`:
  - `Authorization: Bearer <access_token>`
  - Optional `__Secure-next-auth.session-token`
  - Optional `oai-device-id`
  - First call `GET /api/auth/session` to refresh / validate access token
  - Then sequentially try ABCard compatible flow:
    - `POST /backend-api/payments/checkout`
    - `POST /backend-api/subscriptions/checkout`
- ABCard style payload:
  - `plan_type`
  - `payment_lower_bound_amount_cents`
  - `payment_upper_bound_amount_cents`
  - `billing_country_code`
  - `billing_currency_code`
  - `workspace_name`
  - `seat_quantity`
  - `promo_campaign_id`
- `flows` now serves only as **optional template source**:
  - When `request_style=modern` or explicitly enabling `use_flows_for_templates`
  - Will only read sentinel/body templates in `../flows`
- `config.auto.json` added new `fresh_checkout` section:
  - Defaults:
    - `fresh_checkout.auth.mode = "access_token"`
    - `fresh_checkout.request_style = "abcard"`
    - `fresh_checkout.bootstrap_from_flows = false`
  - Main fields to fill:
    - `fresh_checkout.auth.access_token`
    - `fresh_checkout.auth.session_token` (optional but recommended; script can auto-refresh access token)
    - `fresh_checkout.auth.device_id` / `oai_device_id` (optional)
    - `fresh_checkout.plan.plan_name`
    - `fresh_checkout.plan.promo_campaign_id`
  - Scheme 1 (recommended closed-loop):
    - `fresh_checkout.auth.auto_register.enabled = true`
    - `fresh_checkout.auth.auto_register.project_dir = "./CTF-reg"`
    - `fresh_checkout.auth.auto_register.config_path = "./CTF-reg/config.example.json"`
    - `CTF-reg` is the built-in registration flow code directory in this repo, no longer depends on external project paths
    - If `mode = "auto_register"`, script will first call local registration flow to get `access_token / session_token / device_id`, then generate fresh checkout
    - If keeping `mode = "access_token"` but enabling `auto_register.enabled = true`, when existing token expires / becomes invalid / account is deactivated, will also auto-register new account and retry
  - When `auto_refresh_on_inactive: true`, if Stripe returns `checkout_not_active_session`, script will auto-generate fresh checkout and continue
  - If you need to stabilize discount checkout, recommend also enabling:
    - `fresh_checkout.check_coupon_after_checkout = true`
      - After creating checkout, additionally call `GET /backend-api/promo_campaign/check_coupon`
      - Only for observing `eligible / not_eligible`, **not** treated as actual redeem flow
    - `fresh_checkout.expected_due = 0`
      - Don't check ChatGPT checkout preview, instead use Stripe `init.total_summary.due` as truth for validating discount hit
    - `fresh_checkout.auto_refresh_on_due_mismatch = true`
      - If fresh checkout created successfully but Stripe `due` is not expected amount, script will auto-regenerate fresh checkout and retry
    - `fresh_checkout.max_due_mismatch_refreshes = 3`
      - Controls max number of refreshes on amount mismatch
  - `pre_solve_passive_captcha = true`
    - More aligned with real `flows`: get `passive_captcha_token` before confirming
    - In historical `due=0` flow, confirm request included `passive_captcha_token`
  - `browser_challenge.use_for_passive_captcha = true`
    - Will prioritize trying local headless browser to execute Stripe invisible hCaptcha, maximizing getting passive token closer to real frontend
    - If browser approach doesn't get token, falls back to captcha service
  - `browser_challenge.passive_headless = true`
    - Can run passive captcha browser flow even in headless environments without DISPLAY
  - `browser_challenge.passive_timeout_ms = 45000`
    - Controls browser wait duration for invisible/passive captcha

**Note:**

- If `/api/auth/session` still returns user info, but checkout returns:
  - `401[token_invalidated]`: Current access_token / session_token login state revoked;
  - `401[account_deactivated]`: Current account itself deactivated;
  - Both cases are not Stripe protocol issues, but ChatGPT side credential / account status issues.

## Pure Local CTF Mock Gateway

If current target is to further solidify **local challenge / 3DS state machine** without relying on any external network, directly use:

- `python card.py auto --config config.local-mock.json --local-mock`

Behavior:

- Auto rebuild fresh checkout parameters from local `flows`;
- Start a local HTTP mock gateway on `127.0.0.1`;
- `card.py` initiates real HTTP requests to local gateway, replaying:
  - `fresh checkout`
  - `confirm`
  - `verify_challenge`
  - `3ds2/authenticate`
  - `poll`

Default scenario:

- `challenge_pass_then_decline`
  - Local mock `network_checkcaptcha(pass=true)`
  - `verify_challenge -> requires_action`
  - `3DS2 -> succeeded`
  - Finally `card_declined`

Also supports:

- `challenge_failed`
- `no_3ds_card_declined`

Replay artifacts written to by default:

- `/tmp/ctf_local_mock_latest.json`

## GoPay WhatsApp OTP Auto-Receive

GoPay linking sends OTP to WhatsApp. Old flow only supported CLI / WebUI manual input; now `gopay.py` supports auto-receiving from WebUI WhatsApp login sidecar, local HTTP relay, state/log file or command.

### 1. WebUI Recommended Path: Only Expose One WhatsApp Login Entry

WebUI GoPay config page shows only one entry:```text
WhatsApp Login / QR Code Scanning to Receive GoPay OTP
```Click to enter `/whatsapp`, then scan the QR code to log in to WhatsApp. On the login page, you can freely choose between the `baileys` or `wwebjs` engine: Baileys (`@whiskeysockets/baileys`) is recommended by default, which directly listens to the WhatsApp multi-device socket. If you need to fall back to the legacy `whatsapp-web.js`/Chromium path, simply select `whatsapp-web.js` from the dropdown on the page and restart the sidecar. `WEBUI_WA_ENGINE=wwebjs` can still be used as the default value on first startup. The sidecar will listen for new messages, extract GoPay OTP, and write to:```text
SQLite runtime_meta[wa_state] / HTTP relay
```Note: The GoPay/WhatsApp OTP template is sometimes marked as a sensitive message by WhatsApp. Linked devices (WhatsApp Web) can only see placeholder hints like "You received a one-time password, which can only be viewed on the primary device", and cannot retrieve the OTP message content. This is not a regex parsing issue, but rather WhatsApp Web not delivering the OTP message content. The WebUI runner will automatically pop up a GoPay OTP fallback input box when `[gopay] waiting WhatsApp OTP from file: ...` appears in the payment log; after entering the verification code seen on the mobile primary device, it will be written to the same `SQLite runtime_meta[wa_state] / HTTP relay`, and the payment process continues.

When exporting the GoPay configuration via WebUI, it will automatically write:```json
{
  "gopay": {
    "otp": {
      "source": "file",
      "path": "SQLite runtime_meta[wa_state] / HTTP relay",
      "timeout": 300,
      "interval": 1
    }
  }
}
```sidecar dependency in `webui/whatsapp_relay/`:```bash
cd webui/whatsapp_relay
npm install
```Current dependencies include:

- `@whiskeysockets/baileys`: default engine;
- `whatsapp-web.js`: fallback engine.

### 2. Optional: Standalone HTTP relay

The repository includes a minimal HTTP relay that only receives WhatsApp webhooks / notification forwarding content under your own control, extracts the 6-digit OTP, writes it to `SQLite runtime_meta[wa_state]`, and exposes `/latest` for the payment flow to poll:```bash
python CTF-pay/whatsapp_otp_relay.py --port 8765
```# Local Quick Self-Testing:```bash
curl -X POST http://127.0.0.1:8765/ingest \
  -H 'Content-Type: application/json' \
  -d '{"from":"gopay","text":"Kode verifikasi GoPay Anda adalah 123456"}'

curl http://127.0.0.1:8765/latest
````/webhook` is compatible with common Meta WhatsApp Cloud API webhook formats:

- `GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`
  Used for webhook verification;
- `POST /webhook` receives messages from `entry[].changes[].value.messages[]`.

If you use Android notification forwarding, already have a WhatsApp Web bridge, or other self-built relay, you can simply POST message text to `/ingest`, or provide your own `/latest` endpoint that returns the latest OTP.

### 3. Manual configuration of `gopay.otp`

See example in `CTF-pay/config.gopay.example.json`:```json
{
  "gopay": {
    "country_code": "62",
    "phone_number": "81234567890",
    "pin": "YOUR_6_DIGIT_GOPAY_PIN",
    "otp": {
      "source": "file",
      "path": "SQLite runtime_meta[wa_state] / HTTP relay",
      "timeout": 300,
      "interval": 1,
      "code_regex": "(?<!\\d)(\\d{6})(?!\\d)",
      "issued_after_slack_s": 15
    }
  }
}
```Also supports file polling:```json
{
  "gopay": {
    "otp": {
      "source": "file",
      "path": "SQLite runtime_meta[wa_state]",
      "timeout": 300,
      "interval": 1
    }
  }
}
```HTTP relay polling:```json
{
  "gopay": {
    "otp": {
      "source": "http",
      "url": "http://127.0.0.1:8765/latest",
      "timeout": 300,
      "interval": 1
    }
  }
}
```Or command polling:```json
{
  "gopay": {
    "otp": {
      "source": "command",
      "command": ["python", "scripts/get_latest_wa_otp.py"],
      "timeout": 300,
      "interval": 2
    }
  }
}
```### 4. Running

CLI / pipeline will prioritize using `gopay.otp` as long as `--gopay-otp-file` is not passed:```bash
python CTF-pay/gopay.py --config CTF-pay/config.gopay.example.json
python CTF-pay/card.py auto --config CTF-pay/config.paypal.json --gopay
python pipeline.py --config CTF-pay/config.paypal.json --gopay
```In WebUI mode, if the configuration contains a non-manual `gopay.otp`, the runner will skip the old `--gopay-otp-file` manual popup and let `gopay.py` directly poll the automatic OTP provider. If the automatic provider is waiting for a file path, the runner will also recognize the wait log and open the same manual fallback popup to handle WhatsApp Web's "primary device visible" placeholder message. If `gopay.otp` is not configured, the behavior remains unchanged: a manual input popup appears on the run page for WhatsApp OTP.