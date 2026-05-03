"""Tests for CTF-pay/gopay.py — GoPay tokenization charger.

All HTTP endpoints are mocked via the `responses` library; no live network.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
import requests
import responses


# Load CTF-pay/gopay.py directly (path with hyphen — can't `from CTF-pay`)
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("gopay_mod", ROOT / "CTF-pay" / "gopay.py")
gopay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gopay)  # type: ignore[union-attr]

# `responses` mocks the requests library only; force gopay.py to use plain
# requests in tests so HTTP mocks are intercepted (production uses curl_cffi
# for chrome TLS fingerprint to bypass Cloudflare).
gopay._CurlCffiSession = None


CS_ID = "cs_live_a1test123456789"
PM_ID = "pm_1Ttesta"
SNAP_TOKEN = "11111111-aaaa-bbbb-cccc-222222222222"
LINK_REF = "33333333-dddd-eeee-ffff-444444444444"
LINKING_ID = "55555555-9999-8888-7777-666666666666"
CHARGE_REF = "A120260429000000TEST123"
CHALLENGE_ID = "77777777-8888-9999-aaaa-bbbbcccccccc"
CHALLENGE_ID2 = "99999999-1111-2222-3333-444444444444"
PIN_JWT_LINK = "eyJ0eXAi.linktoken.xxx"
PIN_JWT_CHARGE = "eyJ0eXAi.chargetoken.yyy"
STRIPE_PK = "pk_live_test_xxx"


# ────────────────── helper to build a charger with mocks ──────────────────


def build_charger(otp_value: str = "123456", pin: str = "654321") -> gopay.GoPayCharger:
    cs_session = requests.Session()
    cs_session.headers["Cookie"] = "__Secure-next-auth.session-token=fake"
    cfg = {"country_code": "86", "phone_number": "00000000000", "pin": pin}
    return gopay.GoPayCharger(cs_session, cfg, otp_provider=lambda: otp_value, log=lambda _m: None)


# ────────────────── full happy path ──────────────────


@responses.activate
def test_full_flow_succeeds():
    # Step 1: chatgpt /payments/checkout
    responses.post(
        "https://chatgpt.com/backend-api/payments/checkout",
        json={"id": CS_ID, "session_id": CS_ID},
    )
    # Step 2: stripe payment_methods
    responses.post(
        "https://api.stripe.com/v1/payment_methods",
        json={"id": PM_ID, "type": "gopay"},
    )
    responses.post(f"https://api.stripe.com/v1/payment_pages/{CS_ID}/init", json={"init_checksum": "fake_ic"})
    # Step 3: stripe confirm
    responses.post(
        f"https://api.stripe.com/v1/payment_pages/{CS_ID}/confirm",
        json={"payment_status": "open"},
    )
    # Step 4: chatgpt approve
    responses.post(
        "https://chatgpt.com/backend-api/payments/checkout/approve",
        json={"result": "approved"},
    )
    # Step 5a: payment_pages refetch → setup_intent.next_action.redirect_to_url.url
    pm_redirect_url = f"https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_{SNAP_TOKEN[:10]}"
    responses.get(
        f"https://api.stripe.com/v1/payment_pages/{CS_ID}",
        json={
            "setup_intent": {
                "status": "requires_action",
                "next_action": {
                    "redirect_to_url": {"url": pm_redirect_url, "return_url": "https://chatgpt.com/checkout/verify"},
                },
            },
        },
    )
    # Step 5b: pm-redirects → 302 to midtrans
    responses.get(
        pm_redirect_url,
        status=302,
        headers={"Location": f"https://app.midtrans.com/snap/v4/redirection/{SNAP_TOKEN}"},
    )
    # Step 6: midtrans transactions
    responses.get(
        f"https://app.midtrans.com/snap/v1/transactions/{SNAP_TOKEN}",
        json={"enabled_payments": [{"type": "gopay"}, {"type": "qris"}]},
    )
    # Step 7: midtrans linking — first 406 then 201
    responses.post(
        f"https://app.midtrans.com/snap/v3/accounts/{SNAP_TOKEN}/linking",
        json={"error_messages": ["account already linked"]},
        status=406,
    )
    responses.post(
        f"https://app.midtrans.com/snap/v3/accounts/{SNAP_TOKEN}/linking",
        json={
            "status_code": "201",
            "activation_link_url": (
                f"https://merchants-gws-app.gopayapi.com/app/authorize?reference={LINK_REF}&target=gwc"
            ),
        },
        status=201,
    )
    # Step 8-10: GoPay linking
    responses.post(
        "https://gwa.gopayapi.com/v1/linking/validate-reference",
        json={"success": True, "data": {"reference_id": LINK_REF, "next_action": "linking-user-consent"}},
    )
    responses.post(
        "https://gwa.gopayapi.com/v1/linking/user-consent",
        json={"success": True, "data": {"next_action": "linking-validate-otp"}},
    )
    responses.post(
        "https://gwa.gopayapi.com/v1/linking/validate-otp",
        json={
            "success": True,
            "data": {
                "next_action": "linking-validate-pin",
                "challenge": {
                    "action": {
                        "type": "GOPAY_PIN_CHALLENGE",
                        "value": {
                            "challenge_id": CHALLENGE_ID,
                            "client_id": gopay.GOPAY_PIN_CLIENT_ID_LINK,
                            "redirect_uri": "https://pin-web-client.gopayapi.com/auth/pin/verify?...",
                        },
                    },
                },
            },
        },
    )
    # Step 11: PIN tokenize (linking)
    responses.post(
        "https://customer.gopayapi.com/api/v1/users/pin/tokens/nb",
        json={"token": PIN_JWT_LINK},
    )
    # Step 12: validate-pin
    responses.post(
        "https://gwa.gopayapi.com/v1/linking/validate-pin",
        json={
            "success": True,
            "data": {
                "next_action": "linking-success",
                "redirect_url": f"https://app.midtrans.com/snap/v3/callback/gopay/linking/{LINKING_ID}?success=true",
            },
        },
    )
    # Step 13: midtrans charge create
    responses.post(
        f"https://app.midtrans.com/snap/v2/transactions/{SNAP_TOKEN}/charge",
        json={
            "status_code": "201",
            "transaction_status": "pending",
            "gopay_verification_link_url": f"https://merchants-gws-app.gopayapi.com/app/challenge?reference={CHARGE_REF}",
        },
    )
    # Step 14: payment validate / confirm / process
    responses.get(
        f"https://gwa.gopayapi.com/v1/payment/validate?reference_id={CHARGE_REF}",
        json={"success": True, "data": {"merchant_name": "OpenAI LLC"}},
    )
    responses.post(
        f"https://gwa.gopayapi.com/v1/payment/confirm?reference_id={CHARGE_REF}",
        json={
            "success": True,
            "data": {
                "next_action": "payment-validate-pin",
                "challenge": {
                    "action": {
                        "type": "GOPAY_PIN_CHALLENGE",
                        "value": {"challenge_id": CHALLENGE_ID2, "client_id": gopay.GOPAY_PIN_CLIENT_ID_CHARGE},
                    },
                },
            },
        },
    )
    # Second pin tokenize call
    responses.post(
        "https://customer.gopayapi.com/api/v1/users/pin/tokens/nb",
        json={"token": PIN_JWT_CHARGE},
    )
    responses.post(
        f"https://gwa.gopayapi.com/v1/payment/process?reference_id={CHARGE_REF}",
        json={"success": True, "data": {"next_action": "payment-success"}},
    )
    # Step 15: chatgpt verify
    responses.get(
        "https://chatgpt.com/checkout/verify",
        json={"state": "verified"},
    )

    charger = build_charger()
    result = charger.run(stripe_pk=STRIPE_PK)
    assert result["state"] == "succeeded"
    assert result["cs_id"] == CS_ID


# ────────────────── 406 retry exhausted ──────────────────


@responses.activate
def test_linking_406_exhaustion_raises():
    # Pre-flow: stub the early steps minimally so we get to linking
    responses.post("https://chatgpt.com/backend-api/payments/checkout", json={"id": CS_ID, "session_id": CS_ID})
    responses.post("https://api.stripe.com/v1/payment_methods", json={"id": PM_ID})
    responses.post(f"https://api.stripe.com/v1/payment_pages/{CS_ID}/init", json={"init_checksum": "fake_ic"})
    responses.post(f"https://api.stripe.com/v1/payment_pages/{CS_ID}/confirm", json={"payment_status": "open"})
    responses.post("https://chatgpt.com/backend-api/payments/checkout/approve", json={"result": "approved"})
    pm_redirect_url2 = f"https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_{SNAP_TOKEN[:10]}"
    responses.get(
        f"https://api.stripe.com/v1/payment_pages/{CS_ID}",
        json={"setup_intent": {"status": "requires_action",
              "next_action": {"redirect_to_url": {"url": pm_redirect_url2}}}},
    )
    responses.get(
        pm_redirect_url2,
        status=302,
        headers={"Location": f"https://app.midtrans.com/snap/v4/redirection/{SNAP_TOKEN}"},
    )
    responses.get(
        f"https://app.midtrans.com/snap/v1/transactions/{SNAP_TOKEN}",
        json={"enabled_payments": [{"type": "gopay"}]},
    )
    # All linking attempts return 406
    for _ in range(gopay.LINK_RETRY_LIMIT + 1):
        responses.post(
            f"https://app.midtrans.com/snap/v3/accounts/{SNAP_TOKEN}/linking",
            json={"error_messages": ["account already linked"]},
            status=406,
        )

    charger = build_charger()
    with pytest.raises(gopay.GoPayError, match="exhausted"):
        charger.run(stripe_pk=STRIPE_PK)


# ────────────────── OTP cancel ──────────────────


@responses.activate
def test_otp_provider_cancel_raises():
    responses.post("https://chatgpt.com/backend-api/payments/checkout", json={"id": CS_ID, "session_id": CS_ID})
    responses.post("https://api.stripe.com/v1/payment_methods", json={"id": PM_ID})
    responses.post(f"https://api.stripe.com/v1/payment_pages/{CS_ID}/init", json={"init_checksum": "fake_ic"})
    responses.post(f"https://api.stripe.com/v1/payment_pages/{CS_ID}/confirm", json={"payment_status": "open"})
    responses.post("https://chatgpt.com/backend-api/payments/checkout/approve", json={"result": "approved"})
    pm_redirect_url3 = f"https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_{SNAP_TOKEN[:10]}"
    responses.get(
        f"https://api.stripe.com/v1/payment_pages/{CS_ID}",
        json={"setup_intent": {"status": "requires_action",
              "next_action": {"redirect_to_url": {"url": pm_redirect_url3}}}},
    )
    responses.get(
        pm_redirect_url3,
        status=302,
        headers={"Location": f"https://app.midtrans.com/snap/v4/redirection/{SNAP_TOKEN}"},
    )
    responses.get(
        f"https://app.midtrans.com/snap/v1/transactions/{SNAP_TOKEN}",
        json={"enabled_payments": [{"type": "gopay"}]},
    )
    responses.post(
        f"https://app.midtrans.com/snap/v3/accounts/{SNAP_TOKEN}/linking",
        json={"status_code": "201",
              "activation_link_url": f"https://merchants-gws-app.gopayapi.com/app/authorize?reference={LINK_REF}&target=gwc"},
        status=201,
    )
    responses.post(
        "https://gwa.gopayapi.com/v1/linking/validate-reference",
        json={"success": True, "data": {"reference_id": LINK_REF}},
    )
    responses.post(
        "https://gwa.gopayapi.com/v1/linking/user-consent",
        json={"success": True, "data": {"next_action": "linking-validate-otp"}},
    )

    cs_session = requests.Session()
    cs_session.headers["Cookie"] = "__Secure-next-auth.session-token=x"
    charger = gopay.GoPayCharger(
        cs_session,
        {"country_code": "86", "phone_number": "00000000000", "pin": "111111"},
        otp_provider=lambda: "",  # cancelled / empty
        log=lambda _m: None,
    )
    with pytest.raises(gopay.OTPCancelled):
        charger.run(stripe_pk=STRIPE_PK)


# ────────────────── PIN rejected ──────────────────


@responses.activate
def test_pin_rejected_raises():
    responses.post("https://chatgpt.com/backend-api/payments/checkout", json={"id": CS_ID, "session_id": CS_ID})
    responses.post("https://api.stripe.com/v1/payment_methods", json={"id": PM_ID})
    responses.post(f"https://api.stripe.com/v1/payment_pages/{CS_ID}/init", json={"init_checksum": "fake_ic"})
    responses.post(f"https://api.stripe.com/v1/payment_pages/{CS_ID}/confirm", json={"payment_status": "open"})
    responses.post("https://chatgpt.com/backend-api/payments/checkout/approve", json={"result": "approved"})
    pm_redirect_url4 = f"https://pm-redirects.stripe.com/authorize/acct_test/sa_nonce_{SNAP_TOKEN[:10]}"
    responses.get(
        f"https://api.stripe.com/v1/payment_pages/{CS_ID}",
        json={"setup_intent": {"status": "requires_action",
              "next_action": {"redirect_to_url": {"url": pm_redirect_url4}}}},
    )
    responses.get(
        pm_redirect_url4,
        status=302,
        headers={"Location": f"https://app.midtrans.com/snap/v4/redirection/{SNAP_TOKEN}"},
    )
    responses.get(
        f"https://app.midtrans.com/snap/v1/transactions/{SNAP_TOKEN}",
        json={"enabled_payments": [{"type": "gopay"}]},
    )
    responses.post(
        f"https://app.midtrans.com/snap/v3/accounts/{SNAP_TOKEN}/linking",
        json={"status_code": "201",
              "activation_link_url": f"https://merchants-gws-app.gopayapi.com/app/authorize?reference={LINK_REF}&target=gwc"},
        status=201,
    )
    responses.post("https://gwa.gopayapi.com/v1/linking/validate-reference",
                   json={"success": True, "data": {"reference_id": LINK_REF}})
    responses.post("https://gwa.gopayapi.com/v1/linking/user-consent",
                   json={"success": True, "data": {}})
    responses.post(
        "https://gwa.gopayapi.com/v1/linking/validate-otp",
        json={
            "success": True,
            "data": {"challenge": {"action": {"value": {
                "challenge_id": CHALLENGE_ID, "client_id": gopay.GOPAY_PIN_CLIENT_ID_LINK,
            }}}},
        },
    )
    responses.post(
        "https://customer.gopayapi.com/api/v1/users/pin/tokens/nb",
        json={"error": "pin_invalid"},
        status=401,
    )

    charger = build_charger(pin="000000")
    with pytest.raises(gopay.GoPayPINRejected):
        charger.run(stripe_pk=STRIPE_PK)


# ────────────────── file-watch OTP provider ──────────────────


def test_file_watch_otp_provider(tmp_path):
    watch = tmp_path / "otp.txt"
    provider = gopay.file_watch_otp_provider(watch, timeout=5.0)

    # Write OTP from a "background" thread
    import threading
    def writer():
        time.sleep(0.2)
        watch.write_text("987654")

    threading.Thread(target=writer, daemon=True).start()
    val = provider()
    assert val == "987654"
    assert not watch.exists()  # provider unlinks after read


def test_file_watch_otp_timeout(tmp_path):
    watch = tmp_path / "otp.txt"
    provider = gopay.file_watch_otp_provider(watch, timeout=0.5)
    with pytest.raises(gopay.OTPCancelled):
        provider()


# ────────────────── WhatsApp auto OTP providers ──────────────────


def test_extract_otp_from_whatsapp_text():
    text = "Kode verifikasi GoPay Anda adalah 123456. Jangan bagikan kode ini."
    assert gopay._extract_otp_from_text(text) == "123456"


def test_whatsapp_file_otp_provider_reads_state(tmp_path):
    state = tmp_path / "wa_state.json"
    provider = gopay.whatsapp_file_otp_provider(
        state,
        timeout=5.0,
        interval=0.1,
        log=lambda _m: None,
    )

    import threading
    def writer():
        time.sleep(0.2)
        state.write_text(
            '{"latest":{"otp":"246810","ts": %s, "text":"GoPay code 246810"}}'
            % int(time.time()),
            encoding="utf-8",
        )

    threading.Thread(target=writer, daemon=True).start()
    assert provider() == "246810"


@responses.activate
def test_whatsapp_http_otp_provider_reads_latest():
    url = "http://127.0.0.1:8765/latest"
    responses.get(url, status=204)
    responses.get(
        url,
        json={"otp": "135790", "ts": int(time.time()), "text": "GoPay OTP 135790"},
    )
    provider = gopay.whatsapp_http_otp_provider(
        url,
        timeout=5.0,
        interval=0.1,
        log=lambda _m: None,
    )
    assert provider() == "135790"


# ────────────────── chatgpt session builder ──────────────────


def test_build_chatgpt_session_needs_auth():
    with pytest.raises(gopay.GoPayError):
        gopay._build_chatgpt_session({})


def test_build_chatgpt_session_with_token():
    s = gopay._build_chatgpt_session({"session_token": "abc123"})
    assert "__Secure-next-auth.session-token=abc123" in s.headers["Cookie"]


def test_build_chatgpt_session_with_cookie_header():
    s = gopay._build_chatgpt_session({"cookie_header": "foo=bar; baz=qux"})
    assert "foo=bar" in s.headers["Cookie"]
    assert "baz=qux" in s.headers["Cookie"]
