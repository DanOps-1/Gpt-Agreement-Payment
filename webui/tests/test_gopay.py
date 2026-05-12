"""Tests for CTF-pay/gopay.py — GoPay tokenization charger.

All HTTP endpoints are mocked via the `responses` library; no live network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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


def build_logged_charger(logs: list[str], otp_value: str = "123456", pin: str = "654321") -> gopay.GoPayCharger:
    cs_session = requests.Session()
    cs_session.headers["Cookie"] = "__Secure-next-auth.session-token=fake"
    cfg = {"country_code": "86", "phone_number": "00000000000", "pin": pin}
    return gopay.GoPayCharger(cs_session, cfg, otp_provider=lambda: otp_value, log=logs.append)


def build_sms_charger(logs: list[str], pin: str = "654321") -> gopay.GoPayCharger:
    cs_session = requests.Session()
    cs_session.headers["Cookie"] = "__Secure-next-auth.session-token=fake"
    cfg = {
        "country_code": "86",
        "phone_number": "00000000000",
        "pin": pin,
        "use_sms_otp": True,
        "sms_otp_poll_url": "https://sms.example/latest",
        "sms_otp_timeout": 5,
        "sms_otp_interval": 0.1,
    }
    return gopay.GoPayCharger(cs_session, cfg, otp_provider=lambda: "should-not-use", log=logs.append)


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
    responses.get(
        "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        json={"account": {"planType": "plus", "isActive": True}},
    )

    charger = build_charger()
    result = charger.run(stripe_pk=STRIPE_PK)
    assert result["state"] == "succeeded"
    assert result["cs_id"] == CS_ID


@responses.activate
def test_sms_otp_mode_resends_and_polls_six_digit_code():
    logs: list[str] = []
    charger = build_sms_charger(logs)
    responses.post(
        "https://gwa.gopayapi.com/v1/linking/resend-otp",
        json={"success": True, "data": {"next_action": "linking-validate-otp"}},
    )
    responses.get(
        "https://sms.example/latest",
        body=(
            "YES|(GOJEK) Ini OTP buat hubungkan OpenAI LLC ke GoPay. "
            "JANGAN KASIH KE SIAPA PUN. OTP: 927716 gojek.com/safety "
            "merchants-gws-app.gopayapi.com #927716"
        ),
    )

    charger._gopay_resend_sms_otp(LINK_REF)
    assert charger._poll_sms_otp() == "927716"
    resend = responses.calls[0].request
    assert resend.url == "https://gwa.gopayapi.com/v1/linking/resend-otp"
    assert resend.headers["x-user-locale"] == "zh-CN"
    assert b'"reference_id": "33333333-dddd-eeee-ffff-444444444444"' in resend.body


def test_sms_otp_extractor_requires_message_context():
    text = (
        "YES|(GOJEK) Ini OTP buat hubungkan OpenAI LLC ke GoPay. "
        "JANGAN KASIH KE SIAPA PUN. OTP: 927716 gojek.com/safety "
        "merchants-gws-app.gopayapi.com #927716"
    )

    assert gopay._extract_sms_otp_from_payload(text) == "927716"
    assert gopay._extract_sms_otp_from_payload({"status": 200, "id": "123456"}) == ""


@responses.activate
def test_sms_otp_provider_resends_while_relay_returns_no_code(monkeypatch):
    url = "https://sms.example/latest"
    for _ in range(6):
        responses.get(url, body="NO|暂时没有消息，如果60s后没有收到消息会进行重发，这个操作最多3次")
    responses.get(
        url,
        body=(
            "YES|(GOJEK) Ini OTP buat hubungkan OpenAI LLC ke GoPay. "
            "OTP: 927716 merchants-gws-app.gopayapi.com #927716"
        ),
    )
    now = [1000.0]
    resends: list[float] = []

    monkeypatch.setattr(gopay.time, "time", lambda: now[0])
    monkeypatch.setattr(gopay.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + float(seconds)))

    provider = gopay.sms_http_otp_provider(
        url,
        timeout=5.0,
        interval=0.2,
        resend_callback=lambda: resends.append(now[0]),
        resend_after_s=1.0,
        max_resends=2,
        log=lambda _m: None,
    )

    assert provider() == "927716"
    assert len(resends) == 1


def test_disabled_gopay_accounts_are_not_selected():
    logs: list[str] = []
    cfg = {
        "accounts": [
            {"label": "off", "country_code": "62", "phone_number": "811111111", "pin": "111111", "disabled": True},
            {"label": "on", "country_code": "62", "phone_number": "822222222", "pin": "222222"},
        ]
    }

    selected = gopay.pick_gopay_account_config(cfg, log=logs.append)

    assert selected["label"] == "on"
    assert selected["phone_number"] == "822222222"
    assert selected["_selected_accounts_count"] == 1


def test_all_disabled_gopay_accounts_are_unusable():
    cfg = {
        "country_code": "62",
        "phone_number": "899999999",
        "pin": "999999",
        "accounts": [
            {"label": "off", "country_code": "62", "phone_number": "811111111", "pin": "111111", "disabled": True},
        ],
    }

    assert gopay.normalize_gopay_accounts(cfg) == []
    with pytest.raises(gopay.GoPayError):
        gopay.pick_gopay_account_config(cfg)


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


def test_midtrans_linking_429_retries_then_success(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(gopay.time, "sleep", lambda s: sleeps.append(float(s)))
    def fake_uniform(a, b):
        assert a == 2.0
        assert b == 3.0
        return 2.5
    monkeypatch.setattr(gopay.random, "uniform", fake_uniform)

    class Resp:
        def __init__(self, status_code: int, text: str = "", body: dict | None = None):
            self.status_code = status_code
            self.text = text
            self._body = body or {}

        def json(self):
            return self._body

    class Ext:
        def __init__(self):
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                return Resp(429)
            return Resp(201, body={"activation_link_url": f"https://x.test/?reference={LINK_REF}"})

    charger = build_charger()
    ext = Ext()
    charger.ext = ext

    assert charger._midtrans_init_linking(SNAP_TOKEN) == LINK_REF
    assert ext.calls == 3
    assert sleeps == [2.5, 2.5]


# ────────────────── OTP cancel ──────────────────


def test_midtrans_linking_first_429_retries_without_realtime_429_log(monkeypatch):
    sleeps: list[float] = []
    logs: list[str] = []
    monkeypatch.setattr(gopay.time, "sleep", lambda s: sleeps.append(float(s)))
    monkeypatch.setattr(gopay.random, "uniform", lambda a, b: 2.5)

    class Resp:
        def __init__(self, status_code: int, text: str = "", body: dict | None = None):
            self.status_code = status_code
            self.text = text
            self._body = body or {}

        def json(self):
            return self._body

    class Ext:
        def __init__(self):
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Resp(429, text="too many requests")
            return Resp(201, body={"activation_link_url": f"https://x.test/?reference={LINK_REF}"})

    charger = build_logged_charger(logs)
    ext = Ext()
    charger.ext = ext

    assert charger._midtrans_init_linking(SNAP_TOKEN) == LINK_REF
    assert ext.calls == 2
    assert sleeps == [2.5]
    assert not [line for line in logs if "429" in line or "request_headers" in line or "rate limited" in line]


def test_midtrans_linking_prechecks_unbind_and_continues_when_no_links(monkeypatch):
    logs: list[str] = []
    calls: list[str] = []
    charger = gopay.GoPayCharger(
        requests.Session(),
        {
            "country_code": "86",
            "phone_number": "00000000000",
            "pin": "111111",
            "auto_unbind": {"raw_request": "GET /v1/linkedapps HTTP/2\nHost: customer.gopayapi.com\nx-m1: m1\n"},
        },
        otp_provider=lambda: "",
        log=logs.append,
    )

    class Resp:
        status_code = 201
        text = '{"status_code":"201"}'
        def json(self):
            return {"activation_link_url": f"https://x.test/?reference={LINK_REF}"}

    class Ext:
        def post(self, *args, **kwargs):
            calls.append("linking")
            return Resp()

    monkeypatch.setattr(gopay._gopay_auto_unbind, "fetch_linkedapps", lambda *a, **k: {
        "has_data": True,
        "status_code": 200,
        "body_json": {"data": {"linked_services": []}},
    })
    monkeypatch.setattr(gopay._gopay_auto_unbind, "run_from_gopay_config", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not unlink")))
    charger.ext = Ext()

    assert charger._midtrans_init_linking(SNAP_TOKEN) == LINK_REF
    assert calls == ["linking"]
    assert any("no existing binding" in line for line in logs)


def test_midtrans_linking_prechecks_unbind_when_links_exist(monkeypatch):
    logs: list[str] = []
    calls: list[str] = []
    charger = gopay.GoPayCharger(
        requests.Session(),
        {
            "country_code": "86",
            "phone_number": "00000000000",
            "pin": "111111",
            "auto_unbind": {"raw_request": "GET /v1/linkedapps HTTP/2\nHost: customer.gopayapi.com\nx-m1: m1\n"},
        },
        otp_provider=lambda: "",
        log=logs.append,
    )

    class Resp:
        status_code = 201
        text = '{"status_code":"201"}'
        def json(self):
            return {"activation_link_url": f"https://x.test/?reference={LINK_REF}"}

    class Ext:
        def post(self, *args, **kwargs):
            calls.append("linking")
            return Resp()

    linked_payload = {
        "data": {
            "linked_services": [{
                "service_id": "checkout_midtrans",
                "linked_accounts": [{"link_id": "link-1", "unlink_url": "/v1/links/link-1"}],
            }],
        },
    }
    monkeypatch.setattr(gopay._gopay_auto_unbind, "fetch_linkedapps", lambda *a, **k: {
        "has_data": True,
        "status_code": 200,
        "body_json": linked_payload,
    })
    monkeypatch.setattr(gopay._gopay_auto_unbind, "run_from_gopay_config", lambda *a, **k: {
        "ok": True,
        "unlink_status_code": 200,
    })
    charger.ext = Ext()

    assert charger._midtrans_init_linking(SNAP_TOKEN) == LINK_REF
    assert calls == ["linking"]
    assert any("unlinking before linking" in line for line in logs)


def test_gopay_proxy_pool_prefers_gopay_list():
    charger = build_charger()
    charger.proxy_pool = gopay._proxy_list_from_cfg(
        {"proxies": {"gopay_list": ["http://gopay-1.example:8080", "http://gopay-2.example:8080"]}},
        "http://payment.example:8080",
    )

    assert charger.proxy_pool == ["http://gopay-1.example:8080", "http://gopay-2.example:8080"]


def test_gopay_proxy_pool_falls_back_to_payment_list():
    charger = build_charger()
    charger.proxy_pool = gopay._proxy_list_from_cfg(
        {"proxies": {"payment_list": ["http://payment-list.example:8080"]}},
        "http://payment.example:8080",
    )

    assert charger.proxy_pool == ["http://payment-list.example:8080"]


def test_qr_payment_mode_does_not_require_gopay_account():
    cs_session = requests.Session()
    cs_session.headers["Cookie"] = "__Secure-next-auth.session-token=fake"

    charger = gopay.GoPayCharger(
        cs_session,
        {"qr_payment": True, "qr_wait_timeout": 120},
        otp_provider=lambda: "",
        log=lambda _m: None,
    )

    assert charger.qr_payment is True
    assert charger.qr_wait_timeout == 120


def test_qr_payment_uses_qris_charge(monkeypatch):
    logs: list[str] = []
    qris_calls: list[tuple[str, dict]] = []
    charger = gopay.GoPayCharger(
        requests.Session(),
        {
            "qr_payment": True,
            "qr_pre_linking": False,
            "country_code": "62",
            "phone_number": "81234567890",
            "pin": "123456",
        },
        otp_provider=lambda: "000000",
        log=logs.append,
    )

    monkeypatch.setattr(charger, "_midtrans_load_transaction", lambda snap_token: None)
    monkeypatch.setattr(
        charger,
        "_midtrans_create_qr_charge",
        lambda snap_token: {
            "payload": "000201010212QRISDATA",
            "payload_type": "qr_string",
            "charge_mode": "qris",
            "snap_token": snap_token,
            "http_status": 201,
            "response_headers": {"content-type": "application/json"},
            "response_keys": ["order_id", "qr_string", "status_code", "transaction_status"],
            "response": {
                "order_id": "setatt_test",
                "qr_string": "000201010212QRISDATA",
                "status_code": "201",
                "status_message": "Your Transaction is being processed",
                "transaction_status": "pending",
            },
        },
    )
    monkeypatch.setattr(charger, "_save_qr_artifact", lambda qr_info: "output/gopay_qr/test.png")
    monkeypatch.setattr(charger, "_run_qris_app_payment", lambda qris, qr_info: qris_calls.append((qris, qr_info)) or {"payment_id": "A120260509150543TEST"})
    monkeypatch.setattr(charger, "_chatgpt_verify", lambda cs_id, timeout_s=60.0, **_kwargs: {"state": "succeeded", "cs_id": cs_id})

    result = charger._run_midtrans_qr(SNAP_TOKEN, "cs_live_test")

    assert result["state"] == "succeeded"
    assert result["qr_charge_mode"] == "qris"
    assert result["qr_payload_type"] == "qr_string"
    assert result["cs_id"] == "cs_live_test"
    assert result["qr_http_status"] == 201
    assert result["qr_status_code"] == "201"
    assert result["qr_transaction_status"] == "pending"
    assert qris_calls and qris_calls[0][0] == "000201010212QRISDATA"
    assert result["qris_payment"]["payment_id"] == "A120260509150543TEST"
    assert any("starting GoPay APP payment" in line for line in logs)
    assert any("二维码已生成" in line for line in logs)


def test_qr_payment_runs_pre_linking_when_account_is_available(monkeypatch):
    calls: list[str] = []
    charger = gopay.GoPayCharger(
        requests.Session(),
        {
            "qr_payment": True,
            "country_code": "62",
            "phone_number": "81234567890",
            "pin": "123456",
        },
        otp_provider=lambda: "000000",
        log=lambda _m: None,
    )

    monkeypatch.setattr(charger, "_midtrans_load_transaction", lambda snap_token: calls.append("load"))
    monkeypatch.setattr(charger, "_midtrans_init_linking", lambda snap_token: calls.append("linking") or LINK_REF)
    monkeypatch.setattr(charger, "_complete_linking", lambda reference_id: calls.append(f"complete:{reference_id}") or reference_id)
    monkeypatch.setattr(
        charger,
        "_midtrans_create_qr_charge",
        lambda snap_token: calls.append("qr_charge") or {
            "payload": "000201010212QRISDATA",
            "payload_type": "qr_string",
            "charge_mode": "qris",
            "snap_token": snap_token,
            "http_status": 201,
            "response": {},
        },
    )
    monkeypatch.setattr(charger, "_save_qr_artifact", lambda qr_info: "")
    monkeypatch.setattr(charger, "_run_qris_app_payment", lambda qris, qr_info: {"payment_id": "A120260509150543TEST"})
    monkeypatch.setattr(charger, "_chatgpt_verify", lambda cs_id, timeout_s=60.0, **_kwargs: {"state": "succeeded", "cs_id": cs_id})

    result = charger._run_midtrans_qr(SNAP_TOKEN, "cs_live_test")

    assert calls == ["load", "linking", f"complete:{LINK_REF}", "qr_charge"]
    assert result["qr_pre_linking"] == {"ok": True, "reference_id": LINK_REF}


def test_qr_payment_can_skip_pre_linking(monkeypatch):
    calls: list[str] = []
    charger = gopay.GoPayCharger(
        requests.Session(),
        {
            "qr_payment": True,
            "qr_pre_linking": False,
            "country_code": "62",
            "phone_number": "81234567890",
            "pin": "123456",
        },
        otp_provider=lambda: "000000",
        log=lambda _m: None,
    )

    monkeypatch.setattr(charger, "_midtrans_load_transaction", lambda snap_token: calls.append("load"))
    monkeypatch.setattr(charger, "_midtrans_init_linking", lambda snap_token: calls.append("linking") or LINK_REF)
    monkeypatch.setattr(
        charger,
        "_midtrans_create_qr_charge",
        lambda snap_token: calls.append("qr_charge") or {
            "payload": "000201010212QRISDATA",
            "payload_type": "qr_string",
            "charge_mode": "qris",
            "snap_token": snap_token,
            "http_status": 201,
            "response": {},
        },
    )
    monkeypatch.setattr(charger, "_save_qr_artifact", lambda qr_info: "")
    monkeypatch.setattr(charger, "_run_qris_app_payment", lambda qris, qr_info: {"payment_id": "A120260509150543TEST"})
    monkeypatch.setattr(charger, "_chatgpt_verify", lambda cs_id, timeout_s=60.0, **_kwargs: {"state": "succeeded", "cs_id": cs_id})

    result = charger._run_midtrans_qr(SNAP_TOKEN, "cs_live_test")

    assert calls == ["load", "qr_charge"]
    assert result["qr_pre_linking"]["skipped"] is True


def test_extract_qr_payload_from_midtrans_actions():
    charger = build_charger()

    payload, kind = charger._extract_qr_payload({
        "actions": [
            {"name": "generate-qr-code", "url": "https://api.midtrans.com/v2/qris.png"},
        ],
    })

    assert payload == "https://api.midtrans.com/v2/qris.png"
    assert kind == "qr_image_url"


def test_extract_qr_payload_from_qr_string():
    charger = build_charger()

    payload, kind = charger._extract_qr_payload({"qr_string": "000201010212QRISDATAEXAMPLE"})

    assert payload == "000201010212QRISDATAEXAMPLE"
    assert kind == "qr_string"


def test_extract_qr_payload_from_nested_qr_string():
    charger = build_charger()

    payload, kind = charger._extract_qr_payload({
        "data": {
            "transaction": {
                "qris_string": "000201010212QRISDATAEXAMPLE",
            },
        },
    })

    assert payload == "000201010212QRISDATAEXAMPLE"
    assert kind == "qr_string"


def test_decode_data_image_payload_accepts_data_url():
    raw = b"\x89PNG\r\n\x1a\nfake"
    data_url = "data:image/png;base64," + gopay.base64.b64encode(raw).decode("ascii")

    decoded, suffix = gopay._decode_data_image(data_url)

    assert decoded == raw
    assert suffix == ".png"


def test_qris_string_from_info_reads_nested_response():
    charger = build_charger()

    qris = charger._qris_string_from_info(
        {
            "payload": "https://api.midtrans.com/v2/qris.png",
            "payload_type": "payment_url",
            "response": {
                "data": {
                    "qr_content": "000201010212QRISDATAEXAMPLE",
                },
            },
        },
        "",
    )

    assert qris == "000201010212QRISDATAEXAMPLE"


def test_extract_qr_payload_ignores_stripe_return_url():
    charger = build_charger()

    payload, kind = charger._extract_qr_payload({
        "finish_redirect_url": (
            "https://pm-redirects.stripe.com/return/acct_1HOrSwC6h1nxGoI3/"
            "sa_nonce_UTrPoXcSieL6w9f0lQjXMEHccRvUEaF?order_id=setatt_x&status_code=500"
        ),
    })

    assert payload == ""
    assert kind == ""


def test_qris_request_resigns_x_e1_per_body(monkeypatch):
    signed: list[dict] = []
    charger = gopay.GoPayCharger(
        requests.Session(),
        {
            "qr_payment": True,
            "pin": "123456",
            "headers": {
                "authorization": "Bearer token",
                "x-m1": "m1",
                "x-uniqueid": "unique",
                "x-phonemake": "Google",
                "x-phonemodel": "Pixel",
                "x-deviceos": "Android, 12",
                "x-appversion": "2.8.0",
                "x-appid": "com.gojek.gopay",
                "x-apptype": "GOPAY",
                "x-platform": "Android",
                "x-e1": "old",
            },
        },
        otp_provider=lambda: "",
        log=lambda _m: None,
    )

    def fake_signed(headers, **kwargs):
        signed.append({"headers": dict(headers), **kwargs})
        out = dict(headers)
        out["x-e1"] = f"signed:{kwargs['body']}"
        return out

    class Resp:
        status_code = 200
        text = '{"ok":true}'
        def json(self):
            return {"ok": True}

    monkeypatch.setattr(gopay, "_signed_gopay_headers", fake_signed)
    monkeypatch.setattr(charger, "_request_ext", lambda *a, **k: Resp())

    charger._qris_request("POST", "/v1/explore", {"type": "QR_CODE", "data": "000201010212QRISDATA"})

    assert signed
    assert signed[0]["method"] == "POST"
    assert signed[0]["path"] == "/v1/explore"
    assert '"type":"QR_CODE"' in signed[0]["body"]
    assert signed[0]["body_for_signature"] == ""
    assert signed[0]["body_text"] == signed[0]["body"]
    assert signed[0]["headers"]["authorization"] == "Bearer token"

    charger._qris_request("POST", "/v1/qris/payments", {"qr_code": "000201010212QRISDATA"})

    assert signed[1]["path"] == "/v1/qris/payments"
    assert signed[1]["body_for_signature"] == ""
    assert signed[1]["body_text"] == signed[1]["body"]


@responses.activate
def test_gopay_auto_login_posts_login_and_saves_token(monkeypatch, tmp_path):
    signed: list[dict] = []
    logs: list[str] = []
    charger = gopay.GoPayCharger(
        requests.Session(),
        {
            "country_code": "62",
            "phone_number": "87868653447",
            "pin": "123456",
            "auto_login_phone": "89530397723",
            "auto_login_otp_poll_url": "https://sms.example/auto-login",
            "auto_login_token_dir": str(tmp_path),
            "auto_login_pin_client_id": "",
            "auto_login_pin_challenge_id": "",
            "headers": {
                "authorization": "Bearer token",
                "x-m1": "m1",
                "x-uniqueid": "unique",
                "x-phonemake": "OPPO",
                "x-phonemodel": "OPPO, PGEM10",
                "x-deviceos": "Android, 12",
                "x-appversion": "2.8.0",
                "x-appid": "com.gojek.gopay",
                "x-apptype": "GOPAY",
                "x-platform": "Android",
                "x-e1": "old",
            },
        },
        otp_provider=lambda: "",
        log=logs.append,
    )

    def fake_signed(headers, **kwargs):
        signed.append({"headers": dict(headers), **kwargs})
        out = dict(headers)
        out["x-e1"] = f"signed:{kwargs['path']}"
        return out

    monkeypatch.setattr(gopay, "_signed_gopay_headers", fake_signed)
    responses.post(
        "https://accounts.goto-products.com/goto-auth/login/methods",
        status=401,
        json={
            "data": None,
            "success": False,
            "errors": [{"code": "auth:error:user:not_found"}],
        },
    )
    responses.post(
        "https://accounts.goto-products.com/cvs/v1/initiate",
        json={
            "data": {
                "otp_token": "otp-token",
                "otp_length": 4,
                "metadata": {"phone": "+62*******7723"},
            },
            "success": True,
        },
    )
    responses.get(
        "https://sms.example/auto-login",
        body="YES|(GOJEK) OTP: 9302",
    )
    responses.get(
        "https://sms.example/auto-login",
        body="YES|(GOJEK) OTP: 9457",
    )
    responses.post(
        "https://accounts.goto-products.com/cvs/v1/verify",
        json={"data": {"verification_token": "verification-token"}, "success": True},
    )
    responses.post(
        "https://accounts.goto-products.com/goto-auth/accountlist",
        json={
            "data": {
                "account_list": [{"account_id": "758035604"}],
                "1fa_token": "one-fa-token",
            },
            "success": True,
        },
    )
    responses.post(
        "https://accounts.goto-products.com/goto-auth/token",
        status=201,
        json={
            "data": {
                "access_token": "access-token-abcdef",
                "refresh_token": "refresh-token-uvwxyz",
                "flags": {"onetap_eligible": False},
                "user_type": "customer",
            },
            "success": True,
        },
    )
    responses.post(
        "https://accounts.goto-products.com/cvs/v1/methods",
        json={
            "data": {
                "default_method": "otp_wa",
                "methods": ["otp_wa", "otp_sms"],
                "verification_id": "pin-verification-id",
            },
            "success": True,
        },
    )
    responses.post(
        "https://accounts.goto-products.com/cvs/v1/initiate",
        json={
            "data": {
                "otp_token": "pin-otp-token",
                "otp_length": 4,
                "metadata": {"phone": "+62*******7723"},
            },
            "success": True,
        },
    )
    responses.post(
        "https://accounts.goto-products.com/cvs/v1/verify",
        json={"data": {"verification_token": "pin-verification-token"}, "success": True},
    )
    responses.post(
        "https://customer.gopayapi.com/api/v2/users/pins/setup/tokens",
        json={"data": {"token": "pin-setup-token-12345678"}, "success": True},
    )

    result = charger._gopay_auto_login_if_configured()

    assert result["ok"] is True
    assert result["otp_token_received"] is True
    assert [item["path"] for item in signed] == [
        "/goto-auth/login/methods",
        "/cvs/v1/initiate",
        "/cvs/v1/verify",
        "/goto-auth/accountlist",
        "/goto-auth/token",
        "/cvs/v1/methods",
        "/cvs/v1/initiate",
        "/cvs/v1/verify",
        "/api/v2/users/pins/setup/tokens",
    ]
    first_body = json.loads(responses.calls[0].request.body)
    second_body = json.loads(responses.calls[1].request.body)
    verify_body = json.loads(responses.calls[3].request.body)
    accountlist_body = json.loads(responses.calls[4].request.body)
    token_body = json.loads(responses.calls[5].request.body)
    pin_methods_body = json.loads(responses.calls[6].request.body)
    pin_initiate_body = json.loads(responses.calls[7].request.body)
    pin_verify_body = json.loads(responses.calls[9].request.body)
    pin_setup_body = json.loads(responses.calls[10].request.body)
    assert first_body["phone_number"] == "89530397723"
    assert first_body["country_code"] == "+62"
    assert first_body["client_id"] == gopay.GOPAY_LOGIN_CLIENT_ID
    assert "authorization" not in responses.calls[0].request.headers
    assert second_body["flow"] == "login_1fa"
    assert second_body["verification_method"] == "otp_sms"
    assert second_body["phone_number"] == "89530397723"
    assert verify_body["data"] == {"otp": "9302", "otp_token": "otp-token"}
    assert accountlist_body["client_id"] == gopay.GOPAY_LOGIN_CLIENT_ID
    assert responses.calls[4].request.headers["verification-token"] == "Bearer verification-token"
    assert token_body["account_id"] == "758035604"
    assert token_body["token"] == "one-fa-token"
    assert pin_methods_body["flow"] == "goto_pin_wa_sms"
    assert responses.calls[6].request.headers["authorization"] == "Bearer access-token-abcdef"
    assert pin_initiate_body["verification_id"] == "pin-verification-id"
    assert pin_initiate_body["flow"] == "goto_pin_wa_sms"
    assert pin_verify_body["data"] == {"otp": "9457", "otp_token": "pin-otp-token"}
    assert pin_setup_body == {"client_id": "", "pin": "123456", "challenge_id": ""}
    assert responses.calls[10].request.headers["authorization"] == "Bearer access-token-abcdef"
    assert responses.calls[10].request.headers["verification-token"] == "Bearer pin-verification-token"
    token_path = tmp_path / "62_89530397723.json"
    saved = json.loads(token_path.read_text(encoding="utf-8"))
    assert saved["phone_number"] == "89530397723"
    assert saved["account_id"] == "758035604"
    assert saved["token"]["access_token"] == "access-token-abcdef"
    assert saved["token"]["refresh_token"] == "refresh-token-uvwxyz"
    assert saved["pin_setup"]["ok"] is True
    assert saved["pin_setup"]["setup_token_tail"] == "12345678"
    assert result["token_result"]["token_path"] == str(token_path)
    assert result["token_result"]["pin_setup"]["ok"] is True
    assert any("login/methods status=401" in line for line in logs)
    assert any("cvs/initiate status=200 otp_token=yes" in line for line in logs)
    assert any("pin setup status=200" in line for line in logs)
    assert any("token saved" in line for line in logs)


def test_qris_request_reuses_auto_unbind_raw_headers(monkeypatch):
    signed: list[dict] = []
    raw_request = "\n".join([
        "GET /v1/linkedapps HTTP/2",
        "Host: customer.gopayapi.com",
        "authorization: Bearer auto-unbind-token",
        "x-m1: m1",
        "x-uniqueid: unique-from-unbind",
        "x-phonemake: Google",
        "x-phonemodel: Pixel",
        "x-deviceos: Android, 12",
        "x-appversion: 2.8.0",
        "x-appid: com.gojek.gopay",
        "x-apptype: GOPAY",
        "x-platform: Android",
        "x-e1: stale",
        "",
    ])
    charger = gopay.GoPayCharger(
        requests.Session(),
        {
            "qr_payment": True,
            "pin": "123456",
            "auto_unbind": {"raw_request": raw_request},
        },
        otp_provider=lambda: "",
        log=lambda _m: None,
    )

    def fake_signed(headers, **kwargs):
        signed.append({"headers": dict(headers), **kwargs})
        out = dict(headers)
        out["x-e1"] = "fresh"
        return out

    class Resp:
        status_code = 200
        text = '{"ok":true}'
        def json(self):
            return {"ok": True}

    monkeypatch.setattr(gopay, "_signed_gopay_headers", fake_signed)
    monkeypatch.setattr(charger, "_request_ext", lambda *a, **k: Resp())

    charger._qris_request("POST", "/v1/explore", {"type": "QR_CODE", "data": "000201010212QRISDATA"})

    assert signed
    assert signed[0]["headers"]["authorization"] == "Bearer auto-unbind-token"
    assert signed[0]["headers"]["x-uniqueid"] == "unique-from-unbind"
    assert signed[0]["body"].startswith('{"type":"QR_CODE"')


def test_qris_request_returns_pin_challenge_on_461(monkeypatch):
    charger = gopay.GoPayCharger(
        requests.Session(),
        {
            "qr_payment": True,
            "pin": "123456",
            "qris_headers": {
                "authorization": "Bearer token",
                "x-m1": "m1",
                "x-uniqueid": "unique",
                "x-phonemake": "Google",
                "x-phonemodel": "Pixel",
                "x-deviceos": "Android, 12",
                "x-appversion": "2.8.0",
                "x-appid": "com.gojek.gopay",
                "x-apptype": "GOPAY",
                "x-platform": "Android",
            },
        },
        otp_provider=lambda: "123456",
        log=lambda _m: None,
    )

    def fake_signed(headers, **kwargs):
        out = dict(headers)
        out["x-e1"] = "fresh"
        return out

    class Resp:
        status_code = 461
        text = '{"success":false}'

        def json(self):
            return {
                "data": {
                    "challenge": {
                        "action": {
                            "type": "GOPAY_PIN_CHALLENGE",
                            "value": {
                                "challenge_id": "challenge-id",
                                "client_id": "client-id",
                            },
                        }
                    }
                },
                "success": False,
                "errors": [{"code": "GoPay-125"}],
            }

    monkeypatch.setattr(gopay, "_signed_gopay_headers", fake_signed)
    monkeypatch.setattr(charger, "_request_ext", lambda *a, **k: Resp())

    resp = charger._qris_request(
        "PATCH",
        "/v3/payments/pay-id/capture",
        {"challenge": {}},
        allow_pin_challenge=True,
    )

    challenge = resp["data"]["challenge"]["action"]["value"]
    assert challenge["challenge_id"] == "challenge-id"
    assert challenge["client_id"] == "client-id"


def test_qris_capture_pin_loop_reuses_461_challenge_until_success(monkeypatch):
    charger = build_charger()
    calls: list[tuple[str, str, dict | None]] = []

    def challenge(cid: str) -> dict:
        return {
            "data": {
                "challenge": {
                    "action": {
                        "type": "GOPAY_PIN_CHALLENGE",
                        "value": {"challenge_id": cid, "client_id": "client-id"},
                    }
                }
            },
            "success": False,
            "errors": [{"code": "GoPay-125", "message": "retry"}],
        }

    responses = [challenge("challenge-1"), challenge("challenge-2"), {"success": True, "data": {"status": "CAPTURED"}}]

    def fake_qris_request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path.endswith("/capture"):
            return responses.pop(0)
        if path == "/api/v1/users/pin/tokens":
            return {"data": {"token": f"token-{body['challenge_id']}"}}
        return {}

    monkeypatch.setattr(charger, "_qris_request", fake_qris_request)

    resp = charger._qris_capture_with_pin_loop("payment-id", {"challenge": None})

    assert resp["success"] is True
    capture_bodies = [body for _method, path, body in calls if path.endswith("/capture")]
    assert capture_bodies[0]["challenge"] is None
    assert capture_bodies[1]["challenge"]["action"] is None
    assert capture_bodies[1]["challenge"]["type"] == "GOPAY_PIN_CHALLENGE"
    assert capture_bodies[1]["challenge"]["value"]["pin_token"] == "token-challenge-1"
    assert capture_bodies[1]["challenge"]["metadata"] == {
        "challenge_id": "challenge-1",
        "client_id": "client-id",
    }
    assert capture_bodies[2]["challenge"]["value"]["pin_token"] == "token-challenge-2"


def test_qris_capture_marks_wallet_last_used_before_pin_token(monkeypatch):
    charger = build_charger()
    calls: list[tuple[str, str, dict | None]] = []
    responses = [
        {
            "data": {
                "challenge": {
                    "action": {
                        "type": "GOPAY_PIN_CHALLENGE",
                        "value": {"challenge_id": "challenge-1", "client_id": "client-id"},
                    }
                }
            },
            "success": False,
            "errors": [{"code": "GoPay-125", "message": "retry"}],
        },
        {"success": True, "data": {"status": "PAID"}},
    ]

    def fake_qris_request(method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if path.endswith("/capture"):
            return responses.pop(0)
        if path == "/v1/customer/payment-options/settings/last-used":
            return {"success": True}
        if path == "/api/v1/users/pin/tokens":
            return {"data": {"token": "pin-token"}}
        return {}

    monkeypatch.setattr(charger, "_qris_request", fake_qris_request)

    resp = charger._qris_capture_with_pin_loop(
        "payment-id",
        {"challenge": None, "payment_instructions": [{"token": "wallet-token"}]},
    )

    assert resp["success"] is True
    paths = [path for _method, path, _body in calls]
    assert paths.index("/v1/customer/payment-options/settings/last-used") < paths.index("/api/v1/users/pin/tokens")
    last_used_call = next(body for _method, path, body in calls if path == "/v1/customer/payment-options/settings/last-used")
    assert last_used_call == {"token": "wallet-token"}


def test_wait_qris_midtrans_terminal_accepts_settlement(monkeypatch):
    charger = build_charger()

    class Resp:
        status_code = 200
        text = '{"transaction_status":"settlement"}'

        def json(self):
            return {"transaction_status": "settlement"}

    monkeypatch.setattr(charger, "_request_ext", lambda *a, **k: Resp())

    result = charger._wait_qris_midtrans_terminal({
        "response": {"transaction_id": "tx-id", "order_id": "order-id"}
    })

    assert result["ok"] is True
    assert result["status"] == "settlement"


def test_follow_qris_finish_redirect_marks_redirect_status_failed(monkeypatch):
    charger = build_charger()

    class Resp:
        status_code = 200
        url = "https://checkout.stripe.com/c/pay/cs_live_test?redirect_status=failed"

    monkeypatch.setattr(charger, "_request_ext", lambda *a, **k: Resp())

    result = charger._follow_qris_finish_redirect({
        "response": {"finish_200_redirect_url": "https://pm-redirects.stripe.com/return/test"}
    })

    assert result["ok"] is False
    assert result["redirect_failed"] is True


def test_follow_qris_finish_redirect_visits_embedded_return_url(monkeypatch):
    charger = build_charger()
    calls: list[str] = []
    embedded = "https://pay.openai.com/c/pay/cs_live_test?returned_from_redirect=true"

    class Resp:
        status_code = 200

        def __init__(self, url: str):
            self.url = url

    def fake_request(_method, url, **_kwargs):
        calls.append(url)
        return Resp(
            "https://checkout.stripe.com/c/pay/cs_live_test"
            "?redirect_status=failed&setup_intent=seti_test&return_url="
            + embedded.replace(":", "%3A").replace("/", "%2F").replace("?", "%3F").replace("=", "%3D")
            + "#setup_intent_client_secret=seti_secret_test"
        )

    def fake_cs_get(url, **_kwargs):
        calls.append(url)
        return Resp(url)

    monkeypatch.setattr(charger, "_request_ext", fake_request)
    monkeypatch.setattr(charger.cs, "get", fake_cs_get)

    result = charger._follow_qris_finish_redirect({
        "response": {"finish_200_redirect_url": "https://pm-redirects.stripe.com/return/test"}
    })

    assert calls[0] == "https://pm-redirects.stripe.com/return/test"
    assert calls[1].startswith(embedded + "&")
    assert "setup_intent=seti_test" in calls[1]
    assert "setup_intent_client_secret=seti_secret_test" in calls[1]
    assert result["ok"] is True
    assert result["redirect_failed"] is True
    assert result["return_url"]["attempted"] is True


def test_qris_finish_redirect_prefers_success_callback_from_deeplink(monkeypatch):
    charger = build_charger()
    calls: list[str] = []
    callback = "https://pm-redirects.stripe.com/return/acct/nonce?order_id=setatt_test"

    class Resp:
        status_code = 200

        def __init__(self, url: str):
            self.url = url

    def fake_request(_method, url, **_kwargs):
        calls.append(url)
        return Resp(url)

    monkeypatch.setattr(charger, "_request_ext", fake_request)
    monkeypatch.setattr(charger, "_visit_qris_embedded_return_url", lambda _url: {"attempted": False, "reason": "missing"})

    result = charger._follow_qris_finish_redirect({
        "response": {
            "order_id": "setatt_test",
            "deeplink_url": "https://gopay.co.id/app/merchanttransfer?callback_url=" + callback.replace(":", "%3A").replace("/", "%2F").replace("?", "%3F").replace("=", "%3D"),
            "finish_200_redirect_url": "https://pm-redirects.stripe.com/return/acct/nonce?order_id=setatt_test&status_code=200&transaction_status=capture",
        }
    })

    assert calls
    assert calls[0] == callback + "&status_code=200&transaction_status=capture"
    assert result["ok"] is True


def test_qris_try_gwa_settlement_uses_non_qr_payment_process(monkeypatch):
    charger = build_charger()
    charger.gopay_cfg["qris_gwa_settlement"] = True
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(charger, "_gopay_payment_validate", lambda ref: calls.append(("validate", ref)))
    monkeypatch.setattr(charger, "_gopay_payment_confirm", lambda ref: calls.append(("confirm", ref)) or ("challenge-id", "client-id"))
    monkeypatch.setattr(charger, "_tokenize_pin", lambda cid, client: calls.append(("pin", f"{cid}:{client}")) or "pin-token")
    monkeypatch.setattr(charger, "_gopay_payment_process", lambda ref, token: calls.append(("process", f"{ref}:{token}")))

    result = charger._qris_try_gwa_settlement(
        "A220260510080647TESTID",
        {"response": {"deeplink_url": "https://gopay.co.id/app/merchanttransfer?tref=A220260510080647TESTID"}},
    )

    assert result == {"ok": True, "reference_id": "A220260510080647TESTID"}
    assert calls == [
        ("validate", "A220260510080647TESTID"),
        ("confirm", "A220260510080647TESTID"),
        ("pin", "challenge-id:client-id"),
        ("process", "A220260510080647TESTID:pin-token"),
    ]


def test_wait_qris_midtrans_terminal_uses_capture_success_without_status_poll(monkeypatch):
    charger = build_charger()
    calls: list[str] = []
    monkeypatch.setattr(charger, "_request_ext", lambda *a, **k: calls.append(a[1]) or pytest.fail("status poll should be skipped"))

    result = charger._wait_qris_midtrans_terminal(
        {"response": {"transaction_id": "tx-id", "order_id": "order-id"}},
        {"success": True, "data": {"status": "PAID"}},
    )

    assert result["ok"] is True
    assert result["status"] == "paid"
    assert result["source"] == "gopay_capture"
    assert calls == []


def test_wait_qris_midtrans_terminal_stops_on_unauthorized(monkeypatch):
    charger = build_charger()
    calls: list[str] = []

    class Resp:
        status_code = 401
        text = "{}"

        def json(self):
            return {}

    def fake_request(_method, url, **_kwargs):
        calls.append(url)
        return Resp()

    monkeypatch.setattr(charger, "_request_ext", fake_request)

    result = charger._wait_qris_midtrans_terminal({
        "response": {"transaction_id": "tx-id", "order_id": "order-id"}
    })

    assert result["ok"] is False
    assert result["status"] == "unauthorized"
    assert len(calls) == 1


def test_chatgpt_verify_accepts_accounts_check_active_after_html(monkeypatch):
    charger = build_charger()
    sleeps: list[float] = []
    return_calls: list[dict] = []
    monkeypatch.setattr(gopay.time, "sleep", lambda s: sleeps.append(float(s)))
    monkeypatch.setattr(charger, "_follow_qris_finish_redirect", lambda info: return_calls.append(info) or {"ok": True})
    monkeypatch.setattr(charger, "_chatgpt_accounts_check", lambda: {"status_code": 200, "active_hint": "active_subscription"})

    class Resp:
        status_code = 200
        text = "<html></html>"
        url = "https://chatgpt.com/checkout/verify"

        def json(self):
            raise ValueError("html")

    monkeypatch.setattr(charger.cs, "get", lambda *a, **k: Resp())

    result = charger._chatgpt_verify("cs_live_test", timeout_s=5, qris_info={"response": {"finish_200_redirect_url": "https://example.test/return"}})

    assert result["state"] == "succeeded"
    assert result["accounts_check"]["active_hint"] == "active_subscription"
    assert return_calls
    assert sleeps == []


def test_chatgpt_accounts_check_does_not_treat_eligible_offers_as_active(monkeypatch):
    charger = build_charger()

    class Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "accounts": {
                    "acct": {
                        "account": {"plan_type": "free"},
                        "entitlement": {
                            "has_active_subscription": False,
                            "subscription_plan": "chatgptfreeplan",
                        },
                        "eligible_offers": {
                            "offers": [{"id": "chatgptplusplan"}],
                            "default_offer_id": "chatgptplusplan",
                        },
                    }
                },
                "account_ordering": ["acct"],
            }

    monkeypatch.setattr(charger.cs, "get", lambda *a, **k: Resp())

    result = charger._chatgpt_accounts_check()

    assert result["active_hint"] == ""
    assert result["active_plan"] == ""


def test_chatgpt_accounts_check_detects_active_subscription(monkeypatch):
    charger = build_charger()

    class Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "accounts": {
                    "acct": {
                        "account": {"plan_type": "plus"},
                        "entitlement": {
                            "has_active_subscription": True,
                            "subscription_plan": "chatgptplusplan",
                        },
                    }
                }
            }

    monkeypatch.setattr(charger.cs, "get", lambda *a, **k: Resp())

    result = charger._chatgpt_accounts_check()

    assert result["active_hint"] == "active_subscription"
    assert result["active_plan"] == "chatgptplusplan"


def test_qris_headers_prefer_auto_unbind_over_stale_headers(monkeypatch):
    signed: list[dict] = []
    raw_request = "\n".join([
        "GET /v1/linkedapps HTTP/2",
        "Host: customer.gopayapi.com",
        "authorization: Bearer oppo-token",
        "x-m1: oppo-m1",
        "x-uniqueid: oppo-unique",
        "x-phonemake: OPPO",
        "x-phonemodel: OPPO, PGEM10",
        "x-deviceos: Android, 12",
        "x-appversion: 2.8.0",
        "x-appid: com.gojek.gopay",
        "x-apptype: GOPAY",
        "x-platform: Android",
        "",
    ])
    charger = gopay.GoPayCharger(
        requests.Session(),
        {
            "qr_payment": True,
            "pin": "123456",
            "auto_unbind": {"raw_request": raw_request},
            "headers": {
                "authorization": "Bearer huawei-token",
                "x-m1": "huawei-m1",
                "x-uniqueid": "huawei-unique",
                "x-phonemake": "HUAWEI",
                "x-phonemodel": "HUAWEI, ALN-AL00",
                "x-deviceos": "Android, 12",
                "x-appversion": "2.8.0",
                "x-appid": "com.gojek.gopay",
                "x-apptype": "GOPAY",
                "x-platform": "Android",
            },
        },
        otp_provider=lambda: "",
        log=lambda _m: None,
    )

    def fake_signed(headers, **kwargs):
        signed.append({"headers": dict(headers), **kwargs})
        out = dict(headers)
        out["x-e1"] = "fresh"
        return out

    class Resp:
        status_code = 200
        text = '{"ok":true}'
        def json(self):
            return {"ok": True}

    monkeypatch.setattr(gopay, "_signed_gopay_headers", fake_signed)
    monkeypatch.setattr(charger, "_request_ext", lambda *a, **k: Resp())

    charger._qris_request("POST", "/v1/explore", {"type": "QR_CODE", "data": "000201010212QRISDATA"})

    assert signed
    assert signed[0]["headers"]["authorization"] == "Bearer oppo-token"
    assert signed[0]["headers"]["x-m1"] == "oppo-m1"
    assert signed[0]["headers"]["x-phonemake"] == "OPPO"
    assert signed[0]["headers"]["x-phonemodel"] == "OPPO, PGEM10"


def test_midtrans_linking_429_retries_past_406_limit(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(gopay.time, "sleep", lambda s: sleeps.append(float(s)))
    monkeypatch.setattr(gopay.random, "uniform", lambda a, b: 2.5)

    class Resp:
        def __init__(self, status_code: int, text: str = "", body: dict | None = None):
            self.status_code = status_code
            self.text = text
            self._body = body or {}

        def json(self):
            return self._body

    class Ext:
        def __init__(self):
            self.calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= 5:
                return Resp(429)
            return Resp(201, body={"activation_link_url": f"https://x.test/?reference={LINK_REF}"})

    charger = build_charger()
    ext = Ext()
    charger.ext = ext

    assert charger._midtrans_init_linking(SNAP_TOKEN) == LINK_REF
    assert ext.calls == 6
    assert sleeps == [3.5] * 5


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


def test_extract_otp_from_payload_filters_phone():
    payload = {
        "history": [
            {
                "otp": "111111",
                "phone": "81234567890",
                "received_at": time.time(),
                "text": "GoPay OTP 111111",
            },
            {
                "otp": "222222",
                "phone": "81234567891",
                "received_at": time.time(),
                "text": "GoPay OTP 222222",
            },
        ],
    }
    assert gopay._extract_otp_from_payload(payload, phone="81234567890", country_code="62") == "111111"
    assert gopay._extract_otp_from_payload(payload, phone="81234567891", country_code="62") == "222222"


def test_pick_gopay_account_config_uses_random_choice():
    cfg = {
        "otp": {"source": "manual"},
        "accounts": [
            {"label": "a", "country_code": "62", "phone_number": "100", "pin": "111111"},
            {"label": "b", "country_code": "62", "phone_number": "200", "pin": "222222"},
        ],
    }

    class DummyRandom:
        def choice(self, values):
            return values[1]

    selected = gopay.pick_gopay_account_config(cfg, rng=DummyRandom(), log=lambda _m: None)
    assert selected["phone_number"] == "200"
    assert selected["pin"] == "222222"
    assert selected["_selected_accounts_count"] == 2


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


@responses.activate
def test_whatsapp_http_otp_provider_sends_phone_filter_and_skips_wrong_phone():
    url = "http://127.0.0.1:8765/latest"
    responses.get(
        url,
        json={"otp": "111111", "phone": "81234567891", "text": "GoPay OTP 111111"},
    )
    responses.get(
        url,
        json={"otp": "222222", "phone": "81234567890", "text": "GoPay OTP 222222"},
    )
    provider = gopay.whatsapp_http_otp_provider(
        url,
        timeout=5.0,
        interval=0.1,
        phone="81234567890",
        country_code="62",
        log=lambda _m: None,
    )
    assert provider() == "222222"
    query = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert query["phone"] == ["81234567890"]
    assert query["country_code"] == ["62"]
    assert "since" in query


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
