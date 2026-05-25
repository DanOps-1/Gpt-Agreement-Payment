"""Promo long URL scraping: use account access_token + cookie to call ChatGPT checkout API,
get the hosted long URL (https://checkout.stripe.com/c/pay/cs_live_...) that hits promo.

Not actually paying, just get URL and store in DB. Flow:

  register() / login   →  {email, access_token, cookie_header, device_id}
        ↓
  fetch_promo_link()   →  POST chatgpt.com/backend-api/payments/checkout
                          body={entry_point, plan_name, billing_details, promo_campaign}
                          → {checkout_url, checkout_session_id, processor_entity, amount_due, ...}
        ↓
  db.add_promo_link()  →  store promo_links table"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)


def _new_session(impersonate: str = "chrome136"):
    """curl_cffi session with Chrome TLS fingerprint (bypass Cloudflare); fallback to requests if not installed."""
    try:
        from curl_cffi.requests import Session as CurlSession  # type: ignore
        return CurlSession(impersonate=impersonate)
    except ImportError:
        import requests
        return requests.Session()


def _build_checkout_body(
    plan: str = "plus",
    country: str = "ID",
    currency: str = "IDR",
    promo_campaign_id: str = "",
    checkout_ui_mode: str = "hosted",
) -> dict:
    """Minimal payload consistent with card/_monolith._build_fresh_checkout_body.

    plus defaults to promo plus-1-month-free, team defaults to team-1-month-free.
    checkout_ui_mode=hosted makes server return Stripe long URL (what we want); custom returns in-app short URL."""
    is_plus = "plus" in plan.lower()
    plan_name = "chatgptplusplan" if is_plus else "chatgptteamplan"
    entry_point = "all_plans_pricing_modal" if is_plus else "team_workspace_purchase_modal"
    if not promo_campaign_id:
        promo_campaign_id = "plus-1-month-free" if is_plus else "team-1-month-free"
    body = {
        "entry_point": entry_point,
        "plan_name": plan_name,
        "billing_details": {"country": country, "currency": currency},
        "cancel_url": "https://chatgpt.com/#pricing",
        "checkout_ui_mode": checkout_ui_mode,
        "promo_campaign": {
            "promo_campaign_id": promo_campaign_id,
            "is_coupon_from_query_param": False,
        },
    }
    if not is_plus:
        body["team_plan_data"] = {
            "workspace_name": "MyWorkspace",
            "price_interval": "month",
            "seat_quantity": 5,
        }
    return body


def fetch_promo_link(
    access_token: str,
    cookie_header: str = "",
    device_id: str = "",
    plan: str = "plus",
    country: str = "ID",
    currency: str = "IDR",
    promo_campaign_id: str = "",
    proxy_url: str = "",
    chatgpt_account_id: str = "",
    timeout: int = 30,
) -> dict:
    """Call ChatGPT checkout API to get hosted long URL.

    Return dict: {ok, checkout_url, cs_id, processor_entity, plan_name,
              promo_campaign_id, amount_due_cents, billing_country,
              billing_currency, raw, error?}"""
    if not access_token:
        return {"ok": False, "error": "missing access_token"}

    body = _build_checkout_body(plan, country, currency, promo_campaign_id, "hosted")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    if device_id:
        headers["oai-device-id"] = device_id
    if chatgpt_account_id:
        headers["chatgpt-account-id"] = chatgpt_account_id

    s = _new_session()
    proxies = None
    if proxy_url:
        pu = proxy_url.replace("socks5://", "socks5h://")
        proxies = {"http": pu, "https": pu}

    try:
        resp = s.post(
            "https://chatgpt.com/backend-api/payments/checkout",
            headers=headers,
            json=body,
            proxies=proxies,
            timeout=timeout,
        )
    except Exception as e:
        return {"ok": False, "error": f"POST failed: {type(e).__name__}: {e}"}

    if resp.status_code != 200:
        return {
            "ok": False,
            "error": f"http {resp.status_code}: {(resp.text or '')[:200]}",
            "status_code": resp.status_code,
        }

    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "error": f"non-JSON response: {(resp.text or '')[:200]}"}

    cs_id = (data.get("checkout_session_id") or data.get("session_id") or "").strip()
    processor_entity = (data.get("processor_entity") or "").strip()
    checkout_url = (
        data.get("checkout_url")
        or data.get("url")
        or data.get("openai_checkout_url")
        or ""
    ).strip()

    # Fallback to extract cs_id / processor_entity from URL
    if not cs_id and checkout_url:
        m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", checkout_url)
        if m:
            cs_id = m.group(1)
    if not processor_entity and checkout_url:
        m = re.search(r"/checkout/([^/]+)/cs_(?:live|test)_", checkout_url)
        if m:
            processor_entity = m.group(1)

    amount_due = int(data.get("amount_due", 0) or data.get("amount_due_cents", 0) or 0)
    promo_id = (
        ((data.get("promo_campaign") or {}).get("promo_campaign_id"))
        or body["promo_campaign"]["promo_campaign_id"]
    )

    if not checkout_url:
        return {"ok": False, "error": "response 缺 checkout_url", "raw": data}

    return {
        "ok": True,
        "checkout_url": checkout_url,
        "cs_id": cs_id,
        "processor_entity": processor_entity,
        "plan_name": body["plan_name"],
        "promo_campaign_id": promo_id,
        "billing_country": country,
        "billing_currency": currency,
        "amount_due_cents": amount_due,
        "raw": data,
    }
