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
import base64
import binascii
import datetime as _dt
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests

try:
    from pay_trace import install_requests_trace, trace_session
    install_requests_trace("gopay.py")
except Exception:
    def trace_session(session_obj: Any, label: str = "session") -> Any:
        return session_obj

# Cloudflare 拦 plain requests 的 TLS 指纹（403 + HTML challenge），跟 card.py 一致用 curl_cffi
# 模拟真 Chrome 指纹。
try:
    from curl_cffi.requests import Session as _CurlCffiSession  # type: ignore
except ImportError:
    _CurlCffiSession = None  # type: ignore

try:
    from webui.backend.gopay_signer import signed_headers as _signed_gopay_headers
except Exception:
    try:
        _repo_root = Path(__file__).resolve().parents[1]
        if str(_repo_root) not in sys.path:
            sys.path.insert(0, str(_repo_root))
        from webui.backend.gopay_signer import signed_headers as _signed_gopay_headers
    except Exception:
        _signed_gopay_headers = None  # type: ignore

try:
    from webui.backend import gopay_auto_unbind as _gopay_auto_unbind
except Exception:
    try:
        _repo_root = Path(__file__).resolve().parents[1]
        if str(_repo_root) not in sys.path:
            sys.path.insert(0, str(_repo_root))
        from webui.backend import gopay_auto_unbind as _gopay_auto_unbind
    except Exception:
        _gopay_auto_unbind = None  # type: ignore


def _new_session(impersonate: str = "chrome136") -> Any:
    """Build session with chrome TLS fingerprint when available."""
    if _CurlCffiSession is not None:
        return trace_session(_CurlCffiSession(impersonate=impersonate), "gopay.curl_cffi")
    return trace_session(requests.Session(), "gopay.requests")


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
LINK_429_RETRY_LIMIT = 30
LINK_429_RETRY_SLEEP_MIN_S = 2.0
LINK_429_RETRY_SLEEP_MAX_S = 3.0
CHARGE_RETRY_LIMIT = 4
CHARGE_RETRY_SLEEP_S = 2.0
QR_WAIT_TIMEOUT_S = 300.0
QRIS_EXPLORE_RETRY_LIMIT = 3
QRIS_EXPLORE_RETRY_SLEEP_S = 2.0
GOPAY_CUSTOMER_BASE_URL = "https://customer.gopayapi.com"
TRANSIENT_REQUEST_RETRY_LIMIT = 3
TRANSIENT_REQUEST_RETRY_MIN_S = 1.0
TRANSIENT_REQUEST_RETRY_MAX_S = 2.5
DEFAULT_OTP_REGEX = r"(?<!\d)(\d{6})(?!\d)"


def _looks_transient_request_error(exc: Exception) -> bool:
    text = str(exc).lower()
    name = type(exc).__name__.lower()
    transient_words = (
        "curlerror",
        "sslerror",
        "connectionerror",
        "connect error",
        "connection reset",
        "connection aborted",
        "connection refused",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "remote end closed",
        "invalid library",
    )
    return any(word in name or word in text for word in transient_words)


def _safe_headers_for_log(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    sensitive = ("authorization", "cookie", "set-cookie", "x-api-key")
    interesting_prefixes = (
        "retry-after",
        "x-ratelimit",
        "ratelimit",
        "cf-",
        "server",
        "date",
        "content-type",
        "via",
        "x-request",
        "x-correlation",
        "x-amzn",
    )
    try:
        items = headers.items()
    except Exception:
        return out
    for key, value in items:
        k = str(key)
        lk = k.lower()
        if any(s in lk for s in sensitive):
            out[k] = "<redacted>"
            continue
        if any(lk == p or lk.startswith(p) for p in interesting_prefixes):
            out[k] = str(value)[:200]
    return out


def _normalize_proxy_url(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    return f"http://{proxy}"


def _proxy_list_from_cfg(cfg: Optional[dict], primary_proxy: Optional[str] = None) -> list[str]:
    out: list[str] = []
    if primary_proxy:
        out.append(_normalize_proxy_url(str(primary_proxy).strip()))
    return out


def _json_dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _raw_headers_to_dict(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return _raw_headers_to_dict(parsed)
    except Exception:
        pass
    if "\r\n\r\n" in text:
        text = text.split("\r\n\r\n", 1)[0]
    elif "\n\n" in text:
        text = text.split("\n\n", 1)[0]
    headers: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line.upper().startswith(("GET ", "POST ", "PATCH ", "PUT ", "DELETE ")):
            continue
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        name = name.strip()
        value = value.strip()
        if name and value and name.lower() not in ("host", "content-length", "connection"):
            headers[name] = value
    return headers


def _merge_header_sources(*sources: Any) -> dict[str, str]:
    merged: dict[str, str] = {}
    for source in sources:
        parsed = _raw_headers_to_dict(source)
        for key, value in parsed.items():
            lk = key.lower()
            for old_key in list(merged):
                if old_key.lower() == lk:
                    merged.pop(old_key, None)
            merged[key] = value
    return merged


def _auto_unbind_header_sources(gopay_cfg: dict) -> list[Any]:
    auto = gopay_cfg.get("auto_unbind") if isinstance(gopay_cfg.get("auto_unbind"), dict) else {}
    return [
        gopay_cfg.get("auto_unbind_raw_request"),
        gopay_cfg.get("auto_unbind_unlink_raw_request"),
        auto.get("raw_request"),
        auto.get("unlink_raw_request"),
    ]


def _default_qris_headers(gopay_cfg: dict) -> dict[str, str]:
    app_version = str(gopay_cfg.get("app_version") or gopay_cfg.get("x_appversion") or "2.8.0")
    device_os = str(gopay_cfg.get("device_os") or gopay_cfg.get("x_deviceos") or "Android, 12")
    return {
        "accept-encoding": "gzip",
        "country-code": str(gopay_cfg.get("gopay_country_code") or "ID"),
        "gojek-country-code": str(gopay_cfg.get("gopay_country_code") or "ID"),
        "gojek-service-area": str(gopay_cfg.get("gojek_service_area") or "1"),
        "x-appversion": app_version,
        "x-help-version": str(gopay_cfg.get("help_version") or app_version),
        "x-location": str(gopay_cfg.get("x_location") or "23.1868994,113.4191515"),
        "x-location-accuracy": str(gopay_cfg.get("x_location_accuracy") or "0.019999999552965164"),
        "custom_location": str(gopay_cfg.get("custom_location") or ""),
        "x-uniqueid": str(gopay_cfg.get("x_uniqueid") or ""),
        "x-phonemake": str(gopay_cfg.get("x_phonemake") or ""),
        "x-phonemodel": str(gopay_cfg.get("x_phonemodel") or ""),
        "x-deviceos": device_os,
        "x-user-type": str(gopay_cfg.get("x_user_type") or "customer"),
        "x-appid": str(gopay_cfg.get("x_appid") or "com.gojek.gopay"),
        "gojek-timezone": str(gopay_cfg.get("gojek_timezone") or "Asia/Jakarta"),
        "x-apptype": str(gopay_cfg.get("x_apptype") or "GOPAY"),
        "x-user-locale": str(gopay_cfg.get("x_user_locale") or "en_ID"),
        "accept-language": str(gopay_cfg.get("accept_language") or "en-ID"),
        "x-platform": str(gopay_cfg.get("x_platform") or "Android"),
        "user-agent": str(
            gopay_cfg.get("gopay_user_agent")
            or f"GoPay/{app_version} (com.gojek.gopay; build:{app_version.replace('.', '')}; {device_os})"
        ),
        "content-type": "application/json",
    }


def _parse_tlv(value: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    index = 0
    while index + 4 <= len(value):
        tag = value[index:index + 2]
        try:
            length = int(value[index + 2:index + 4])
        except ValueError:
            break
        start = index + 4
        end = start + length
        if end > len(value):
            break
        result.append((tag, value[start:end]))
        index = end
    return result


def _extract_payment_id_from_qris(qris: str) -> str:
    for tag, val in _parse_tlv(qris):
        if tag == "62":
            for nested_tag, nested_val in _parse_tlv(val):
                if nested_tag in ("50", "05", "07") and nested_val:
                    return nested_val
    return ""


def _looks_like_qris_payload(value: str) -> bool:
    text = str(value or "").strip()
    return bool(text.startswith("000201") and len(text) >= 80 and text[-4:].isalnum())


def _header_value(headers: Any, name: str) -> str:
    try:
        items = headers.items()
    except Exception:
        return ""
    for key, value in items:
        if str(key).lower() == name.lower():
            return str(value)
    return ""


def _safe_qris_header_summary(headers: dict[str, str]) -> str:
    names = sorted(str(k).lower() for k in headers)
    sensitive = {"authorization", "x-e1", "x-m1", "x-passkey", "x-devicetoken", "cookie"}
    visible = [name for name in names if name not in sensitive]
    auth = _header_value(headers, "authorization")
    x_e2 = _header_value(headers, "x-e2")
    return (
        f"fields={','.join(visible)} "
        f"auth_tail={(auth[-6:] if auth else '-')} "
        f"x-e2_tail={(x_e2[-6:] if x_e2 else '-')} "
        f"has_passkey={bool(_header_value(headers, 'x-passkey'))} "
        f"has_devicetoken={bool(_header_value(headers, 'x-devicetoken'))}"
    )


def _safe_qris_preview(qris: str) -> str:
    text = str(qris or "").strip()
    if len(text) <= 32:
        return text
    return f"{text[:18]}...{text[-10:]}"


def _json_for_log(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return str(value)


def _qris_error_code(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    if err.get("code"):
        return str(err.get("code") or "")
    errors = data.get("errors") if isinstance(data.get("errors"), list) else []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    return str(first.get("code") or "")


def _iter_nested_dicts(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_nested_dicts(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_nested_dicts(child, depth + 1)


def _data_image_suffix(content_type: str, raw: bytes = b"") -> str:
    ctype = str(content_type or "").lower()
    if "svg" in ctype or raw.lstrip().startswith(b"<svg") or b"<svg" in raw[:256].lower():
        return ".svg"
    if "jpeg" in ctype or "jpg" in ctype or raw.startswith(b"\xff\xd8"):
        return ".jpg"
    if "png" in ctype or raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if "gif" in ctype or raw.startswith(b"GIF"):
        return ".gif"
    if "webp" in ctype or raw.startswith(b"RIFF"):
        return ".webp"
    return ".png"


def _decode_data_image(value: str) -> tuple[bytes, str]:
    text = str(value or "").strip()
    m = re.match(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", text, re.I | re.S)
    if m:
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except (binascii.Error, ValueError):
            return b"", ""
        return raw, _data_image_suffix(m.group(1), raw)
    if "://" in text or len(text) < 80:
        return b"", ""
    compact = re.sub(r"\s+", "", text)
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return b"", ""
    try:
        raw = base64.b64decode(compact.replace("-", "+").replace("_", "/"), validate=False)
    except (binascii.Error, ValueError):
        return b"", ""
    suffix = _data_image_suffix("", raw)
    if suffix == ".png" and not raw.startswith((b"\x89PNG", b"\xff\xd8", b"GIF", b"RIFF")) and b"<svg" not in raw[:256].lower():
        return b"", ""
    return raw, suffix


def _phone_digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _phone_match_candidates(phone: str = "", country_code: str = "") -> set[str]:
    local = _phone_digits(phone)
    cc = _phone_digits(country_code)
    candidates: set[str] = set()
    if local:
        candidates.add(local)
        if cc and not local.startswith(cc):
            candidates.add(f"{cc}{local}")
    return candidates


def _phone_values_match(
    values: Any,
    phone: str = "",
    country_code: str = "",
) -> bool:
    candidates = _phone_match_candidates(phone, country_code)
    if not candidates:
        return True
    raw_values = values if isinstance(values, (list, tuple, set)) else [values]
    for value in raw_values:
        digits = _phone_digits(value)
        if not digits:
            continue
        if digits in candidates:
            return True
        # Accept a full international number for a locally configured account.
        if any(digits.endswith(candidate) or candidate.endswith(digits) for candidate in candidates):
            return True
    return False


def _collect_phone_values(obj: Any) -> list[str]:
    values: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_norm = str(key).lower().replace("-", "_")
            if key_norm in (
                "phone",
                "phone_number",
                "msisdn",
                "recipient_phone",
                "target_phone",
                "account_phone",
                "to_phone",
            ):
                if isinstance(value, (str, int, float)):
                    values.append(str(value))
                elif isinstance(value, dict):
                    for sub_key in ("phone", "phone_number", "msisdn", "id"):
                        sub_value = value.get(sub_key)
                        if isinstance(sub_value, (str, int, float)):
                            values.append(str(sub_value))
            elif isinstance(value, (dict, list)):
                values.extend(_collect_phone_values(value))
    elif isinstance(obj, list):
        for item in obj:
            values.extend(_collect_phone_values(item))
    return values


def _mask_phone(phone: str = "", country_code: str = "") -> str:
    digits = _phone_digits(phone)
    cc = _phone_digits(country_code)
    full = f"+{cc}{digits}" if cc and digits and not digits.startswith(cc) else digits
    if not full:
        return "-"
    clean = full if full.startswith("+") else digits
    tail = clean[-4:] if len(clean) >= 4 else clean
    prefix = f"+{cc} " if cc and not digits.startswith(cc) else ""
    return f"{prefix}***{tail}"


def _has_gopay_account_fields(cfg: dict) -> bool:
    return all(str(cfg.get(k) or "").strip() for k in ("country_code", "phone_number", "pin"))


def normalize_gopay_accounts(gopay_cfg: dict) -> list[dict]:
    """Return usable GoPay accounts from either new accounts[] or legacy fields."""
    if not isinstance(gopay_cfg, dict):
        return []

    raw_accounts = gopay_cfg.get("accounts")
    accounts: list[dict] = []
    if isinstance(raw_accounts, list):
        for idx, item in enumerate(raw_accounts):
            if not isinstance(item, dict):
                continue
            account = dict(item)
            if not account.get("country_code") and gopay_cfg.get("country_code"):
                account["country_code"] = gopay_cfg.get("country_code")
            if not account.get("midtrans_client_id") and gopay_cfg.get("midtrans_client_id"):
                account["midtrans_client_id"] = gopay_cfg.get("midtrans_client_id")
            if _has_gopay_account_fields(account):
                account["_account_index"] = idx
                accounts.append(account)

    if accounts:
        return accounts

    if _has_gopay_account_fields(gopay_cfg):
        return [{
            "label": gopay_cfg.get("label") or gopay_cfg.get("name") or "default",
            "country_code": gopay_cfg.get("country_code"),
            "phone_number": gopay_cfg.get("phone_number"),
            "pin": gopay_cfg.get("pin"),
            "midtrans_client_id": gopay_cfg.get("midtrans_client_id"),
            "_account_index": 0,
        }]
    return []


def _truthy_cfg(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on", "qr", "qris")
    return bool(value)


def is_qr_payment_enabled(gopay_cfg: dict) -> bool:
    if not isinstance(gopay_cfg, dict):
        return False
    mode = str(gopay_cfg.get("payment_mode") or gopay_cfg.get("mode") or "").strip().lower()
    return (
        mode in ("qr", "qris", "qr_payment")
        or _truthy_cfg(gopay_cfg.get("qr_payment"))
        or _truthy_cfg(gopay_cfg.get("qr_enabled"))
    )


def _looks_like_payment_return_url(value: str) -> bool:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return False
    if "pm-redirects.stripe.com/return/" in lowered:
        return True
    if "finish_redirect_url" in lowered:
        return True
    return bool(
        "transaction_status=" in lowered
        and ("status_code=" in lowered or "/return/" in lowered)
    )


def pick_gopay_account_config(
    gopay_cfg: dict,
    *,
    log: Callable[[str], None] = print,
    rng: Optional[random.Random] = None,
) -> dict:
    accounts = normalize_gopay_accounts(gopay_cfg)
    if not accounts:
        raise GoPayError("gopay config missing usable account: need country_code / phone_number / pin")

    picker = rng.choice if rng is not None else random.choice
    selected = dict(picker(accounts))
    merged = {k: v for k, v in dict(gopay_cfg).items() if k != "accounts"}
    merged.update({k: v for k, v in selected.items() if v is not None})
    merged["_selected_account"] = True
    merged["_selected_accounts_count"] = len(accounts)
    merged["_selected_account_index"] = int(selected.get("_account_index") or 0)
    merged["_selected_account_label"] = str(
        selected.get("label") or selected.get("name") or f"account-{merged['_selected_account_index'] + 1}"
    )
    log(
        "[gopay] selected account "
        f"{merged['_selected_account_index'] + 1}/{len(accounts)} "
        f"phone={_mask_phone(str(merged.get('phone_number') or ''), str(merged.get('country_code') or ''))}"
    )
    return merged


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
        proxy_cfg: Optional[dict] = None,
        runtime_cfg: Optional[dict] = None,
    ):
        self.cs = chatgpt_session
        self.qr_payment = is_qr_payment_enabled(gopay_cfg)
        if isinstance(gopay_cfg, dict) and (
            gopay_cfg.get("accounts") and not gopay_cfg.get("_selected_account")
        ):
            gopay_cfg = pick_gopay_account_config(gopay_cfg, log=log)
        self.country_code = str(gopay_cfg.get("country_code") or "62").lstrip("+")
        self.phone = _phone_digits(gopay_cfg.get("phone_number") or "")
        self.pin = str(gopay_cfg.get("pin") or "")
        self.gopay_cfg = dict(gopay_cfg or {})
        if not self.qr_payment and not (self.country_code and self.phone and self.pin):
            raise GoPayError("gopay config missing usable account: need country_code / phone_number / pin")
        self.midtrans_client_id = str(
            gopay_cfg.get("midtrans_client_id") or DEFAULT_MIDTRANS_CLIENT_ID
        )
        self.qr_wait_timeout = _float_cfg(gopay_cfg, "qr_wait_timeout", QR_WAIT_TIMEOUT_S)
        self.midtrans_page_url = ""
        self.gopay_activation_link_url = ""
        self.otp_provider = otp_provider
        self.log = log
        self.proxy_pool = _proxy_list_from_cfg(proxy_cfg, proxy)
        self.proxy_index = 0
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
        if self.proxy_pool:
            self._apply_proxy(self.proxy_pool[0])

    def _apply_proxy(self, proxy: str) -> None:
        proxy = str(proxy or "").strip()
        try:
            self.cs.proxies = {"http": proxy, "https": proxy} if proxy else {"http": "", "https": ""}
        except Exception:
            pass
        try:
            self.ext.proxies = {"http": proxy, "https": proxy} if proxy else {"http": "", "https": ""}
        except Exception:
            pass

    def _request_ext(self, method: str, url: str, *, retry_label: str = "", **kwargs: Any):
        last_exc: Exception | None = None
        for attempt in range(1, TRANSIENT_REQUEST_RETRY_LIMIT + 1):
            try:
                return getattr(self.ext, method.lower())(url, **kwargs)
            except Exception as exc:
                if not _looks_transient_request_error(exc) or attempt >= TRANSIENT_REQUEST_RETRY_LIMIT:
                    raise
                last_exc = exc
                sleep_s = random.uniform(TRANSIENT_REQUEST_RETRY_MIN_S, TRANSIENT_REQUEST_RETRY_MAX_S)
                label = retry_label or url.split("?", 1)[0]
                self.log(
                    f"[gopay] transient request error at {label}: {type(exc).__name__}: "
                    f"{str(exc)[:160]} - retry {attempt}/{TRANSIENT_REQUEST_RETRY_LIMIT - 1} "
                    f"after {sleep_s:.1f}s"
                )
                time.sleep(sleep_s)
        if last_exc:
            raise last_exc
        raise GoPayError(f"request retry exhausted: {method} {url}")

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
            r = self._request_ext(
                "get",
                f"https://api.stripe.com/v1/payment_pages/{cs_id}",
                params=params,
                timeout=DEFAULT_TIMEOUT,
                retry_label="stripe payment_pages get",
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
        r = self._request_ext(
            "get",
            pm_url,
            allow_redirects=False,
            timeout=DEFAULT_TIMEOUT,
            retry_label="stripe pm-redirect authorize",
        )
        if r.status_code not in (301, 302, 303, 307, 308):
            raise GoPayError(f"pm-redirects: expected redirect, got {r.status_code}")
        loc = r.headers.get("Location", "")
        m = re.search(r"app\.midtrans\.com/snap/v[14]/redirection/([a-f0-9-]{36})", loc)
        if not m:
            raise GoPayError(f"pm-redirects: no midtrans token in Location={loc!r}")
        return m.group(1)

    def _midtrans_load_transaction(self, snap_token: str):
        """Optional: load transaction page so any session cookies get set."""
        r = self._request_ext(
            "get",
            f"https://app.midtrans.com/snap/v1/transactions/{snap_token}",
            headers={
                "x-source": "snap",
                "x-source-app-type": "redirection",
                "x-source-version": "2.3.0",
            },
            timeout=DEFAULT_TIMEOUT,
            retry_label="midtrans load transaction",
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

    def _pre_unbind_existing_gopay_links(self) -> None:
        auto = self.gopay_cfg.get("auto_unbind") if isinstance(self.gopay_cfg.get("auto_unbind"), dict) else {}
        raw_request = str(auto.get("raw_request") or self.gopay_cfg.get("auto_unbind_raw_request") or "").strip()
        base_url = str(auto.get("base_url") or self.gopay_cfg.get("auto_unbind_base_url") or "")
        timeout = _float_cfg(auto or self.gopay_cfg, "timeout", DEFAULT_TIMEOUT)
        if not raw_request:
            self.log("[gopay] auto-unbind precheck skipped: raw_request not configured")
            return
        if _gopay_auto_unbind is None:
            self.log("[gopay] auto-unbind precheck skipped: gopay_auto_unbind unavailable")
            return

        self.log("[gopay] auto-unbind precheck: checking linked GoPay apps")
        try:
            linked = _gopay_auto_unbind.fetch_linkedapps(raw_request, base_url, timeout=timeout)
            entries = _gopay_auto_unbind.extract_gopay_link_entries(linked.get("body_json"))
        except Exception as exc:
            raise GoPayError(f"auto-unbind precheck failed: {type(exc).__name__}: {str(exc)[:300]}") from exc

        if not linked.get("has_data"):
            raise GoPayError(
                "auto-unbind precheck failed before linking: "
                f"linkedapps status={linked.get('status_code') or '-'} body={str(linked.get('body') or '')[:240]}"
            )
        if not entries:
            self.log("[gopay] auto-unbind precheck ok: no existing binding")
            return

        self.log(f"[gopay] auto-unbind precheck found {len(entries)} linked account(s), unlinking before linking")
        try:
            result = _gopay_auto_unbind.run_from_gopay_config(self.gopay_cfg, log=self.log)
        except Exception as exc:
            raise GoPayError(f"auto-unbind failed before linking: {type(exc).__name__}: {str(exc)[:300]}") from exc
        if result.get("ok"):
            self.log(
                "[gopay] auto-unbind precheck ok: existing binding removed "
                f"status={result.get('unlink_status_code') or '-'}"
            )
            return
        raise GoPayError(
            "auto-unbind failed before linking: "
            f"reason={result.get('reason') or '-'} linkedapps_status={result.get('linkedapps_status_code') or '-'} "
            f"unlink_status={result.get('unlink_status_code') or '-'}"
        )

    # ───── Step 7: Midtrans linking initiation ─────

    def _midtrans_init_linking(self, snap_token: str) -> str:
        """POST snap/v3/accounts/{snap}/linking. Retries on 406."""
        self._pre_unbind_existing_gopay_links()
        url = f"https://app.midtrans.com/snap/v3/accounts/{snap_token}/linking"
        self.midtrans_page_url = f"https://app.midtrans.com/snap/v4/redirection/{snap_token}"
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
        rate_limit_attempt = 0
        attempt = 0
        while attempt < LINK_RETRY_LIMIT + 1:
            try:
                r = self.ext.post(url, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
            except Exception as exc:
                if not _looks_transient_request_error(exc):
                    raise
                rate_limit_attempt += 1
                last_err = f"{type(exc).__name__}: {str(exc)[:160]}"
                if rate_limit_attempt > LINK_429_RETRY_LIMIT:
                    raise GoPayError(f"midtrans linking transient exhausted retries: {last_err}") from exc
                sleep_s = random.uniform(LINK_429_RETRY_SLEEP_MIN_S, LINK_429_RETRY_SLEEP_MAX_S)
                self.log(
                    "[gopay] midtrans linking transient error, "
                    f"{sleep_s:.1f}s 后重试 {rate_limit_attempt}/{LINK_429_RETRY_LIMIT}: {last_err}"
                )
                time.sleep(sleep_s)
                continue
            if r.status_code == 201:
                data = r.json()
                activation_link = data.get("activation_link_url", "")
                self.gopay_activation_link_url = activation_link
                m = re.search(r"reference=([a-f0-9-]{36})", activation_link)
                if not m:
                    raise GoPayError(f"midtrans linking 201 but no reference: {data}")
                ref = m.group(1)
                self.log(f"[gopay] midtrans linking ok reference={ref}")
                if activation_link:
                    self.log(f"[gopay] 当前网页地址: {activation_link}")
                return ref
            if r.status_code == 406:
                attempt += 1
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
            if r.status_code == 429:
                rate_limit_attempt += 1
                last_err = f"429 {r.text[:120]}"
                if rate_limit_attempt > LINK_429_RETRY_LIMIT:
                    raise GoPayError(f"midtrans linking 429 exhausted retries: {last_err}")
                sleep_s = random.uniform(LINK_429_RETRY_SLEEP_MIN_S, LINK_429_RETRY_SLEEP_MAX_S)
                time.sleep(sleep_s)
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
        body = {"payment_type": "gopay", "tokenization": "true", "promo_details": None}
        last_detail = ""
        for attempt in range(1, CHARGE_RETRY_LIMIT + 1):
            try:
                r = self.ext.post(
                    url,
                    json=body,
                    headers=headers, timeout=DEFAULT_TIMEOUT,
                )
            except Exception as exc:
                if not _looks_transient_request_error(exc) or attempt >= CHARGE_RETRY_LIMIT:
                    raise
                last_detail = f"{type(exc).__name__}: {str(exc)[:200]}"
                self.log(
                    "[gopay] midtrans charge transient error, "
                    f"{CHARGE_RETRY_SLEEP_S:.1f}s 后重试 {attempt}/{CHARGE_RETRY_LIMIT}: {last_detail}"
                )
                time.sleep(CHARGE_RETRY_SLEEP_S)
                continue

            raw_text = (r.text or "")[:1000]
            try:
                data = r.json()
            except Exception:
                data = {}
            headers_log = json.dumps(_safe_headers_for_log(r.headers), ensure_ascii=False, separators=(",", ":"))
            keys_log = ",".join(sorted(data.keys())) if isinstance(data, dict) else type(data).__name__
            link = data.get("gopay_verification_link_url", "") if isinstance(data, dict) else ""
            self.log(
                "[gopay] midtrans charge response "
                f"attempt={attempt}/{CHARGE_RETRY_LIMIT} status={r.status_code} "
                f"headers={headers_log} keys={keys_log} link={link!r} body={raw_text!r}"
            )
            r.raise_for_status()

            m = re.search(r"reference=([A-Za-z0-9_-]+)", link or "")
            if m:
                charge_ref = m.group(1)
                self.log(f"[gopay] midtrans charge ref={charge_ref}")
                return charge_ref

            last_detail = f"status={r.status_code} keys={keys_log} link={link!r} body={raw_text[:300]!r}"
            if attempt < CHARGE_RETRY_LIMIT:
                self.log(
                    "[gopay] midtrans charge no reference, "
                    f"{CHARGE_RETRY_SLEEP_S:.1f}s 后重试 {attempt}/{CHARGE_RETRY_LIMIT}"
                )
                time.sleep(CHARGE_RETRY_SLEEP_S)
                continue
        raise GoPayError(f"midtrans charge: no reference after retries: {last_detail}")

    def _extract_qr_payload(self, data: dict) -> tuple[str, str]:
        if not isinstance(data, dict):
            return "", ""

        qr_string_keys = ("qr_string", "qr_content", "qris_string", "qris", "qr_code")
        qr_image_keys = ("qris_url", "qr_code_url", "qr_image", "qr_image_url", "qr_image_base64")
        payment_url_keys = (
            "deeplink_redirect_url",
            "deeplink_url",
            "gopay_deeplink_url",
            "redirect_url",
            "payment_url",
            "gopay_web_url",
        )

        for obj in _iter_nested_dicts(data):
            for key in qr_string_keys + qr_image_keys:
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    value = value.strip()
                    if _looks_like_payment_return_url(value):
                        continue
                    if _looks_like_qris_payload(value):
                        return value, "qr_string"
                    if value.startswith("data:image/") or key in qr_image_keys or value.startswith(("http://", "https://")):
                        return value, "qr_image_url"
                    return value, "qr_string" if key in qr_string_keys else "qr_image_url"

            actions = obj.get("actions")
            if isinstance(actions, list):
                for action in actions:
                    if not isinstance(action, dict):
                        continue
                    name = str(action.get("name") or "").lower()
                    url = str(action.get("url") or "").strip()
                    if _looks_like_payment_return_url(url):
                        continue
                    if url and ("qr" in name or "qris" in url.lower() or "qr-code" in url.lower()):
                        return url, "qr_image_url"

        for obj in _iter_nested_dicts(data):
            for key in payment_url_keys:
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    value = value.strip()
                    if _looks_like_payment_return_url(value):
                        continue
                    return value, "payment_url"
        return "", ""

    def _midtrans_create_qr_charge(self, snap_token: str) -> dict:
        url = f"https://app.midtrans.com/snap/v2/transactions/{snap_token}/charge"
        headers = {
            **self._midtrans_basic_auth(),
            "Content-Type": "application/json",
            "Origin": "https://app.midtrans.com",
            "Referer": f"https://app.midtrans.com/snap/v4/redirection/{snap_token}?gopayMode=qr",
        }
        candidates = [
            ("qris", {"payment_type": "qris", "promo_details": None}),
            ("gopay-qr", {"payment_type": "gopay", "tokenization": "false", "promo_details": None}),
        ]
        last_detail = ""
        for label, body in candidates:
            for attempt in range(1, CHARGE_RETRY_LIMIT + 1):
                try:
                    r = self.ext.post(url, json=body, headers=headers, timeout=DEFAULT_TIMEOUT)
                except Exception as exc:
                    if not _looks_transient_request_error(exc) or attempt >= CHARGE_RETRY_LIMIT:
                        raise
                    last_detail = f"{label} {type(exc).__name__}: {str(exc)[:200]}"
                    self.log(
                        "[gopay-qr] midtrans charge transient error, "
                        f"{CHARGE_RETRY_SLEEP_S:.1f}s 后重试 {attempt}/{CHARGE_RETRY_LIMIT}: {last_detail}"
                    )
                    time.sleep(CHARGE_RETRY_SLEEP_S)
                    continue

                raw_text = (r.text or "")[:1000]
                try:
                    data = r.json()
                except Exception:
                    data = {}
                headers_log = json.dumps(_safe_headers_for_log(r.headers), ensure_ascii=False, separators=(",", ":"))
                keys_log = ",".join(sorted(data.keys())) if isinstance(data, dict) else type(data).__name__
                payload, kind = self._extract_qr_payload(data)
                status_code = str(data.get("status_code") or "") if isinstance(data, dict) else ""
                status_message = str(data.get("status_message") or "") if isinstance(data, dict) else ""
                transaction_status = str(data.get("transaction_status") or "") if isinstance(data, dict) else ""
                self.log(
                    "[gopay-qr] midtrans charge response "
                    f"mode={label} attempt={attempt}/{CHARGE_RETRY_LIMIT} "
                    f"status={r.status_code} headers={headers_log} keys={keys_log} "
                    f"midtrans_status={status_code or '-'} transaction_status={transaction_status or '-'} "
                    f"payload_type={kind or '-'} payload_preview={payload[:180]!r} "
                    f"message={status_message[:160]!r} body={raw_text!r}"
                )
                r.raise_for_status()

                if payload:
                    return {
                        "payload": payload,
                        "payload_type": kind,
                        "charge_mode": label,
                        "snap_token": snap_token,
                        "http_status": r.status_code,
                        "response_headers": _safe_headers_for_log(r.headers),
                        "response_keys": sorted(data.keys()) if isinstance(data, dict) else [],
                        "response": data,
                    }

                last_detail = f"mode={label} status={r.status_code} keys={keys_log} body={raw_text[:300]!r}"
                if attempt < CHARGE_RETRY_LIMIT:
                    self.log(
                        "[gopay-qr] midtrans charge no QR payload, "
                        f"{CHARGE_RETRY_SLEEP_S:.1f}s 后重试 {attempt}/{CHARGE_RETRY_LIMIT}"
                    )
                    time.sleep(CHARGE_RETRY_SLEEP_S)
                    continue
                break
        raise GoPayError(f"midtrans qr charge: no QR payload after retries: {last_detail}")

    def _save_qr_artifact(self, qr_info: dict) -> str:
        payload = str(qr_info.get("payload") or "").strip()
        kind = str(qr_info.get("payload_type") or "")
        snap_token = str(qr_info.get("snap_token") or uuid.uuid4())
        if not payload:
            return ""

        out_dir = Path(__file__).resolve().parents[1] / "output" / "gopay_qr"
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", snap_token)[:80]

        if kind != "qr_image_url":
            try:
                import qrcode  # type: ignore

                path = out_dir / f"{stem}.png"
                img = qrcode.make(payload)
                img.save(path)
                return str(path)
            except Exception as exc:
                self.log(f"[gopay-qr] qrcode 生成失败，改写入文本: {type(exc).__name__}: {str(exc)[:120]}")

        if kind == "qr_image_url":
            raw, suffix = _decode_data_image(payload)
            if raw:
                path = out_dir / f"{stem}{suffix}"
                path.write_bytes(raw)
                self.log(f"[gopay-qr] saved data-url QR image: {path} bytes={len(raw)}")
                return str(path)
            try:
                r = self.ext.get(payload, headers=self._midtrans_basic_auth(), timeout=DEFAULT_TIMEOUT)
                if 200 <= r.status_code < 300:
                    content_type = str(r.headers.get("content-type") or "").lower()
                    suffix = _data_image_suffix(content_type, r.content)
                    path = out_dir / f"{stem}{suffix}"
                    path.write_bytes(r.content)
                    self.log(
                        f"[gopay-qr] downloaded QR image: {path} "
                        f"status={r.status_code} content_type={content_type or '-'} bytes={len(r.content)}"
                    )
                    return str(path)
                self.log(f"[gopay-qr] 下载二维码图片失败 status={r.status_code}: {payload[:160]}")
            except Exception as exc:
                self.log(f"[gopay-qr] 下载二维码图片异常: {type(exc).__name__}: {str(exc)[:160]}")

        path = out_dir / f"{stem}.txt"
        path.write_text(payload, encoding="utf-8")
        return str(path)

    # ───── Step 14: GoPay charge processing ─────

    def _decode_qr_artifact(self, artifact: str) -> str:
        if not artifact:
            return ""
        path = Path(artifact)
        if not path.exists():
            self.log(f"[gopay-qris] QR artifact missing: {artifact}")
            return ""
        if path.suffix.lower() == ".txt":
            return path.read_text(encoding="utf-8", errors="replace").strip()

        try:
            stat = path.stat()
            self.log(f"[gopay-qris] decoding QR artifact path={path} suffix={path.suffix.lower() or '-'} bytes={stat.st_size}")
        except Exception:
            pass

        if path.suffix.lower() == ".svg":
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in (
                r">((?:000201)[^<\s]{8,})<",
                r"(?:data|qris|qr[_-]?string)[\"'\s:=]+((?:000201)[A-Za-z0-9]+)",
            ):
                m = re.search(pattern, text, re.I)
                if m:
                    return m.group(1).strip()

        decoders = []
        unavailable: list[str] = []
        try:
            import cv2  # type: ignore
            decoders.append(("opencv", lambda: self._decode_qr_with_opencv(cv2, path)))
        except Exception as exc:
            unavailable.append(f"opencv={type(exc).__name__}: {str(exc)[:120]}")
        try:
            from pyzbar.pyzbar import decode as pyzbar_decode  # type: ignore
            from PIL import Image  # type: ignore
            decoders.append(("pyzbar", lambda: [
                item.data.decode("utf-8", "replace")
                for item in pyzbar_decode(Image.open(path))
                if getattr(item, "data", None)
            ]))
        except Exception as exc:
            unavailable.append(f"pyzbar={type(exc).__name__}: {str(exc)[:120]}")
        for name, decoder in decoders:
            try:
                for value in decoder():
                    value = str(value or "").strip()
                    if value:
                        self.log(f"[gopay-qris] QR image decoded via {name}")
                        return value
            except Exception as exc:
                self.log(f"[gopay-qris] {name} decode failed: {type(exc).__name__}: {str(exc)[:120]}")
        if unavailable:
            self.log(f"[gopay-qris] unavailable QR decoders: {'; '.join(unavailable)}")
        self.log(f"[gopay-qris] QR image decode produced no data; artifact={path}")
        return ""

    def _decode_qr_with_opencv(self, cv2: Any, path: Path) -> list[str]:
        img = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if img is None:
            self.log(f"[gopay-qris] opencv could not read image: {path}")
            return []

        detector = cv2.QRCodeDetector()
        candidates = []
        try:
            h, w = img.shape[:2]
            channels = int(img.shape[2]) if len(img.shape) > 2 else 1
            self.log(f"[gopay-qris] opencv image shape={w}x{h} channels={channels}")

            if channels == 4:
                alpha = img[:, :, 3]
                bgr = img[:, :, :3]
                white = 255 * (1 - (alpha[:, :, None] / 255.0))
                img = (bgr * (alpha[:, :, None] / 255.0) + white).astype("uint8")
            elif channels == 1:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            variants = [img, gray]
            otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
            variants.extend([
                otsu,
                255 - gray,
                255 - otsu,
                cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 2),
            ])

            for variant in variants:
                for border in (0, 16, 32, 64):
                    framed = (
                        cv2.copyMakeBorder(variant, border, border, border, border, cv2.BORDER_CONSTANT, value=255)
                        if border else variant
                    )
                    candidates.append(framed)
                    vh, vw = framed.shape[:2]
                    for target in (480, 720, 1024, 1440):
                        if min(vh, vw) < target:
                            scale = target / max(1, min(vh, vw))
                            candidates.append(cv2.resize(framed, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST))
                            candidates.append(cv2.resize(framed, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC))
        except Exception:
            candidates.append(img)

        values: list[str] = []
        for candidate in candidates:
            try:
                value = detector.detectAndDecode(candidate)[0]
                if value:
                    values.append(value)
            except Exception:
                continue
            try:
                ok, decoded, _points, _straight = detector.detectAndDecodeMulti(candidate)
                if ok:
                    values.extend([v for v in decoded if v])
            except Exception:
                continue
        return values

    def _qris_string_from_info(self, qr_info: dict, artifact: str) -> str:
        payload = str(qr_info.get("payload") or "").strip()
        kind = str(qr_info.get("payload_type") or "")
        if _looks_like_qris_payload(payload):
            return payload
        if kind == "qr_image_url" or payload.startswith(("http://", "https://")):
            decoded = self._decode_qr_artifact(artifact)
            if decoded:
                return decoded
        response = qr_info.get("response") if isinstance(qr_info.get("response"), dict) else {}
        qr_field_details: list[str] = []
        for obj in _iter_nested_dicts(response):
            for key in ("qr_string", "qris", "qris_string", "qr_content", "qr_code"):
                raw_value = obj.get(key)
                value = str(raw_value or "").strip()
                if value:
                    return value
                if key in obj:
                    qr_field_details.append(f"{key}:{type(raw_value).__name__}:len={len(str(raw_value or ''))}")
        keys = qr_info.get("response_keys") or (sorted(response.keys()) if isinstance(response, dict) else [])
        raise GoPayError(
            "QRIS payload not found: unable to decode QR to string "
            f"(payload_type={kind or '-'} artifact={artifact or '-'} response_keys={','.join(keys) if keys else '-'}; "
            f"qr_fields={','.join(qr_field_details) if qr_field_details else '-'}; "
            "install a QR decoder in the same Python env: pip install opencv-python-headless)"
        )

    def _qris_cfg(self) -> dict:
        cfg = self.gopay_cfg.get("qris") if isinstance(self.gopay_cfg.get("qris"), dict) else {}
        return dict(cfg or {})

    def _qris_base_headers(self) -> dict[str, str]:
        qris_cfg = self._qris_cfg()
        prefer_auto_unbind = _truthy_cfg(
            qris_cfg.get("prefer_auto_unbind_headers")
            or self.gopay_cfg.get("qris_prefer_auto_unbind_headers")
            or self.gopay_cfg.get("prefer_auto_unbind_headers")
            or True
        )
        auto_sources = _auto_unbind_header_sources(self.gopay_cfg)
        explicit_sources = (
            self.gopay_cfg.get("headers"),
            self.gopay_cfg.get("qris_headers"),
            self.gopay_cfg.get("qris_raw_headers"),
            qris_cfg.get("headers"),
            qris_cfg.get("raw_headers"),
        )
        ordered_sources = (
            (_default_qris_headers(self.gopay_cfg), *explicit_sources, *auto_sources)
            if prefer_auto_unbind
            else (_default_qris_headers(self.gopay_cfg), *auto_sources, *explicit_sources)
        )
        headers = _merge_header_sources(
            *ordered_sources,
        )
        headers = {k: v for k, v in headers.items() if str(v or "").strip()}
        missing = [
            name for name in (
                "authorization", "x-m1", "x-uniqueid", "x-phonemake",
                "x-phonemodel", "x-deviceos", "x-appversion", "x-appid",
                "x-apptype", "x-platform",
            )
            if not any(k.lower() == name for k in headers)
        ]
        if missing:
            raise GoPayError(
                "gopay qris headers missing required fields for x-e1 signing: "
                + ", ".join(missing)
                + "; paste GoPay APP headers in the existing auto-unbind raw request/header field"
            )
        return headers

    def _qris_url(self, path: str) -> str:
        qris_cfg = self._qris_cfg()
        base = str(
            qris_cfg.get("base_url")
            or self.gopay_cfg.get("qris_base_url")
            or self.gopay_cfg.get("customer_base_url")
            or GOPAY_CUSTOMER_BASE_URL
        ).strip()
        if "://" not in base:
            base = f"https://{base}"
        return urljoin(base.rstrip("/") + "/", path.lstrip("/"))

    def _qris_body_for_signature(self, method: str, path: str, body: dict | None, body_text: str) -> tuple[str, str]:
        if body_text and method.upper() in ("POST", "PUT", "PATCH"):
            return "", "empty+body-md5"
        return body_text, "body"

    def _qris_debug_logs(self) -> bool:
        qris_cfg = self._qris_cfg()
        return _truthy_cfg(
            qris_cfg.get("debug_logs")
            or self.gopay_cfg.get("qris_debug_logs")
            or self.gopay_cfg.get("debug_qris")
        )

    def _qris_signed_headers(
        self,
        method: str,
        url: str,
        body_text: str,
        body_for_signature: str | None = None,
    ) -> dict[str, str]:
        if _signed_gopay_headers is None:
            raise GoPayError("gopay qris signer unavailable: webui.backend.gopay_signer import failed")
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        qris_cfg = self._qris_cfg()
        key = str(qris_cfg.get("hmac_key") or self.gopay_cfg.get("qris_hmac_key") or "")
        nonce_marker = str(
            qris_cfg.get("nonce_marker")
            or self.gopay_cfg.get("qris_nonce_marker")
            or ""
        ).strip()
        kwargs = {"method": method.upper(), "host": parsed.netloc, "path": path, "body": body_text}
        if body_for_signature is not None:
            kwargs["body_for_signature"] = body_for_signature
            kwargs["body_text"] = body_text
        if key:
            kwargs["key"] = key
        if nonce_marker:
            kwargs["nonce_marker_hex"] = nonce_marker
        return _signed_gopay_headers(self._qris_base_headers(), **kwargs)

    def _qris_request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        allow_pin_challenge: bool = False,
    ) -> dict:
        url = self._qris_url(path)
        body_text = _json_dumps_compact(body) if body is not None else ""
        parsed = urlsplit(url)
        sign_path = parsed.path or "/"
        if parsed.query:
            sign_path = f"{sign_path}?{parsed.query}"
        body_for_signature, body_signature_source = self._qris_body_for_signature(method, sign_path, body, body_text)
        headers = self._qris_signed_headers(method, url, body_text, body_for_signature=body_for_signature)
        debug_logs = self._qris_debug_logs()
        if debug_logs:
            self.log("[gopay-qris] === request start ===")
            self.log(f"[gopay-qris] method={method.upper()} url={url}")
            self.log(f"[gopay-qris] headers={_json_for_log(headers)}")
            self.log(
                "[gopay-qris] x-e1 body_signature_source="
                f"{body_signature_source} "
                f"signature_len={len(body_for_signature.encode('utf-8'))}"
            )
            xe1 = _header_value(headers, "x-e1")
            try:
                nonce = xe1.split(":", 2)[1] if xe1 else ""
                marker = nonce[96:128] if len(nonce) == 160 else ""
                zero_segment = len(nonce) == 160 and nonce[64:96] == ("0" * 32)
                self.log(
                    "[gopay-qris] x-e1 nonce_shape="
                    f"zero={zero_segment} "
                    f"marker={marker or '-'}"
                )
            except Exception:
                pass
            self.log(f"[gopay-qris] body={body_text if body_text else ''}")
            self.log("[gopay-qris] === request end ===")
        r = self._request_ext(
            method,
            url,
            headers=headers,
            data=body_text.encode("utf-8") if body is not None else None,
            timeout=DEFAULT_TIMEOUT,
            retry_label=f"gopay qris {path}",
        )
        raw_text = (r.text or "")[:1200]
        try:
            data = r.json()
        except Exception:
            data = {}
        response_keys = ",".join(sorted(data.keys())) if isinstance(data, dict) else type(data).__name__
        is_allowed_pin_challenge = (
            allow_pin_challenge
            and r.status_code >= 400
            and self._qris_has_pin_challenge(data)
        )
        if debug_logs or (r.status_code >= 400 and not is_allowed_pin_challenge):
            self.log(
                f"[gopay-qris] response {method.upper()} {urlsplit(url).path} "
                f"status={r.status_code} keys={response_keys} "
                f"body={raw_text[:500]!r}"
            )
        else:
            self.log(f"[gopay-qris] {method.upper()} {urlsplit(url).path} -> {r.status_code}")
        if is_allowed_pin_challenge:
            self.log(f"[gopay-qris] response {method.upper()} {urlsplit(url).path} returned PIN challenge status={r.status_code}")
            return data
        if r.status_code >= 400:
            detail = ""
            if isinstance(data, dict):
                errors = data.get("errors") if isinstance(data.get("errors"), list) else []
                first = errors[0] if errors and isinstance(errors[0], dict) else {}
                err = data.get("error") if isinstance(data.get("error"), dict) else {}
                detail = (
                    f" code={_qris_error_code(data) or '-'}"
                    f" message={err.get('description') or first.get('message') or '-'}"
                )
            raise GoPayError(f"qris {method.upper()} {path} {r.status_code}{detail}: {raw_text[:600]}")
        return data if isinstance(data, dict) else {}

    def _qris_has_pin_challenge(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        value = ((((data.get("data") or {}).get("challenge") or {}).get("action") or {}).get("value") or {})
        action_type = (((data.get("data") or {}).get("challenge") or {}).get("action") or {}).get("type")
        return (
            action_type == "GOPAY_PIN_CHALLENGE"
            and isinstance(value, dict)
            and bool(value.get("challenge_id"))
            and bool(value.get("client_id"))
        )

    def _qris_error_summary(self, data: Any) -> str:
        if not isinstance(data, dict):
            return type(data).__name__
        errors = data.get("errors") if isinstance(data.get("errors"), list) else []
        first = errors[0] if errors and isinstance(errors[0], dict) else {}
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        return (
            f"success={data.get('success')!r}"
            f" code={_qris_error_code(data) or '-'}"
            f" message={err.get('description') or first.get('message') or '-'}"
        )

    def _qris_capture_succeeded(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        if self._qris_has_pin_challenge(data):
            return False
        if data.get("success") is True:
            return True
        if data.get("success") is False:
            return False
        errors = data.get("errors") if isinstance(data.get("errors"), list) else []
        if errors:
            return False
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        status = str(
            payload.get("status")
            or payload.get("state")
            or payload.get("transaction_status")
            or payload.get("payment_status")
            or ""
        ).strip().lower()
        return status in ("success", "succeeded", "capture", "captured", "completed", "paid")

    def _qris_pin_token(self, challenge_id: str, client_id: str, payment_option_token: str = "") -> str:
        if payment_option_token:
            self._qris_mark_last_used(payment_option_token)
        self._qris_request("GET", f"/api/v2/challenges/{challenge_id}/pin-page")
        pin_resp = self._qris_request("POST", "/api/v1/users/pin/tokens", {
            "pin": self.pin,
            "client_id": client_id,
            "challenge_id": challenge_id,
        })
        pin_data = pin_resp.get("data") if isinstance(pin_resp.get("data"), dict) else {}
        pin_token = str(pin_data.get("token") or pin_resp.get("token") or "")
        if not pin_token:
            raise GoPayError(f"qris pin token missing: {json.dumps(pin_resp, ensure_ascii=False)[:500]}")
        return pin_token

    def _qris_mark_last_used(self, payment_option_token: str) -> dict:
        token = str(payment_option_token or "").strip()
        if not token:
            return {"ok": False, "reason": "missing_token"}
        try:
            resp = self._qris_request(
                "PUT",
                "/v1/customer/payment-options/settings/last-used",
                {"token": token},
            )
            self.log("[gopay-qris] marked wallet payment option as last-used")
            return {"ok": True, "response": resp}
        except Exception as exc:
            self.log(f"[gopay-qris] mark last-used failed: {type(exc).__name__}: {str(exc)[:160]}")
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def _qris_capture_with_pin_loop(self, payment_id: str, capture_body: dict) -> dict:
        max_attempts = max(1, int(self.gopay_cfg.get("qris_capture_pin_attempts") or 3))
        current_body = dict(capture_body)
        instructions = current_body.get("payment_instructions") if isinstance(current_body.get("payment_instructions"), list) else []
        first_instruction = instructions[0] if instructions and isinstance(instructions[0], dict) else {}
        payment_option_token = str(first_instruction.get("token") or "")
        last_resp: dict = {}
        for attempt in range(1, max_attempts + 1):
            resp = self._qris_request(
                "PATCH",
                f"/v3/payments/{payment_id}/capture",
                current_body,
                allow_pin_challenge=True,
            )
            last_resp = resp
            if self._qris_capture_succeeded(resp):
                self.log(f"[gopay-qris] capture succeeded attempt={attempt}/{max_attempts}")
                return resp
            if not self._qris_has_pin_challenge(resp):
                raise GoPayError(
                    "qris capture did not succeed: "
                    + self._qris_error_summary(resp)
                    + f" body={json.dumps(resp, ensure_ascii=False)[:600]}"
                )

            capture_data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            challenge = (((capture_data.get("challenge") or {}).get("action") or {}).get("value") or {})
            challenge_id = str(challenge.get("challenge_id") or "")
            challenge_client_id = str(challenge.get("client_id") or "")
            self.log(f"[gopay-qris] capture requires PIN attempt={attempt}/{max_attempts} challenge_id={challenge_id[:8]}...")
            pin_token = self._qris_pin_token(challenge_id, challenge_client_id, payment_option_token)
            current_body = dict(current_body)
            current_body["challenge"] = {
                "action": None,
                "value": {"pin_token": pin_token},
                "type": "GOPAY_PIN_CHALLENGE",
                "metadata": {
                    "challenge_id": challenge_id,
                    "client_id": challenge_client_id,
                },
            }

        raise GoPayError(
            "qris capture PIN attempts exhausted: "
            + self._qris_error_summary(last_resp)
            + f" body={json.dumps(last_resp, ensure_ascii=False)[:600]}"
        )

    def _follow_qris_finish_redirect(self, qr_info: dict) -> dict:
        response = qr_info.get("response") if isinstance(qr_info.get("response"), dict) else {}
        finish_url = str(
            response.get("finish_200_redirect_url")
            or response.get("finish_redirect_url")
            or ""
        ).strip()
        if not finish_url:
            self.log("[gopay-qris] finish redirect missing; skip")
            return {"ok": False, "reason": "missing"}
        try:
            r = self._request_ext(
                "get",
                finish_url,
                allow_redirects=True,
                timeout=DEFAULT_TIMEOUT,
                retry_label="gopay qris finish redirect",
            )
            final_url = str(getattr(r, "url", finish_url))
            return_result = self._visit_qris_embedded_return_url(final_url)
            self.log(
                "[gopay-qris] finish redirect visited "
                f"status={getattr(r, 'status_code', '?')} "
                f"url={final_url[:160]}"
                + (
                    f" return_status={return_result.get('status_code')}"
                    if return_result.get("attempted")
                    else ""
                )
            )
            redirect_failed = "redirect_status=failed" in final_url.lower()
            return {
                "ok": (
                    200 <= int(getattr(r, "status_code", 0) or 0) < 400
                    and not redirect_failed
                ) or bool(return_result.get("ok")),
                "status_code": getattr(r, "status_code", None),
                "url": final_url,
                "redirect_failed": redirect_failed,
                "return_url": return_result,
            }
        except Exception as exc:
            self.log(f"[gopay-qris] finish redirect failed: {type(exc).__name__}: {str(exc)[:160]}")
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def _visit_qris_embedded_return_url(self, url: str) -> dict:
        return_url = self._extract_qris_embedded_return_url(url)
        if not return_url:
            return {"attempted": False, "reason": "missing"}
        try:
            host = urlsplit(return_url).netloc.lower()
            if host.endswith(("pay.openai.com", "chatgpt.com")):
                r = self.cs.get(
                    return_url,
                    allow_redirects=True,
                    timeout=DEFAULT_TIMEOUT,
                    headers={"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                )
            else:
                r = self._request_ext(
                    "get",
                    return_url,
                    allow_redirects=True,
                    timeout=DEFAULT_TIMEOUT,
                    retry_label="gopay qris embedded return",
                )
            final_url = str(getattr(r, "url", return_url))
            status_code = int(getattr(r, "status_code", 0) or 0)
            return {
                "attempted": True,
                "ok": 200 <= status_code < 400,
                "status_code": status_code,
                "url": final_url,
            }
        except Exception as exc:
            self.log(f"[gopay-qris] embedded return failed: {type(exc).__name__}: {str(exc)[:160]}")
            return {"attempted": True, "ok": False, "url": return_url, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def _extract_qris_embedded_return_url(self, url: str) -> str:
        parsed = urlsplit(str(url or ""))
        query = parse_qs(parsed.query, keep_blank_values=True)
        fragment_query = parse_qs(parsed.fragment, keep_blank_values=True)
        outer_query = {**query}
        for key, value in fragment_query.items():
            outer_query.setdefault(key, value)
        candidates = []
        candidates.extend(query.get("return_url") or [])
        candidates.extend(query.get("redirect") or [])
        candidates.extend(query.get("redirect_url") or [])
        for candidate in candidates:
            candidate = str(candidate or "").strip()
            if candidate.startswith(("https://pay.openai.com/", "https://chatgpt.com/checkout/verify")):
                return self._merge_qris_return_query(candidate, outer_query)
        return ""

    def _merge_qris_return_query(self, return_url: str, outer_query: dict) -> str:
        parsed = urlsplit(return_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        for key in (
            "redirect_status",
            "returned_from_redirect",
            "setup_intent",
            "setup_intent_client_secret",
            "payment_intent",
            "payment_intent_client_secret",
        ):
            if key not in query and outer_query.get(key):
                query[key] = outer_query[key]
        return parsed._replace(query=urlencode(query, doseq=True)).geturl()

    def _qris_capture_terminal_status(self, capture_resp: dict) -> dict:
        payload = capture_resp.get("data") if isinstance(capture_resp.get("data"), dict) else {}
        status = str(
            payload.get("status")
            or payload.get("state")
            or payload.get("transaction_status")
            or payload.get("payment_status")
            or ""
        ).strip().lower()
        ok = self._qris_capture_succeeded(capture_resp)
        return {"ok": ok, "status": status or ("success" if ok else "unknown"), "body": capture_resp}

    def _wait_qris_midtrans_terminal(self, qr_info: dict, capture_resp: dict | None = None) -> dict:
        capture_status = self._qris_capture_terminal_status(capture_resp or {})
        if capture_status.get("ok"):
            return {"ok": True, "status": capture_status.get("status") or "paid", "source": "gopay_capture", "body": capture_resp or {}}
        response = qr_info.get("response") if isinstance(qr_info.get("response"), dict) else {}
        tx_id = str(response.get("transaction_id") or "").strip()
        order_id = str(response.get("order_id") or "").strip()
        candidates = [
            f"https://api.midtrans.com/v2/{tx_id}/status" if tx_id else "",
            f"https://api.midtrans.com/v2/{order_id}/status" if order_id else "",
        ]
        terminal = {"settlement", "capture", "settled", "success"}
        for attempt in range(1, 7):
            for url in [u for u in candidates if u]:
                try:
                    r = self._request_ext(
                        "get",
                        url,
                        headers=self._midtrans_basic_auth(),
                        timeout=DEFAULT_TIMEOUT,
                        retry_label="midtrans qris status",
                    )
                    data = r.json() if r.text else {}
                    status = str((data or {}).get("transaction_status") or (data or {}).get("status") or "").lower()
                    self.log(f"[gopay-qris] midtrans status attempt={attempt} http={r.status_code} transaction_status={status or '-'}")
                    if r.status_code == 200 and status in terminal:
                        return {"ok": True, "status": status, "source": "midtrans_status", "body": data}
                    if r.status_code in (401, 403):
                        self.log("[gopay-qris] midtrans status unauthorized; using GoPay capture result instead")
                        return {"ok": False, "status": "unauthorized", "source": "midtrans_status"}
                except Exception as exc:
                    self.log(f"[gopay-qris] midtrans status check failed: {type(exc).__name__}: {str(exc)[:120]}")
            time.sleep(2)
        return {"ok": False, "status": "unknown", "source": "midtrans_status"}

    def _qris_explore(self, qris: str) -> dict:
        retry_limit = max(1, int(self.gopay_cfg.get("qris_explore_retries") or QRIS_EXPLORE_RETRY_LIMIT))
        last: Exception | None = None
        for attempt in range(1, retry_limit + 1):
            try:
                return self._qris_request("POST", "/v1/explore", {"type": "QR_CODE", "data": qris})
            except GoPayError as exc:
                last = exc
                text = str(exc)
                retryable = (
                    "qris POST /v1/explore 400" in text
                    and ("code=1000" in text or "GoPay-1000" in text or "Internal server error" in text)
                )
                if not retryable or attempt >= retry_limit:
                    raise
                sleep_s = max(0.2, float(self.gopay_cfg.get("qris_explore_retry_sleep_s") or QRIS_EXPLORE_RETRY_SLEEP_S))
                self.log(f"[gopay-qris] /v1/explore GoPay-1000, retry {attempt}/{retry_limit} after {sleep_s:.1f}s")
                time.sleep(sleep_s)
        if last:
            raise last
        raise GoPayError("qris explore retry exhausted")

    def _qris_capture_additional_data(self, explore: dict, amount_value: int | float | str, amount_currency: str) -> dict:
        additional = dict(explore.get("additional_data") or {})
        v2 = additional.get("aspiqr_information_v2")
        if not isinstance(v2, dict):
            v2 = {}
            additional["aspiqr_information_v2"] = v2
        if not isinstance(v2.get("transaction_details"), dict):
            v2["transaction_details"] = {"amount": {"value": amount_value, "currency_code": amount_currency}}
        return additional

    def _run_qris_app_payment(self, qris: str, qr_info: dict) -> dict:
        qris = str(qris or "").strip()
        if not qris:
            raise GoPayError("qris string is empty")
        self.log(f"[gopay-qris] start GoPay APP QRIS payment data_len={len(qris)}")

        explore_resp = self._qris_explore(qris)
        explore = explore_resp.get("data") if isinstance(explore_resp.get("data"), dict) else {}
        payment_id = str(explore.get("payment_id") or _extract_payment_id_from_qris(qris) or "").strip()
        amount = explore.get("amount") if isinstance(explore.get("amount"), dict) else {}
        amount_value = amount.get("value", 1)
        amount_currency = str(amount.get("currency") or "IDR")
        merchant_info = (explore.get("additional_data") or {}).get("aspiqr_information") or {}
        merchant_id = str(
            merchant_info.get("merchant_id")
            or self._qris_cfg().get("merchant_id")
            or self.gopay_cfg.get("qris_merchant_id")
            or "G761482587"
        )
        if not payment_id:
            raise GoPayError(f"qris explore missing payment_id: {json.dumps(explore_resp, ensure_ascii=False)[:500]}")
        self.log(f"[gopay-qris] explore ok payment_id={payment_id} amount={amount_value} {amount_currency}")

        payment_resp = self._qris_request("POST", "/v1/qris/payments", {
            "qr_code": qris,
            "amount": {"value": amount_value, "currency": amount_currency},
            "channel_type": explore.get("channel_type", "DYNAMIC_QR"),
            "additional_data": explore.get("additional_data", {}),
            "metadata": explore.get("metadata", {}),
        })
        payment = payment_resp.get("data") if isinstance(payment_resp.get("data"), dict) else {}
        payment_id = str(payment.get("payment_id") or payment_id)
        checksum = payment.get("checksum") or explore.get("checksum") or {}

        service_id = str(self._qris_cfg().get("service_id") or self.gopay_cfg.get("qris_service_id") or "1001")
        checkout_resp = self._qris_request("POST", "/v2/customer/payment-options/checkout/list", {
            "intent": "EWALLET_QR",
            "order_pricing": {
                "payment_method_specific_pricing": [],
                "default_amount": {"amount": amount_value},
            },
            "selected_options_tokens": [],
            "merchant_id": merchant_id,
            "frontend_overrides": {
                "offline_methods": [],
                "payment_method_rollout": [],
                "exclude_paylater": False,
            },
            "service_id": service_id,
            "metadata": {"service_type": "QRIS", "service_id": service_id, "merchant_id": merchant_id},
        })
        checkout_data = checkout_resp.get("data") if isinstance(checkout_resp.get("data"), dict) else {}
        selected = checkout_data.get("selected_options") if isinstance(checkout_data.get("selected_options"), list) else []
        payment_option_token = str((selected[0] or {}).get("token") or "") if selected else ""
        if not payment_option_token:
            raise GoPayError(f"qris checkout/list missing selected option token: {json.dumps(checkout_resp, ensure_ascii=False)[:500]}")
        self.log("[gopay-qris] checkout/list ok selected option token received")

        instructions = [{
            "token": payment_option_token,
            "amount": {"value": amount_value, "currency": amount_currency},
        }]
        self._qris_request("POST", "/v1/promotions/evaluate", {
            "order_id": payment_id,
            "payment_instructions": instructions,
            "transaction_type": "MERCHANT_TRANSACTION",
        })

        capture_body = {
            "payment_instructions": [{**instructions[0], "admin_fee_token": None}],
            "applied_promo_code": ["NO_PROMO_APPLIED"],
            "description": None,
            "payment_method": None,
            "channel_type": "ONLINE_GATEWAY",
            "additional_data": self._qris_capture_additional_data(explore, amount_value, amount_currency),
            "challenge": None,
            "metadata": payment.get("metadata") or explore.get("metadata", {}),
            "checksum": checksum,
            "order_signature": explore.get("order_signature", {}),
        }
        final_resp = self._qris_capture_with_pin_loop(payment_id, capture_body)
        self.log(f"[gopay-qris] final capture ok payment_id={payment_id}")
        finish_redirect = self._follow_qris_finish_redirect(qr_info)
        midtrans_status = self._wait_qris_midtrans_terminal(qr_info, final_resp)
        return {
            "payment_id": payment_id,
            "merchant_id": merchant_id,
            "amount": amount_value,
            "currency": amount_currency,
            "capture": final_resp,
            "finish_redirect": finish_redirect,
            "midtrans_status": midtrans_status,
            "qr_charge": qr_info,
        }

    def _gopay_payment_validate(self, charge_ref: str):
        # midtrans 创建 charge 后 GoPay 后端要数秒才能 fetch；轮询直到 ready
        for i in range(8):
            try:
                r = self._request_ext(
                    "get",
                    f"https://gwa.gopayapi.com/v1/payment/validate?reference_id={charge_ref}",
                    headers={"Origin": "https://merchants-gws-app.gopayapi.com",
                             "Referer": "https://merchants-gws-app.gopayapi.com/"},
                    timeout=DEFAULT_TIMEOUT,
                    retry_label="gopay payment validate",
                )
            except Exception as exc:
                if not _looks_transient_request_error(exc):
                    raise
                self.log(f"[gopay] payment/validate transient error after retries: {type(exc).__name__}: {str(exc)[:160]}")
                time.sleep(1.5)
                continue
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

    def _chatgpt_verify(self, cs_id: str, timeout_s: float = 60.0, qris_info: dict | None = None) -> dict:
        """Poll chatgpt verify until plan is active."""
        deadline = time.time() + max(1.0, float(timeout_s or 60.0))
        saw_http_200 = False
        last_accounts_check: dict = {}
        return_retry_interval = max(5.0, float(self.gopay_cfg.get("qris_finish_redirect_retry_s") or 8.0))
        next_return_retry = 0.0
        while time.time() < deadline:
            if qris_info and time.time() >= next_return_retry:
                self._follow_qris_finish_redirect(qris_info)
                next_return_retry = time.time() + return_retry_interval
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
                saw_http_200 = True
                try:
                    data = r.json()
                except Exception:
                    data = {}
                state = str(data.get("state") or data.get("status") or "").lower() if isinstance(data, dict) else ""
                if state in ("verified", "succeeded", "success", "active"):
                    self.log("[gopay] chatgpt verify ok")
                    return {"state": "succeeded", "cs_id": cs_id}
                if not data and "checkout/verify" in str(getattr(r, "url", "")):
                    self.log("[gopay] chatgpt verify returned 200 html/page, waiting for terminal state")
                elif isinstance(data, dict):
                    self.log(f"[gopay] chatgpt verify pending state={state or '-'}")
            last_accounts_check = self._chatgpt_accounts_check()
            if str(last_accounts_check.get("active_hint") or "") == "active_subscription":
                self.log("[gopay] accounts/check indicates active plan")
                return {
                    "state": "succeeded",
                    "cs_id": cs_id,
                    "verify_http_200_seen": saw_http_200,
                    "accounts_check": last_accounts_check,
                }
            time.sleep(2)
        return {
            "state": "verify_timeout",
            "cs_id": cs_id,
            "verify_http_200_seen": saw_http_200,
            "accounts_check": last_accounts_check,
        }

    def _chatgpt_accounts_check(self) -> dict:
        try:
            offset_min = -int(round((time.timezone if time.localtime().tm_isdst == 0 else time.altzone) / 60))
        except Exception:
            offset_min = 0
        try:
            r = self.cs.get(
                "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                params={"timezone_offset_min": offset_min},
                headers={"accept": "application/json", "origin": "https://chatgpt.com"},
                timeout=DEFAULT_TIMEOUT,
            )
            data = r.json() if r.text else {}
            active_hint = ""
            active_plan = ""
            if isinstance(data, dict):
                for account_info in self._iter_chatgpt_account_infos(data):
                    account = account_info.get("account") if isinstance(account_info.get("account"), dict) else {}
                    entitlement = account_info.get("entitlement") if isinstance(account_info.get("entitlement"), dict) else {}
                    plan_type = str(account.get("plan_type") or "").strip().lower()
                    subscription_plan = str(entitlement.get("subscription_plan") or "").strip().lower()
                    has_active_subscription = entitlement.get("has_active_subscription") is True
                    if has_active_subscription or (
                        plan_type
                        and plan_type not in ("free", "chatgptfreeplan")
                    ) or (
                        subscription_plan
                        and subscription_plan not in ("free", "chatgptfreeplan")
                        and "free" not in subscription_plan
                    ):
                        active_hint = "active_subscription"
                        active_plan = subscription_plan or plan_type
                        break
            self.log(
                "[gopay] accounts/check "
                f"status={r.status_code} active_hint={active_hint or '-'} "
                f"plan={active_plan or '-'}"
            )
            return {
                "status_code": r.status_code,
                "active_hint": active_hint,
                "active_plan": active_plan,
                "body_keys": sorted(data.keys()) if isinstance(data, dict) else [],
                "body": data if isinstance(data, dict) else {},
            }
        except Exception as exc:
            self.log(f"[gopay] accounts/check failed: {type(exc).__name__}: {str(exc)[:160]}")
            return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def _iter_chatgpt_account_infos(self, data: dict) -> list[dict]:
        accounts = data.get("accounts") if isinstance(data.get("accounts"), dict) else {}
        result: list[dict] = []
        for value in accounts.values():
            if isinstance(value, dict):
                result.append(value)
        default = data.get("default") if isinstance(data.get("default"), dict) else {}
        if default:
            result.append(default)
        return result

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
        if self.qr_payment:
            return self._run_midtrans_qr(snap_token, cs_id)
        return self._run_midtrans_and_gopay(snap_token, cs_id)

    def _run_midtrans_qr(self, snap_token: str, cs_id: str) -> dict:
        self._midtrans_load_transaction(snap_token)
        qr_info = self._midtrans_create_qr_charge(snap_token)
        artifact = self._save_qr_artifact(qr_info)
        payload = str(qr_info.get("payload") or "")
        kind = str(qr_info.get("payload_type") or "")
        charge_mode = str(qr_info.get("charge_mode") or "")
        response = qr_info.get("response") if isinstance(qr_info.get("response"), dict) else {}
        response_keys = qr_info.get("response_keys") or (sorted(response.keys()) if isinstance(response, dict) else [])
        status_code = str(response.get("status_code") or "") if isinstance(response, dict) else ""
        status_message = str(response.get("status_message") or "") if isinstance(response, dict) else ""
        transaction_status = str(response.get("transaction_status") or "") if isinstance(response, dict) else ""
        order_id = str(response.get("order_id") or "") if isinstance(response, dict) else ""
        transaction_id = str(response.get("transaction_id") or "") if isinstance(response, dict) else ""
        if artifact:
            self.log(f"[gopay-qr] 二维码已生成: {artifact}")
            print(f"GOPAY_QR_FILE={artifact}", flush=True)
        if kind == "qr_string":
            print(f"GOPAY_QR_DATA={payload}", flush=True)
        elif payload:
            print(f"GOPAY_QR_URL={payload}", flush=True)
        self.log(
            "[gopay-qris] QR charge ready, starting GoPay APP payment "
            f"mode={charge_mode or '-'} http={qr_info.get('http_status') or '-'} "
            f"status_code={status_code or '-'} transaction_status={transaction_status or '-'} "
            f"order_id={order_id or '-'} transaction_id={transaction_id or '-'} "
            f"payload_type={kind or '-'} artifact={artifact or '-'} "
            f"keys={','.join(response_keys) if response_keys else '-'} "
            f"message={status_message[:160]!r}"
        )
        qris = self._qris_string_from_info(qr_info, artifact)
        print(f"GOPAY_QRIS_DATA={qris}", flush=True)
        qris_result = self._run_qris_app_payment(qris, qr_info)
        if cs_id:
            result = self._chatgpt_verify(cs_id, timeout_s=self.qr_wait_timeout, qris_info=qr_info)
        else:
            result = {"state": "succeeded", "cs_id": cs_id}
        accounts_check = result.get("accounts_check") if isinstance(result.get("accounts_check"), dict) else (self._chatgpt_accounts_check() if cs_id else {})
        result.update({
            "snap_token": snap_token,
            "qr_payload_type": kind,
            "qr_artifact": artifact,
            "qr_charge_mode": charge_mode,
            "qr_payload_preview": payload[:240],
            "qr_http_status": qr_info.get("http_status"),
            "qr_response_headers": qr_info.get("response_headers") or {},
            "qr_response_keys": response_keys,
            "qr_status_code": status_code,
            "qr_status_message": status_message,
            "qr_transaction_status": transaction_status,
            "qr_order_id": order_id,
            "qr_transaction_id": transaction_id,
            "qris_payment": qris_result,
            "accounts_check": accounts_check,
        })
        return result

    def _run_midtrans_and_gopay(self, snap_token: str, cs_id: str) -> dict:
        self._midtrans_load_transaction(snap_token)
        reference_id = self._midtrans_init_linking(snap_token)

        # ── Linking: OTP + first PIN
        self._gopay_validate_reference(reference_id)
        self._gopay_user_consent(reference_id)
        print(
            f"GOPAY_OTP_TARGET phone={self.phone} country_code={self.country_code}",
            flush=True,
        )
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


def _json_path_get_with_parent(obj: Any, path: str) -> tuple[Any, Any]:
    cur = obj
    parent = obj
    for part in (path or "").split("."):
        part = part.strip()
        if not part:
            continue
        parent = cur
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if idx >= len(cur):
                return None, parent
            cur = cur[idx]
        else:
            return None, parent
    return cur, parent


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
    """Yield (text, timestamp, source object) candidates from relay/webhook JSON."""
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
            yield " ".join(pieces), ts, obj
        for value in obj.values():
            yield from _iter_json_message_candidates(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_json_message_candidates(item)
    elif isinstance(obj, str):
        yield obj, None, obj


def _extract_otp_from_payload(
    payload: Any,
    *,
    code_regex: str = DEFAULT_OTP_REGEX,
    json_path: str = "",
    issued_after: float = 0.0,
    phone: str = "",
    country_code: str = "",
) -> str:
    if isinstance(payload, str):
        stripped = payload.strip()
        if stripped[:1] in ("{", "["):
            try:
                payload = json.loads(stripped)
            except Exception:
                if phone:
                    return ""
                return _extract_otp_from_text(payload, code_regex=code_regex)
        else:
            if phone:
                return ""
            return _extract_otp_from_text(payload, code_regex=code_regex)

    if json_path:
        target, parent = _json_path_get_with_parent(payload, json_path)
        if target is None:
            return ""
        if phone:
            source_phone_values = _collect_phone_values(target) or _collect_phone_values(parent)
            if not source_phone_values or not _phone_values_match(source_phone_values, phone, country_code):
                return ""
        if not isinstance(target, str):
            target = json.dumps(target, ensure_ascii=False)
        return _extract_otp_from_text(target, code_regex=code_regex)

    found = ""
    for text, ts, source_obj in _iter_json_message_candidates(payload):
        if issued_after and ts is not None and ts < issued_after:
            continue
        if phone:
            phone_values = _collect_phone_values(source_obj)
            if not phone_values or not _phone_values_match(phone_values, phone, country_code):
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
    phone: str = "",
    country_code: str = "",
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
                            phone=phone,
                            country_code=country_code,
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
    phone: str = "",
    country_code: str = "",
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
        sess = trace_session(requests.Session(), "gopay.otp_relay")
        base_params = dict(params or {})
        if phone and "phone" not in base_params:
            base_params["phone"] = phone
        if country_code and "country_code" not in base_params:
            base_params["country_code"] = country_code
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
                    phone=phone,
                    country_code=country_code,
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
    phone: str = "",
    country_code: str = "",
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
                code = _extract_otp_from_payload(
                    text,
                    code_regex=code_regex,
                    phone=phone,
                    country_code=country_code,
                )
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
    otp_phone = _phone_digits(
        otp_cfg.get("phone")
        or otp_cfg.get("phone_number")
        or gopay_cfg.get("phone_number")
        or ""
    )
    otp_country_code = _phone_digits(
        otp_cfg.get("country_code") or gopay_cfg.get("country_code") or ""
    )

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
            phone=otp_phone,
            country_code=otp_country_code,
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
                phone=otp_phone,
                country_code=otp_country_code,
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
                phone=otp_phone,
                country_code=otp_country_code,
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
    raw_gopay_cfg = cfg.get("gopay") or {}
    if not raw_gopay_cfg:
        print("[error] config has no 'gopay' block", file=sys.stderr)
        sys.exit(2)
    try:
        gopay_cfg = dict(raw_gopay_cfg) if is_qr_payment_enabled(raw_gopay_cfg) else pick_gopay_account_config(raw_gopay_cfg)
    except GoPayError as e:
        print(f"[error] {e}", file=sys.stderr)
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
        proxy_cfg=cfg,
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
