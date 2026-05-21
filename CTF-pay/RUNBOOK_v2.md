# GoPay X-E1 v2 Signer — Runbook

## What's working (verified 2026-05-14)

- **Algorithm**: HMAC-SHA256(K, real_msg) + HMAC chain cipher + device-static X-E2
- **Live server probe**: `customer.gopayapi.com/v1/support/customer/activity` → HTTP 200 ✓
- **Reach test**: PIN endpoint, gwa linking, Midtrans all reachable (signatures accepted)
- **14-test regression suite**: all PASS
- **Committed**: dev branch commit `6e8803d` (no push, per policy)

## To run a live payment, choose ONE path

### Path A — ChatGPT Plus full 15-step

```bash
cd /root/Gpt-Agreement-Payment

# 1. Edit config — add v2 signer block
vim CTF-pay/config.gopay.example.json
# Set:
#   gopay.protocol.enabled = true
#   gopay.protocol.sign_version = "v2"
#   gopay.protocol.extra.display_encoder_key = "<K from extract_K.py>"
#   gopay.protocol.extra.signed_msg_template = "/tmp/big_msg_1867.bin"
#   gopay.pin = "<6-digit GoPay PIN>"
#   gopay.phone_number = "<linked phone>"
#   gopay.otp.source = "auto"
#   fresh_checkout.auth.session_token = "<ChatGPT session token>"
# Save as config.json (rename from .example)

# 2. Run pipeline
python3 pipeline.py --gopay --plan plus --config CTF-pay/config.json
```

### Path B — Direct GoPay API call (no Stripe / Midtrans)

```bash
# Test the PIN tokenization endpoint with real challenge_id
python3 CTF-pay/gopay_client.py POST customer.gopayapi.com/api/v1/users/pin/tokens/nb \
  --body '{"challenge_id":"<from gwa OTP flow>","client_id":"<from gwa OTP flow>","pin":"870657"}'
```

### Path C — Lowest-stakes proof (no money, just protocol)

```bash
# Hits a confirmed working endpoint to prove the signing stack works
python3 CTF-pay/gopay_client.py POST customer.gopayapi.com/v1/support/customer/activity --body '{}'
# Expected: HTTP 200 {"success":true}
```

## Extracting K (display_encoder_key)

```bash
# GoPay must be running. K is persistent across process restarts.
python3 output/gopay_2.8.0_extract/extract_K.py --verify "<known K>"
# OR if no known K, hook the SHA-82394 outer hash to capture from K^opad:
python3 output/gopay_2.8.0_extract/extract_K.py --hook
```

## Capturing signed_msg template

```bash
# Atomic capture during a real sign event (deeplink-driven)
adb shell am start -a android.intent.action.VIEW -d "gopay://envelope/home" -p com.gojek.gopay
python3 /tmp/hook_atomic_xe1.py  # captures fresh big_msg_1867.bin
```

## Algorithm summary

```python
# X-E1 = sha:cipher:D:ts
sha = HMAC-SHA256(displayEncoderKey, real_msg)  # real_msg = SSO+fields+cipher+T3_tail

# Deterministic chain constants
KEY_C = HMAC-SHA256(0_64B, 0x01*64)
KEY_D = HMAC-SHA256(KEY_C, 0x01*64)

# Per-request (with random 32B ASCII nonce)
K9     = HMAC-SHA256(KEY_C, KEY_D || 0x01*32 || KEY_C || nonce)
T1     = HMAC-SHA256(K9, KEY_D || 0x01*32)
cipher = HMAC-SHA256(K9, T1 || 0x01*32)

# X-E2 = device-static constant (from key_struct+0x18)
X-E2 = "ED9A2B38749FBDE9ACA61D6A685B7"  # changes per device install
```

## Tests

```bash
cd CTF-pay
python3 -m unittest tests.test_gopay_sign_v2 -v
# Expected: Ran 14 tests in <1s — OK
```

## Files map

| File | Purpose |
|---|---|
| `CTF-pay/gopay_sign_v2.py` | Algorithm reference |
| `CTF-pay/gopay_client.py` | HTTP client wrapper |
| `CTF-pay/gopay_protocol_pay.py` | Pipeline-aware client (use `sign_version='v2'`) |
| `CTF-pay/tests/test_gopay_sign_v2.py` | 14-test regression suite |
| `output/gopay_2.8.0_extract/extract_K.py` | Frida K extractor |
| `output/gopay_2.8.0_extract/xe1_algorithm_FINAL.md` | Full algorithm doc |
| `/tmp/big_msg_1867.bin` | Captured template (this device's) |
| `/tmp/sso_token.txt` | Captured SSO (this device's, may expire) |
