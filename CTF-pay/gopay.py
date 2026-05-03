#!/usr/bin/env python3
"""GoPay tokenization payment flow for ChatGPT Plus subscriptions.

Replays Stripe → Midtrans → GoPay's tokenization linking + charge in pure
HTTP. No browser needed. WhatsApp OTP delivered via injected callback
(stdin for CLI, file-watch for webui runner, or configured WhatsApp relay).

Flow (15 steps):

    1.  POST chatgpt.com/backend-api/payments/checkout
            body: {entry_point, plan_name, billing_details:{country:ID,currency:IDR}, ...}
            ← cs_live_xxx
    2.  POST api.stripe.com/v1/payment_methods (type=gopay)         ← pm_xxx
    3.  POST api.stripe.com/v1/payment_pages/{cs}/confirm           ← status:open
    4.  POST chatgpt.com/backend-api/payments/checkout/approve      ← approved
    5.  GET  pm-redirects.stripe.com/authorize/{nonce}              → 302 → midtrans
    6.  GET  app.midtrans.com/snap/v1/transactions/{snap_token}     ← merchant info
    7.  POST app.midtrans.com/snap/v3/accounts/{snap_token}/linking
            body: {type:gopay, country_code, phone_number}
            (406 first attempt if account already linked, retry → 201)  ← reference_id
    8.  POST gwa.gopayapi.com/v1/linking/validate-reference         ← display info
    9.  POST gwa.gopayapi.com/v1/linking/user-consent               ← OTP triggered
    10. POST gwa.gopayapi.com/v1/linking/validate-otp               ← challenge_id, client_id
    11. POST customer.gopayapi.com/api/v1/users/pin/tokens/nb       ← pin_token (JWT)
    12. POST gwa.gopayapi.com/v1/linking/validate-pin               ← linking complete
    13. POST app.midtrans.com/snap/v2/transactions/{snap}/charge    ← charge_ref (A12...)
    14. GET  gwa.gopayapi.com/v1/payment/validate?reference_id=...
        POST gwa.gopayapi.com/v1/payment/confirm?reference_id=...   ← second challenge
        POST customer.gopayapi.com/api/v1/users/pin/tokens/nb       ← second pin_token
        POST gwa.gopayapi.com/v1/payment/process?reference_id=...   ← settled
    15. GET  chatgpt.com/checkout/verify?stripe_session_id=...      ← Plus active
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

import requests

# Cloudflare 拦 plain requests 的 TLS 指纹（403 + HTML challenge），跟 card.py 一致用 curl_cffi
# 模拟真 Chrome 指纹。
try:
    from curl_cffi.requests import Session as _CurlCffiSession  # type: ignore
except ImportError:
    _CurlCffiSession = None  # type: ignore


def _new_session(impersonate: str = "chrome136") -> Any:
    """Build session with chrome TLS fingerprint when available."""
    if _CurlCffiSession is not None:
        return _CurlCffiSession(impersonate=impersonate)
    return requests.Session()


# ──────────────────────────── constants ───────────────────────────

# OpenAI's Midtrans merchant client id (public, embedded in JS).
# Override via gopay config block if rotated.
DEFAULT_MIDTRANS_CLIENT_ID = "Mid-client-3TX8nUa-f_RgNrky"

# OpenAI's Stripe live publishable key (public, embedded in checkout page JS).
# Override via cfg["stripe"]["publishable_key"] if it ever changes.
DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)

GOPAY_PIN_CLIENT_ID_LINK = "51b5f09a-3813-11ee-be56-0242ac120002-MGUPA"
GOPAY_PIN_CLIENT_ID_CHARGE = "47180a8e-f56e-11ed-a05b-0242ac120003-GWC"

DEFAULT_TIMEOUT = 30
LINK_RETRY_LIMIT = 2  # 406 "account already linked" retry
LINK_RETRY_SLEEP_S = 12.0  # Midtrans 需要冷却 ~10s 才会让 406 → 201（实测）
DEFAULT_OTP_REGEX = r"(?<!\d)(\d{6})(?!\d)"


# ──────────────────────────── exceptions ──────────────────────────


class GoPayError(RuntimeError):
    pass


class OTPCancelled(GoPayError):
    pass


class GoPayPINRejected(GoPayError):
    pass


# ──────────────────────────── core ────────────────────────────────


class GoPayCharger:
    """Drive the entire GoPay tokenization flow for one subscription.

    Construction needs:
        chatgpt_session: a requests.Session pre-configured with the user's
            chatgpt.com cookies + sentinel headers. Caller is responsible.
        gopay_cfg: {"country_code": "86", "phone_number": "...", "pin": "..."}
        otp_provider: () -> str. Called once per linking; should block until
            the user supplies the OTP via WhatsApp.
        log: () -> None. Called for human-readable progress messages.
    """

    def __init__(
        self,
        chatgpt_session: Any,
        gopay_cfg: dict,
        otp_provider: Callable[[], str],
        log: Callable[[str], None] = print,
        proxy: Optional[str] = None,
        runtime_cfg: Optional[dict] = None,
    ):
        self.cs = chatgpt_session
        self.country_code = str(gopay_cfg["country_code"]).lstrip("+")
        self.phone = re.sub(r"\D", "", str(gopay_cfg["phone_number"]))
        self.pin = str(gopay_cfg["pin"])
        self.midtrans_client_id = str(
            gopay_cfg.get("midtrans_client_id") or DEFAULT_MIDTRANS_CLIENT_ID
        )
        self.otp_provider = otp_provider
        self.log = log
        # Stripe runtime fingerprint (js_checksum / rv_timestamp / version) — these
        # are computed by Stripe.js client-side; replay the captured values from
        # config.runtime or HAR. Without them confirm 400.
        self.runtime = runtime_cfg or {}
        # separate session for non-chatgpt domains (avoid leaking chatgpt cookies)
        self.ext = _new_session()
        self.ext.headers.update({
            "User-Agent": (
                self.cs.headers.get("User-Agent")
                or "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_2_1) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        })
        if proxy:
            try:
                self.cs.proxies = {"http": proxy, "https": proxy}
            except Exception:
                pass
            try:
                self.ext.proxies = {"http": proxy, "https": proxy}
            except Exception:
                pass

    # ───── Step 1-4: ChatGPT/Stripe checkout ─────

    def _chatgpt_create_checkout(self) -> str:
        body = {
            "entry_point": "all_plans_pricing_modal",
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": "ID", "currency": "IDR"},
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": False,
            },
            "checkout_ui_mode": "custom",
        }
        r = self.cs.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            json=body, timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        cs_id = (
            data.get("checkout_session_id")
            or data.get("session_id")
            or data.get("id")
        )
        if not cs_id or not str(cs_id).startswith("cs_"):
            raise GoPayError(f"checkout create: bad response {data!r}")
        self.log(f"[gopay] checkout created cs={cs_id}")
        return cs_id

    def _stripe_create_pm(self, cs_id: str, stripe_pk: str, billing: dict) -> str:
        # PM billing 即使 IDR 计划也接受 US 地址（HAR 验证）；空配置时给个有效默认
        body = {
            "billing_details[name]": billing.get("name") or "John Doe",
            "billing_details[email]": billing.get("email") or "buyer@example.com",
            "billing_details[address][country]": billing.get("country") or "US",
            "billing_details[address][line1]": billing.get("line1") or "3110 Sunset Boulevard",
            "billing_details[address][city]": billing.get("city") or "Los Angeles",
            "billing_details[address][postal_code]": billing.get("postal_code") or "90026",
            "billing_details[address][state]": billing.get("state") or "CA",
            "type": "gopay",
            "client_attribution_metadata[checkout_session_id]": cs_id,
            "key": stripe_pk,
        }
        r = self.ext.post(
            "https://api.stripe.com/v1/payment_methods",
            data=body, timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        pm_id = r.json().get("id", "")
        if not pm_id.startswith("pm_"):
            raise GoPayError(f"stripe payment_methods: bad response {r.text[:300]}")
        self.log(f"[gopay] stripe pm={pm_id}")
        return pm_id

    def _stripe_init(self, cs_id: str, stripe_pk: str) -> str:
        """Call /payment_pages/{cs}/init to get init_checksum."""
        body = {
            "browser_locale": "en-US",
            "browser_timezone": "Asia/Shanghai",
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": str(uuid.uuid4()),
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[stripe_js_locale]": "auto",
            "key": stripe_pk,
        }
        r = self.ext.post(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}/init",
            data=body, timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        ic = (r.json() or {}).get("init_checksum") or ""
        if not ic:
            raise GoPayError(f"stripe init: no init_checksum {r.text[:200]}")
        return ic

    def _stripe_confirm(self, cs_id: str, pm_id: str, stripe_pk: str):
        init_checksum = self._stripe_init(cs_id, stripe_pk)
        # Stripe 需要 return_url 才会把 checkout 推进到 requires_action（带 setup_intent）
        chatgpt_return = (
            f"https://chatgpt.com/checkout/verify?stripe_session_id={cs_id}"
            f"&processor_entity=openai_llc&plan_type=plus"
        )
        from urllib.parse import quote
        return_url = (
            f"https://checkout.stripe.com/c/pay/{cs_id}"
            f"?returned_from_redirect=true&ui_mode=custom&return_url={quote(chatgpt_return, safe='')}"
        )
        body = {
            "guid": uuid.uuid4().hex,
            "muid": uuid.uuid4().hex,
            "sid": uuid.uuid4().hex,
            "payment_method": pm_id,
            "init_checksum": init_checksum,
            "version": self.runtime.get("version") or "fed52f3bc6",
            "expected_amount": "0",
            "expected_payment_method_type": "gopay",
            "return_url": return_url,
            "elements_session_client[session_id]": f"elements_session_{uuid.uuid4().hex[:11]}",
            "elements_session_client[locale]": "en",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[is_aggregation_expected]": "false",
            "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
            "client_attribution_metadata[merchant_integration_source]": "elements",
            "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
            "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
            "key": stripe_pk,
        }
        # Stripe runtime anti-bot tokens (replayable per-session-only; without
        # these confirm fails for hCaptcha-protected merchants like OpenAI).
        if self.runtime.get("js_checksum"):
            body["js_checksum"] = self.runtime["js_checksum"]
        if self.runtime.get("rv_timestamp"):
            body["rv_timestamp"] = self.runtime["rv_timestamp"]
        r = self.ext.post(
            f"https://api.stripe.com/v1/payment_pages/{cs_id}/confirm",
            data=body, timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            raise GoPayError(f"stripe confirm {r.status_code}: {r.text[:400]}")
        self.log(f"[gopay] stripe confirm: {r.json().get('payment_status')}")

    def _chatgpt_sentinel_ping(self):
        try:
            self.cs.post(
                "https://chatgpt.com/backend-api/sentinel/ping",
                json={}, timeout=DEFAULT_TIMEOUT,
            )
        except Exception as e:
            self.log(f"[gopay] sentinel/ping skipped: {e}")

    def _chatgpt_approve(self, cs_id: str, processor_entity: str = "openai_llc"):
        # sentinel/ping 在 approve 之前刷一下，否则 approve 过但 setup_intent 不创
        self._chatgpt_sentinel_ping()
        r = self.cs.post(
            "https://chatgpt.com/backend-api/payments/checkout/approve",
            json={"checkout_session_id": cs_id, "processor_entity": processor_entity},
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        result = r.json().get("result")
        if result != "approved":
            raise GoPayError(f"chatgpt approve: result={result!r}")
        self.log("[gopay] chatgpt approved")

    # ───── Step 5-6: Stripe → Midtrans redirect ─────

    def _follow_redirect_to_midtrans(self, cs_id: str, stripe_pk: str) -> str:
        """Resolve the Midtrans snap_token from setup_intent.next_action.

        After approve, Stripe populates setup_intent on the checkout session.
        The frontend re-GETs payment_pages/{cs} to read
        setup_intent.next_action.redirect_to_url.url which is
        https://pm-redirects.stripe.com/authorize/{acct}/{nonce}. GETting
        that URL with redirects disabled returns 302 → app.midtrans.com/...
        whose path contains the snap_token.
        """
        deadline = time.time() + 60
        last_err = ""
        sess_id = f"elements_session_{uuid.uuid4().hex[:11]}"
        js_id = str(uuid.uuid4())
        params = {
            "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
            "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[session_id]": sess_id,
            "elements_session_client[stripe_js_id]": js_id,
            "elements_session_client[locale]": "en",
            "elements_session_client[is_aggregation_expected]": "false",
            "elements_options_client[stripe_js_locale]": "auto",
            "elements_options_client[saved_payment_method][enable_save]": "never",
            "elements_options_client[saved_payment_method][enable_redisplay]": "never",
            "key": stripe_pk,
            "_stripe_version": (
                "2025-03-31.basil; checkout_server_update_beta=v1; "
                "checkout_manual_approval_preview=v1"
            ),
        }
        while time.time() < deadline:
            r = self.ext.get(
                f"https://api.stripe.com/v1/payment_pages/{cs_id}",
                params=params,
                timeout=DEFAULT_TIMEOUT,
            )
            if r.status_code == 200:
                payload = r.json() or {}
                si = payload.get("setup_intent") or {}
                if si.get("status") == "requires_action":
                    rtu = (si.get("next_action") or {}).get("redirect_to_url") or {}
                    pm_url = rtu.get("url") or ""
                    if pm_url:
                        snap_token = self._fetch_pm_redirect_snap_token(pm_url)
                        self.log(f"[gopay] midtrans snap_token={snap_token}")
                        return snap_token
                last_err = (
                    f"setup_intent status={si.get('status')!r} "
                    f"payment_status={payload.get('payment_status')!r} "
                    f"status={payload.get('status')!r} "
                    f"keys=[{','.join(sorted(payload.keys())[:8])}]"
                )
            else:
                last_err = f"http {r.status_code}: {r.text[:120]}"
            time.sleep(1)
        raise GoPayError(f"snap_token resolution timeout: {last_err}")

    def _fetch_pm_redirect_snap_token(self, pm_url: str) -> str:
        """GET pm-redirects.stripe.com/authorize/... → 302 to midtrans.
        Extract snap_token from the Location header.
        """
        r = self.ext.get(pm_url, allow_redirects=False, timeout=DEFAULT_TIMEOUT)
        if r.status_code not in (301, 302, 303, 307, 308):
            raise GoPayError(f"pm-redirects: expected redirect, got {r.status_code}")
        loc = r.headers.get("Location", "")
        m = re.search(r"app\.midtrans\.com/snap/v[14]/redirection/([a-f0-9-]{36})", loc)
        if not m:
            raise GoPayError(f"pm-redirects: no midtrans token in Location={loc!r}")
        return m.group(1)

    def _midtrans_load_transaction(self, snap_token: str):
        """Optional: load transaction page so any session cookies get set."""
        r = self.ext.get(
            f"https://app.midtrans.com/snap/v1/transactions/{snap_token}",
            headers={
                "x-source": "snap",
                "x-source-app-type": "redirection",
                "x-source-version": "2.3.0",
            },
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        enabled = [p.get("type") for p in body.get("enabled_payments", [])]
        self.log(f"[gopay] midtrans enabled_payments={enabled}")

    def _midtrans_basic_auth(self) -> dict:
        import base64
        token = base64.b64encode(
            f"{self.midtrans_client_id}:".encode("ascii"),
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}

    # ───── Step 7: Midtrans linking initiation ─────

    def _midtrans_init_linking(self, snap_token: str) -> str:
        """POST snap/v3/accounts/{snap}/linking. Retries on 406."""
        url = f"https://app.midtrans.com/snap/v3/accounts/{snap_token}/linking"
        body = {
            "type": "gopay",
            "country_code": self.country_code,
            "phone_number": self.phone,
        }
        headers = {
            **self._midtrans_basic_auth(),
            "Content-Type": "application/json",
            "Origin": "https://app.midtrans.com",
            "Referer": f"https://app.midtrans.com/snap/v4/redirection/{snap_token}",
        }
        last_err: Optional[str] = None
        for attempt in range(1, LINK_RETRY_LIMIT + 2):
            r = self.ext.post(url, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 201:
                data = r.json()
                m = re.search(r"reference=([a-f0-9-]{36})", data.get("activation_link_url", ""))
                if not m:
                    raise GoPayError(f"midtrans linking 201 but no reference: {data}")
                ref = m.group(1)
                self.log(f"[gopay] midtrans linking ok reference={ref}")
                return ref
            if r.status_code == 406:
                try:
                    j = r.json()
                except Exception:
                    j = None
                if isinstance(j, dict):
                    last_err = (j.get("error_messages") or ["?"])[0]
                elif isinstance(j, list) and j:
                    last_err = str(j[0])
                else:
                    last_err = r.text[:120]
                self.log(f"[gopay] midtrans linking 406 ({last_err}), 冷却 {LINK_RETRY_SLEEP_S}s 再重试 {attempt}/{LINK_RETRY_LIMIT}")
                time.sleep(LINK_RETRY_SLEEP_S)
                continue
            raise GoPayError(
                f"midtrans linking unexpected status={r.status_code} body={r.text[:300]}",
            )
        raise GoPayError(f"midtrans linking exhausted retries: {last_err}")

    # ───── Step 8-12: GoPay linking ─────

    def _gopay_validate_reference(self, reference_id: str):
        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/validate-reference",
            json={"reference_id": reference_id},
            headers={"Origin": "https://merchants-gws-app.gopayapi.com",
                     "Referer": "https://merchants-gws-app.gopayapi.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        if not r.json().get("success"):
            raise GoPayError(f"validate-reference failed: {r.text[:300]}")

    def _gopay_user_consent(self, reference_id: str):
        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/user-consent",
            json={"reference_id": reference_id},
            headers={"Origin": "https://merchants-gws-app.gopayapi.com",
                     "Referer": "https://merchants-gws-app.gopayapi.com/",
                     "x-user-locale": "en-US"},
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        if not r.json().get("success"):
            raise GoPayError(f"user-consent failed: {r.text[:300]}")
        self.log("[gopay] consent ok, OTP sent via WhatsApp")

    def _gopay_validate_otp(self, reference_id: str, otp: str) -> tuple[str, str]:
        """Returns (challenge_id, client_id) for PIN tokenization."""
        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/validate-otp",
            json={"reference_id": reference_id, "otp": otp},
            headers={"Origin": "https://merchants-gws-app.gopayapi.com",
                     "Referer": "https://merchants-gws-app.gopayapi.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise GoPayError(f"validate-otp failed: {data}")
        challenge = (
            data.get("data", {}).get("challenge", {}).get("action", {}).get("value", {})
        )
        challenge_id = challenge.get("challenge_id") or ""
        client_id = challenge.get("client_id") or ""
        if not challenge_id or not client_id:
            raise GoPayError(f"validate-otp: missing challenge details {data}")
        self.log(f"[gopay] otp ok challenge_id={challenge_id[:8]}…")
        return challenge_id, client_id

    def _tokenize_pin(self, challenge_id: str, client_id: str) -> str:
        """POST customer.gopayapi.com/api/v1/users/pin/tokens/nb → JWT."""
        r = self.ext.post(
            "https://customer.gopayapi.com/api/v1/users/pin/tokens/nb",
            json={"challenge_id": challenge_id, "client_id": client_id, "pin": self.pin},
            headers={
                "x-appversion": "1.0.0",
                "x-correlation-id": str(uuid.uuid4()),
                "x-is-mobile": "false",
                "x-platform": "Mac OS 12.2.1",
                "x-request-id": str(uuid.uuid4()),
                "x-user-locale": "id",
                "Origin": "https://pin-web-client.gopayapi.com",
                "Referer": "https://pin-web-client.gopayapi.com/",
            },
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code in (400, 401, 403):
            raise GoPayPINRejected(f"PIN rejected: {r.text[:200]}")
        r.raise_for_status()
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        # Token can be in different shapes; check common keys
        token = (
            body.get("token")
            or body.get("data", {}).get("token")
            or body.get("data", {}).get("pin_token")
            or ""
        )
        if not token:
            # Some flows return the JWT in a wrapper; check for raw redirect URL
            # hash extraction not needed since the JWT is in the body for /nb endpoints
            raise GoPayError(f"pin tokenize: no token in response {r.text[:300]}")
        return token

    def _gopay_validate_pin(self, reference_id: str, pin_token: str):
        r = self.ext.post(
            "https://gwa.gopayapi.com/v1/linking/validate-pin",
            json={"reference_id": reference_id, "token": pin_token},
            headers={"Origin": "https://merchants-gws-app.gopayapi.com",
                     "Referer": "https://merchants-gws-app.gopayapi.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        if not r.json().get("success"):
            raise GoPayError(f"validate-pin failed: {r.text[:300]}")
        self.log("[gopay] linking complete")

    # ───── Step 13: Midtrans charge initiation ─────

    def _midtrans_create_charge(self, snap_token: str) -> str:
        """POST snap/v2/transactions/{snap}/charge → charge_ref like A12..."""
        url = f"https://app.midtrans.com/snap/v2/transactions/{snap_token}/charge"
        headers = {
            **self._midtrans_basic_auth(),
            "Content-Type": "application/json",
            "Origin": "https://app.midtrans.com",
            "Referer": f"https://app.midtrans.com/snap/v4/redirection/{snap_token}",
        }
        r = self.ext.post(
            url,
            json={"payment_type": "gopay", "tokenization": "true", "promo_details": None},
            headers=headers, timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        link = data.get("gopay_verification_link_url", "")
        m = re.search(r"reference=([A-Za-z0-9]+)", link)
        if not m:
            raise GoPayError(f"midtrans charge: no reference in {link!r}")
        charge_ref = m.group(1)
        self.log(f"[gopay] midtrans charge ref={charge_ref}")
        return charge_ref

    # ───── Step 14: GoPay charge processing ─────

    def _gopay_payment_validate(self, charge_ref: str):
        # midtrans 创建 charge 后 GoPay 后端要数秒才能 fetch；轮询直到 ready
        for i in range(8):
            r = self.ext.get(
                f"https://gwa.gopayapi.com/v1/payment/validate?reference_id={charge_ref}",
                headers={"Origin": "https://merchants-gws-app.gopayapi.com",
                         "Referer": "https://merchants-gws-app.gopayapi.com/"},
                timeout=DEFAULT_TIMEOUT,
            )
            if r.status_code == 200 and r.json().get("success"):
                return
            time.sleep(1.5)
        raise GoPayError(f"payment/validate failed after retries: {r.status_code} {r.text[:200]}")

    def _gopay_payment_confirm(self, charge_ref: str) -> tuple[str, str]:
        """Returns (challenge_id, client_id) for the charge PIN."""
        r = self.ext.post(
            f"https://gwa.gopayapi.com/v1/payment/confirm?reference_id={charge_ref}",
            json={"payment_instructions": []},
            headers={"Origin": "https://merchants-gws-app.gopayapi.com",
                     "Referer": "https://merchants-gws-app.gopayapi.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise GoPayError(f"payment/confirm failed: {data}")
        ch = data.get("data", {}).get("challenge", {}).get("action", {}).get("value", {})
        return ch.get("challenge_id", ""), ch.get("client_id", "")

    def _gopay_payment_process(self, charge_ref: str, pin_token: str):
        r = self.ext.post(
            f"https://gwa.gopayapi.com/v1/payment/process?reference_id={charge_ref}",
            json={
                "challenge": {
                    "type": "GOPAY_PIN_CHALLENGE",
                    "value": {"pin_token": pin_token},
                },
            },
            headers={"Origin": "https://merchants-gws-app.gopayapi.com",
                     "Referer": "https://merchants-gws-app.gopayapi.com/"},
            timeout=DEFAULT_TIMEOUT,
        )
        if r.status_code != 200:
            raise GoPayError(f"payment/process {r.status_code}: {r.text[:600]}")
        data = r.json()
        if not data.get("success") or data.get("data", {}).get("next_action") != "payment-success":
            raise GoPayError(f"payment/process failed: {data}")
        self.log("[gopay] charge settled")

    # ───── Step 15: Stripe + ChatGPT verify ─────

    def _chatgpt_verify(self, cs_id: str) -> dict:
        """Poll chatgpt verify until plan is active."""
        deadline = time.time() + 60
        while time.time() < deadline:
            r = self.cs.get(
                "https://chatgpt.com/checkout/verify",
                params={
                    "stripe_session_id": cs_id,
                    "processor_entity": "openai_llc",
                    "plan_type": "plus",
                },
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code == 200:
                self.log("[gopay] chatgpt verify ok")
                return {"state": "succeeded", "cs_id": cs_id}
            time.sleep(2)
        return {"state": "verify_timeout", "cs_id": cs_id}

    # ───── Top-level driver ─────

    def run(self, stripe_pk: str, billing: Optional[dict] = None) -> dict:
        billing = billing or {}
        cs_id = self._chatgpt_create_checkout()
        pm_id = self._stripe_create_pm(cs_id, stripe_pk, billing)
        self._stripe_confirm(cs_id, pm_id, stripe_pk)
        self._chatgpt_approve(cs_id)
        snap_token = self._follow_redirect_to_midtrans(cs_id, stripe_pk)
        return self._run_midtrans_and_gopay(snap_token, cs_id)

    def run_from_redirect(self, pm_redirect_url: str, cs_id: str = "") -> dict:
        """半自动模式：用户在浏览器走到 pm-redirects.stripe.com 那一步，把
        URL 粘过来；gopay 接管 Midtrans linking + OTP + PIN + 扣款 + verify。
        """
        snap_token = self._fetch_pm_redirect_snap_token(pm_redirect_url)
        self.log(f"[gopay] midtrans snap_token={snap_token}")
        return self._run_midtrans_and_gopay(snap_token, cs_id)

    def _run_midtrans_and_gopay(self, snap_token: str, cs_id: str) -> dict:
        self._midtrans_load_transaction(snap_token)
        reference_id = self._midtrans_init_linking(snap_token)

        # ── Linking: OTP + first PIN
        self._gopay_validate_reference(reference_id)
        self._gopay_user_consent(reference_id)
        otp = self.otp_provider()
        if not otp:
            raise OTPCancelled("OTP not provided")
        challenge_id, client_id = self._gopay_validate_otp(reference_id, otp)
        pin_token = self._tokenize_pin(challenge_id, client_id)
        self._gopay_validate_pin(reference_id, pin_token)

        # ── Charge: second PIN
        charge_ref = self._midtrans_create_charge(snap_token)
        self._gopay_payment_validate(charge_ref)
        ch2_id, ch2_client = self._gopay_payment_confirm(charge_ref)
        pin_token2 = self._tokenize_pin(ch2_id, ch2_client)
        self._gopay_payment_process(charge_ref, pin_token2)

        if cs_id:
            return self._chatgpt_verify(cs_id)
        return {"state": "succeeded", "snap_token": snap_token, "charge_ref": charge_ref}


# ──────────────────────────── OTP providers ───────────────────────


def cli_otp_provider() -> str:
    """Read OTP from stdin (CLI mode)."""
    sys.stdout.write("\n[GoPay] Enter WhatsApp OTP: ")
    sys.stdout.flush()
    return sys.stdin.readline().strip()


def file_watch_otp_provider(watch_path: Path, timeout: float = 300.0) -> Callable[[], str]:
    """Build an OTP provider that polls a file for the OTP value.

    Used by webui runner: emits 'GOPAY_OTP_REQUEST' marker on stdout, then
    blocks reading watch_path until it appears. The webui runner writes the
    OTP into the file when the user submits via the modal.
    """

    def provider() -> str:
        # Signal to outer runner that OTP is needed
        print(f"GOPAY_OTP_REQUEST path={watch_path}", flush=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if watch_path.exists():
                otp = watch_path.read_text(encoding="utf-8").strip()
                try:
                    watch_path.unlink()
                except FileNotFoundError:
                    pass
                if otp:
                    return otp
            time.sleep(0.5)
        raise OTPCancelled(f"OTP timeout after {timeout}s (file={watch_path})")

    return provider


def _clean_otp_candidate(value: Any) -> str:
    code = re.sub(r"\D", "", str(value or ""))
    if 4 <= len(code) <= 8:
        return code
    return ""


def _extract_otp_from_text(text: str, code_regex: str = DEFAULT_OTP_REGEX) -> str:
    """Extract the most likely WhatsApp OTP from a text blob.

    Keyword-aware patterns run before the generic regex to avoid confusing
    amounts / phone numbers with OTPs in verbose WhatsApp messages.
    """
    if not text:
        return ""
    patterns = [
        r"(?:otp|one[-\s]*time|verification|verify|code|kode|verifikasi|gopay|whatsapp|验证码|驗證碼)[^\d]{0,80}(\d{4,8})(?!\d)",
        r"(?<!\d)(\d{4,8})(?!\d)[^\n\r]{0,80}(?:otp|one[-\s]*time|verification|verify|code|kode|verifikasi|gopay|验证码|驗證碼)",
        code_regex or DEFAULT_OTP_REGEX,
    ]
    for pattern in patterns:
        try:
            matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL))
        except re.error:
            continue
        for match in reversed(matches):
            groups = match.groups() or (match.group(0),)
            for group in reversed(groups):
                code = _clean_otp_candidate(group)
                if code:
                    return code
    return ""


def _json_path_get(obj: Any, path: str) -> Any:
    cur = obj
    for part in (path or "").split("."):
        part = part.strip()
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            return None
    return cur


def _parse_payload_timestamp(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:  # milliseconds
            ts /= 1000.0
        if 946684800 <= ts <= 4102444800:  # 2000-01-01 .. 2100-01-01
            return ts
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        return _parse_payload_timestamp(float(text))
    try:
        return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _dict_timestamp(obj: dict) -> Optional[float]:
    for key in ("ts", "timestamp", "time", "created_at", "received_at", "date"):
        if key in obj:
            ts = _parse_payload_timestamp(obj.get(key))
            if ts is not None:
                return ts
    return None


def _iter_json_message_candidates(obj: Any) -> Any:
    """Yield (text, timestamp) candidates from generic relay / Meta webhook JSON."""
    if isinstance(obj, dict):
        ts = _dict_timestamp(obj)
        pieces: list[str] = []
        for key in ("otp", "code", "body", "message", "text", "content", "caption", "raw"):
            if key not in obj:
                continue
            value = obj.get(key)
            if isinstance(value, dict):
                body = value.get("body") or value.get("text") or value.get("message")
                if body not in (None, ""):
                    pieces.append(str(body))
            elif isinstance(value, (str, int, float)):
                pieces.append(str(value))
        if pieces:
            yield " ".join(pieces), ts
        for value in obj.values():
            yield from _iter_json_message_candidates(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_json_message_candidates(item)
    elif isinstance(obj, str):
        yield obj, None


def _extract_otp_from_payload(
    payload: Any,
    *,
    code_regex: str = DEFAULT_OTP_REGEX,
    json_path: str = "",
    issued_after: float = 0.0,
) -> str:
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped[:1] in ("{", "["):
            try:
                payload = json.loads(stripped)
            except Exception:
                return _extract_otp_from_text(payload, code_regex=code_regex)
        else:
            return _extract_otp_from_text(payload, code_regex=code_regex)

    if json_path:
        target = _json_path_get(payload, json_path)
        if target is None:
            return ""
        if not isinstance(target, str):
            target = json.dumps(target, ensure_ascii=False)
        return _extract_otp_from_text(target, code_regex=code_regex)

    found = ""
    for text, ts in _iter_json_message_candidates(payload):
        if issued_after and ts is not None and ts < issued_after:
            continue
        code = _extract_otp_from_text(text, code_regex=code_regex)
        if code:
            found = code
    return found


def _float_cfg(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _headers_cfg(raw: Any) -> dict:
    return raw if isinstance(raw, dict) else {}


def whatsapp_file_otp_provider(
    path: Path,
    *,
    timeout: float = 300.0,
    interval: float = 1.0,
    code_regex: str = DEFAULT_OTP_REGEX,
    json_path: str = "",
    issued_after_slack_s: float = 15.0,
    delete_after_read: bool = False,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    """Poll a local WhatsApp relay state/log file and extract a fresh OTP."""

    def provider() -> str:
        issued_after = time.time() - max(0.0, issued_after_slack_s)
        deadline = time.time() + timeout
        last_error = ""
        log(f"[gopay] waiting WhatsApp OTP from file: {path}")
        while time.time() < deadline:
            try:
                if path.exists():
                    stat = path.stat()
                    if stat.st_mtime >= issued_after:
                        text = path.read_text(encoding="utf-8", errors="replace")
                        code = _extract_otp_from_payload(
                            text,
                            code_regex=code_regex,
                            json_path=json_path,
                            issued_after=issued_after,
                        )
                        if code:
                            if delete_after_read:
                                try:
                                    path.unlink()
                                except FileNotFoundError:
                                    pass
                            return code
                last_error = ""
            except Exception as exc:
                last_error = str(exc)
            time.sleep(max(0.2, interval))
        detail = f"; last_error={last_error}" if last_error else ""
        raise OTPCancelled(f"OTP timeout after {timeout}s (file={path}{detail})")

    return provider


def whatsapp_http_otp_provider(
    url: str,
    *,
    timeout: float = 300.0,
    interval: float = 1.0,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    code_regex: str = DEFAULT_OTP_REGEX,
    json_path: str = "",
    issued_after_slack_s: float = 15.0,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    """Poll a local/owned WhatsApp relay HTTP endpoint for the latest OTP.

    The endpoint may return plain text or JSON. JSON can either expose the code
    directly (for example {"otp":"123456"}) or contain a WhatsApp Cloud API-like
    message payload; timestamps are honored when present.
    """

    def provider() -> str:
        issued_after = time.time() - max(0.0, issued_after_slack_s)
        deadline = time.time() + timeout
        sess = requests.Session()
        base_params = dict(params or {})
        last_error = ""
        log(f"[gopay] waiting WhatsApp OTP from relay: {url}")
        while time.time() < deadline:
            try:
                req_params = dict(base_params)
                if "since" not in req_params:
                    req_params["since"] = str(int(issued_after))
                resp = sess.get(
                    url,
                    headers=headers or {},
                    params=req_params,
                    timeout=min(10.0, max(2.0, interval + 1.0)),
                )
                if resp.status_code in (204, 404):
                    time.sleep(max(0.2, interval))
                    continue
                resp.raise_for_status()
                try:
                    payload: Any = resp.json()
                except ValueError:
                    payload = resp.text
                code = _extract_otp_from_payload(
                    payload,
                    code_regex=code_regex,
                    json_path=json_path,
                    issued_after=issued_after,
                )
                if code:
                    return code
                last_error = ""
            except Exception as exc:
                last_error = str(exc)
            time.sleep(max(0.2, interval))
        detail = f"; last_error={last_error}" if last_error else ""
        raise OTPCancelled(f"OTP timeout after {timeout}s (url={url}{detail})")

    return provider


def command_otp_provider(
    command: Any,
    *,
    timeout: float = 300.0,
    interval: float = 2.0,
    code_regex: str = DEFAULT_OTP_REGEX,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    """Poll a user-owned command that prints the latest WhatsApp OTP."""
    argv = command if isinstance(command, list) else shlex.split(str(command or ""))
    if not argv:
        raise GoPayError("gopay.otp.command is empty")

    def provider() -> str:
        deadline = time.time() + timeout
        last_error = ""
        log(f"[gopay] waiting WhatsApp OTP from command: {argv[0]}")
        while time.time() < deadline:
            try:
                proc = subprocess.run(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=min(20.0, max(2.0, interval + 1.0)),
                    check=False,
                )
                text = (proc.stdout or "") + "\n" + (proc.stderr or "")
                code = _extract_otp_from_text(text, code_regex=code_regex)
                if code:
                    return code
                if proc.returncode not in (0, 1):
                    last_error = f"exit={proc.returncode}: {text.strip()[:160]}"
            except Exception as exc:
                last_error = str(exc)
            time.sleep(max(0.2, interval))
        detail = f"; last_error={last_error}" if last_error else ""
        raise OTPCancelled(f"OTP timeout after {timeout}s (command{detail})")

    return provider


def build_configured_otp_provider(
    gopay_cfg: dict,
    *,
    fallback_provider: Callable[[], str] = cli_otp_provider,
    log: Callable[[str], None] = print,
) -> Callable[[], str]:
    """Build OTP provider from gopay.otp config, falling back to manual input.

    Supported config:
      "gopay": {
        "otp": {
          "source": "http" | "file" | "command" | "manual" | "auto",
          "url": "http://127.0.0.1:8765/api/whatsapp/latest-otp?token=...",
          "url": "http://127.0.0.1:8765/api/whatsapp/latest-otp?token=...",
          "command": ["python", "scripts/get_wa_otp.py"],
          "timeout": 300,
          "interval": 1,
          "code_regex": "(?<!\\d)(\\d{6})(?!\\d)",
          "issued_after_slack_s": 15
        }
      }
    """
    otp_cfg = gopay_cfg.get("otp") or gopay_cfg.get("otp_provider") or {}
    if not isinstance(otp_cfg, dict) or not otp_cfg:
        return fallback_provider

    source = str(otp_cfg.get("source") or otp_cfg.get("type") or "auto").strip().lower()
    if source in ("", "manual", "cli", "stdin"):
        return fallback_provider

    timeout = _float_cfg(otp_cfg, "timeout", _float_cfg(otp_cfg, "timeout_s", 300.0))
    interval = _float_cfg(otp_cfg, "interval", _float_cfg(otp_cfg, "poll_interval_s", 1.0))
    code_regex = str(otp_cfg.get("code_regex") or DEFAULT_OTP_REGEX)
    json_path = str(otp_cfg.get("json_path") or "")
    slack = _float_cfg(otp_cfg, "issued_after_slack_s", 15.0)

    env_url = os.getenv("WEBUI_GOPAY_OTP_URL", "").strip()
    url = str(otp_cfg.get("url") or otp_cfg.get("relay_url") or env_url or "").strip()
    path = str(
        otp_cfg.get("path")
        or otp_cfg.get("state_file")
        or otp_cfg.get("log_file")
        or ""
    ).strip()
    command = otp_cfg.get("command") or otp_cfg.get("cmd")

    if url and (source in ("auto", "http", "https", "relay", "whatsapp_http", "wa_http") or env_url):
        return whatsapp_http_otp_provider(
            url,
            timeout=timeout,
            interval=interval,
            headers=_headers_cfg(otp_cfg.get("headers")),
            params=otp_cfg.get("params") if isinstance(otp_cfg.get("params"), dict) else None,
            code_regex=code_regex,
            json_path=json_path,
            issued_after_slack_s=slack,
            log=log,
        )

    if source in ("auto", "file", "state_file", "log", "whatsapp_file", "wa_file"):
        if path:
            return whatsapp_file_otp_provider(
                Path(path).expanduser(),
                timeout=timeout,
                interval=interval,
                code_regex=code_regex,
                json_path=json_path,
                issued_after_slack_s=slack,
                delete_after_read=bool(otp_cfg.get("delete_after_read", False)),
                log=log,
            )
        if source != "auto":
            raise GoPayError("gopay.otp source=file requires path/state_file/log_file")

    if source in ("auto", "command", "cmd"):
        if command:
            return command_otp_provider(
                command,
                timeout=timeout,
                interval=interval,
                code_regex=code_regex,
                log=log,
            )
        if source != "auto":
            raise GoPayError("gopay.otp source=command requires command")

    if source == "auto":
        return fallback_provider
    raise GoPayError(f"unsupported gopay.otp source: {source}")


# ──────────────────────────── chatgpt session ─────────────────────


def _build_chatgpt_session(auth_cfg: dict) -> Any:
    """Build a chatgpt-authed session with chrome TLS fingerprint + OAI headers.

    /backend-api/payments/checkout requires: Cookie session-token, Bearer
    access_token, oai-device-id, x-openai-target-path/route, sentinel token.
    We supply everything except sentinel — caller refreshes via
    _ensure_sentinel before each protected call.
    """
    session_token = (auth_cfg.get("session_token") or "").strip()
    access_token = (auth_cfg.get("access_token") or "").strip()
    cookie_header = (auth_cfg.get("cookie_header") or "").strip()
    device_id = (auth_cfg.get("device_id") or "").strip() or str(uuid.uuid4())
    user_agent = auth_cfg.get("user_agent") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    )

    if not (session_token or cookie_header):
        raise GoPayError(
            "auth missing: need session_token or cookie_header in config",
        )

    s = _new_session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Content-Type": "application/json",
        "oai-device-id": device_id,
        "oai-language": "en-US",
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    if access_token:
        s.headers["Authorization"] = f"Bearer {access_token}"

    parts = []
    seen = set()
    for raw in (cookie_header or "").split(";"):
        p = raw.strip()
        if p and "=" in p:
            n = p.split("=", 1)[0].strip()
            if n and n not in seen:
                seen.add(n)
                parts.append(p)
    if session_token and "__Secure-next-auth.session-token" not in seen:
        parts.append(f"__Secure-next-auth.session-token={session_token}")
    if device_id and "oai-did" not in seen:
        parts.append(f"oai-did={device_id}")
    s.headers["Cookie"] = "; ".join(parts)
    # Cache device_id on session for subsequent header use
    s._oai_device_id = device_id  # type: ignore[attr-defined]
    return s


# ──────────────────────────── CLI entry ───────────────────────────


def _load_cfg(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(
        description="ChatGPT Plus 订阅 via GoPay tokenization",
    )
    parser.add_argument("--config", required=True, help="CTF-pay config json")
    parser.add_argument("--otp-file", default="",
                        help="webui mode: poll this file for OTP (file deleted after read)")
    parser.add_argument("--otp-timeout", type=float, default=300.0,
                        help="seconds to wait for OTP file")
    parser.add_argument("--json-result", action="store_true",
                        help="Emit GOPAY_RESULT_JSON=... line on success")
    parser.add_argument("--from-redirect-url", default="", metavar="URL",
                        help="半自动模式：跳过 chatgpt+stripe 前段，直接从 pm-redirects.stripe.com URL 接管 Midtrans+GoPay")
    parser.add_argument("--cs-id", default="", help="可选：cs_live_xxx，verify 阶段用")
    args = parser.parse_args()

    cfg = _load_cfg(args.config)
    gopay_cfg = cfg.get("gopay") or {}
    if not gopay_cfg:
        print("[error] config has no 'gopay' block", file=sys.stderr)
        sys.exit(2)
    if not all(k in gopay_cfg for k in ("country_code", "phone_number", "pin")):
        print("[error] gopay block missing country_code / phone_number / pin",
              file=sys.stderr)
        sys.exit(2)

    auth_cfg = (cfg.get("fresh_checkout") or {}).get("auth") or {}
    try:
        cs_session = _build_chatgpt_session(auth_cfg)
    except GoPayError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(2)
    # Apply proxy from config to both chatgpt + ext sessions
    proxy_url = (cfg.get("proxy") or "").strip() or None

    stripe_pk = (
        (cfg.get("stripe") or {}).get("publishable_key")
        or auth_cfg.get("stripe_pk")
        or DEFAULT_STRIPE_PK
    )

    billing = cfg.get("billing") or {}

    if args.otp_file:
        provider = file_watch_otp_provider(Path(args.otp_file), timeout=args.otp_timeout)
    else:
        provider = build_configured_otp_provider(gopay_cfg, fallback_provider=cli_otp_provider)

    charger = GoPayCharger(
        cs_session, gopay_cfg,
        otp_provider=provider, proxy=proxy_url,
        runtime_cfg=cfg.get("runtime"),
    )
    try:
        if args.from_redirect_url:
            print(f"[gopay] semi-auto mode: starting from {args.from_redirect_url[:80]}...")
            result = charger.run_from_redirect(args.from_redirect_url, cs_id=args.cs_id)
        else:
            result = charger.run(stripe_pk=stripe_pk, billing=billing)
    except GoPayError as e:
        print(f"[gopay] FAILED: {e}", file=sys.stderr)
        if args.json_result:
            print(f"GOPAY_RESULT_JSON={json.dumps({'state':'failed','error':str(e)})}")
        sys.exit(1)

    print(f"[gopay] result: {result}")
    if args.json_result:
        print(f"GOPAY_RESULT_JSON={json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
