# whatsapp_relay

WhatsApp Web sidecar for `gopay.py` — auto-fetches OTPs from GoPay/GoJek
WhatsApp messages and writes them to a file the Python pipeline polls.

## Why

GoPay tokenization sends a 6-digit OTP via WhatsApp during merchant linking.
Without this relay, a human has to read the OTP off their phone and paste it
into the webui modal. With this relay running and the user's WhatsApp linked
once, OTP delivery is fully automatic.

## Install

```bash
cd CTF-pay/whatsapp_relay
npm install
```

Requires Node ≥ 18, Chromium runtime (puppeteer auto-downloads on `npm install`).

## Run (CLI smoke test)

**QR mode** (default — scan with phone):

```bash
WA_LOGIN_MODE=qr \
WA_STATE_FILE=/tmp/wa_state.json \
WA_OTP_FILE=/tmp/wa_otp.txt \
node index.js
```

State file gets a `qr` field with the raw payload + `qr_ascii` (terminal) +
`qr_data_url` (base64 PNG). Scan with WhatsApp → Settings → Linked Devices →
Link a device.

**Pairing-code mode** (no QR — type 8-char code in WhatsApp):

```bash
WA_LOGIN_MODE=pairing \
WA_PAIRING_PHONE=8617788949030 \
WA_STATE_FILE=/tmp/wa_state.json \
WA_OTP_FILE=/tmp/wa_otp.txt \
node index.js
```

`WA_PAIRING_PHONE` = country code + number, digits only.

State file gets a `code` field (e.g. `XQRA-2K8B`). On phone: WhatsApp →
Settings → Linked Devices → Link with phone number → enter that code.

## Env knobs

| Variable | Default | Notes |
|---|---|---|
| `WA_LOGIN_MODE` | `qr` | `qr` or `pairing` |
| `WA_PAIRING_PHONE` | — | required for pairing mode |
| `WA_STATE_FILE` | `/tmp/wa_state.json` | written on every state transition |
| `WA_OTP_FILE` | `/tmp/wa_otp.txt` | overwritten when OTP captured |
| `WA_SESSION_DIR` | `./.wwebjs-session` | LocalAuth persistent dir (~14d) |
| `WA_HEADLESS` | `1` | set `0` to see Chromium for debugging |
| `WA_OTP_SENDER_REGEX` | — | extra sender filter regex (case-insensitive) |

## Filtering

Out of the box matches sender or body containing `gojek` / `gopay` /
`midtrans` (case-insensitive) plus a 6-digit number. Override via
`WA_OTP_SENDER_REGEX`.

## State file shape

```json
{ "status": "awaiting_qr_scan", "login_mode": "qr", "qr": "...", "ts": 1700000000000 }
{ "status": "awaiting_pairing_code", "login_mode": "pairing", "code": "ABCD-1234", "phone": "861...", "ts": ... }
{ "status": "connected", "login_mode": "qr", "wid": "861...@c.us", "pushname": "...", "ts": ... }
{ "status": "disconnected", "reason": "...", "ts": ... }
```

## Webui integration

The webui's `/whatsapp` page polls `WA_STATE_FILE`, renders the QR / pairing
code, and shows live connection status. The `/run` page's gopay flow gets
its OTP via the relay's `WA_OTP_FILE` automatically (same path the manual
modal would write to), so the OTP modal becomes a fallback that only opens if
the relay isn't running.
