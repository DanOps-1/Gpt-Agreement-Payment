"""PayPal "no-card" signup via pure protocol (no browser, no Playwright).

Replays the captured `/root/no_card_paypal_plus` flow as plain HTTPS:
    /agreements/approve  -> /checkoutweb/signup
    DeferredFeature / GriffinMetadataQuery / CheckoutSessionDataQuery
    InitiateRiskBasedTwoFactorPhoneConfirmation  (SMS out)
    ConfirmRiskBasedTwoFactorPhoneConfirmation   (OTP in)
    SignUpNewMemberMutation                      (no card field)
    /checkoutweb/drop -> /webapps/hermes -> /graphql/ authorize

Inputs are the upstream merchant tokens (`ba_token`, optionally `ec_token`)
produced by the Stripe -> PayPal handoff already implemented in
`CTF-pay/card.py`. The signup persona is random data from meiguodizhi's
`/fr-address`; the OTP arrives at a user-supplied phone via a user-supplied
SMS gateway (env: PPS_PAYPAL_PHONE_E164 + PPS_SMS_API_URL).

hCaptcha handling: pure-protocol only.  When PayPal serves authchallenge HTML,
we replay `/auth/logclientdata`, obtain a pre-supplied / Node-passive /
createTask-compatible token, submit `/auth/validatecaptcha`, then retry the
original GraphQL mutation.  No PayPal page automation is used on this path.
"""
from __future__ import annotations

import base64
import html as html_lib
import json
import logging
import os
import random
import re
import shutil
import string
import subprocess
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    from curl_cffi.requests import Session as _CffiSession
    _HAS_CFFI = True
except ImportError:  # pragma: no cover - exercised only on minimal envs
    _CffiSession = None  # type: ignore[assignment]
    _HAS_CFFI = False

import requests

logger = logging.getLogger(__name__)

# ── External providers ────────────────────────────────────────────────────────
PERSONA_URL = "https://www.meiguodizhi.com/api/v1/dz"
# Tampermonkey v32 uses meiguodizhi's default US address endpoint:
#   POST /api/v1/dz  {"path":"/","method":"address"}
# Keep the protocol replay aligned with that instead of the older /fr-address
# capture helper.
PERSONA_PATH = "/"
# Sensitive: Do not hardcode phone / SMS gateway key into source code. Inject via env variables or pass through upper-layer config from the caller (scripts/
# no_card_paypal_plus.py / webui runner).
SMS_PHONE_E164 = os.environ.get("PPS_PAYPAL_PHONE_E164", "")  # e.g. "+1XXXXXXXXXX"
SMS_API_URL = os.environ.get("PPS_SMS_API_URL", "")  # e.g. http://your-sms-gateway/api/get_sms?key=YOUR_KEY

# ── PayPal hosts / paths ──────────────────────────────────────────────────────
PP_ORIGIN = "https://www.paypal.com"

# ── Default UA matches the captured Chrome 146 desktop fingerprint ───────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


# ── Result types ──────────────────────────────────────────────────────────────
@dataclass
class Persona:
    first_name: str
    last_name: str
    email: str
    password: str
    line1: str
    city: str
    state: str
    postal_code: str
    country: str  # ISO-2 (FR for fr-address)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SignupResult:
    success: bool
    error: Optional[str] = None
    error_code: Optional[str] = None
    ec_token: Optional[str] = None
    ba_token: Optional[str] = None
    user_id: Optional[str] = None
    return_url: Optional[str] = None
    euat: Optional[str] = None  # x-paypal-internal-euat for downstream callers
    persona: Optional[Persona] = None
    cookies: dict[str, str] = field(default_factory=dict)
    debug: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.persona is not None:
            d["persona"] = asdict(self.persona)
        return d


class CaptchaRequired(RuntimeError):
    """Raised when the server gates progress on a captcha token we won't mint."""

    def __init__(self, message: str, *, html: str = "", op_name: str = "") -> None:
        super().__init__(message)
        self.html = html
        self.op_name = op_name


def _redact_for_log(obj: Any) -> Any:
    """Return a JSON-safe diagnostic copy with payment/secret fields redacted."""
    secret_keys = {
        "cardNumber",
        "number",
        "securityCode",
        "cvc",
        "cvv",
        "accessToken",
        "refresh_token",
        "password",
    }
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in secret_keys:
                s = str(v or "")
                if k in {"cardNumber", "number"} and len(s) >= 4:
                    out[k] = f"****{s[-4:]}"
                elif k in {"accessToken", "refresh_token"} and s:
                    out[k] = f"{s[:10]}...{s[-6:]}"
                else:
                    out[k] = "***" if s else ""
            else:
                out[k] = _redact_for_log(v)
        return out
    if isinstance(obj, list):
        return [_redact_for_log(x) for x in obj]
    return obj


def _dump_gql_debug(op_name: str, payload: dict[str, Any]) -> None:
    """Persist last GraphQL diagnostic payload for replay diffing."""
    try:
        with open(f"/tmp/pps_gql_{op_name}_last.json", "w", encoding="utf-8") as f:
            json.dump(_redact_for_log(payload), f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    # If the server returned an authchallenge HTML page, keep the full document
    # separately.  The JSON diagnostic intentionally keeps only a head/tail in
    # some call sites, which is not enough to replay /auth/validatecaptcha
    # because the hidden _csrf/_requestId/_hash fields live near the bottom.
    try:
        text = str(payload.get("response_text") or "")
        if text.lstrip().startswith("<"):
            with open(f"/tmp/pps_gql_{op_name}_last.html", "w", encoding="utf-8") as f:
                f.write(text)
    except Exception:
        pass


# ── HTTP session ──────────────────────────────────────────────────────────────
def _make_session(proxy: Optional[str]) -> Any:
    if _HAS_CFFI:
        # Match the captured flow exactly (Chrome 146 desktop).
        s = _CffiSession(impersonate="chrome146")
        s.trust_env = False
        if proxy:
            p = proxy
            if p.startswith("socks5://"):
                p = "socks5h://" + p[len("socks5://"):]
            s.proxies = {"http": p, "https": p}
        else:
            s.proxies = {"http": "", "https": ""}
        return s
    s = requests.Session()
    s.trust_env = False
    s.headers["User-Agent"] = USER_AGENT
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def _session_cookie_names(s: Any) -> list[str]:
    """Best-effort cookie-name snapshot for risk/debug diffs."""
    try:
        jar = getattr(s, "cookies", None)
        if jar is None:
            return []
        try:
            return sorted(str(k) for k in (jar.get_dict() or {}).keys())
        except Exception:
            pass
        names: list[str] = []
        for c in jar:
            name = getattr(c, "name", "")
            if name:
                names.append(str(name))
        return sorted(set(names))
    except Exception:
        return []


US_STATE_ABBR: dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "FLORIDA": "FL",
    "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL",
    "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS", "KENTUCKY": "KY",
    "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN",
    "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH",
    "OKLAHOMA": "OK", "OREGON": "OR", "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI", "WYOMING": "WY",
}


def _us_state_code(value: str) -> str:
    v = (value or "").strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return US_STATE_ABBR.get(v.upper(), v)


# ── Persona provider (meiguodizhi default US address) ─────────────────────────
def fetch_persona(*, proxy: Optional[str] = None, timeout: int = 20) -> Persona:
    """Pull one random US persona/address like the userscript's getAddr()."""
    s = _make_session(proxy)
    method = "address" if PERSONA_PATH == "/" else "refresh"
    body = {"city": "", "path": PERSONA_PATH, "method": method}
    resp = s.post(
        PERSONA_URL,
        json=body,
        headers={
            "Origin": "https://www.meiguodizhi.com",
            "Referer": "https://www.meiguodizhi.com" + PERSONA_PATH,
            "Accept": "application/json, text/plain, */*",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok" or "address" not in data:
        raise RuntimeError(f"meiguodizhi unexpected response: {data}")
    a = data["address"]

    # Names come from the generator as either a single token ("Discli") or
    # "Given Family". Split on whitespace and synthesise a placeholder family
    # name if absent — PayPal rejects single-char last names.
    full = (a.get("Full_Name") or "").strip()
    parts = full.split()
    first = parts[0] if parts else _rand_word(6).capitalize()
    last = " ".join(parts[1:]) if len(parts) > 1 else _rand_word(7).capitalize()

    # The userscript creates a fresh Gmail-looking address using only
    # [a-z0-9].  meiguodizhi usernames may contain hyphens/words that are valid
    # RFC email locals but not Gmail-looking and have correlated with OAS.
    email = f"{_rand_alnum(16)}@gmail.com"

    # Mirror the userscript's strong password shape (upper/lower/digit/symbol)
    # instead of reusing synthetic generator passwords.
    password = _rand_paypal_password()

    return Persona(
        first_name=first,
        last_name=last,
        email=email,
        password=password,
        line1=a.get("Address") or "",
        city=a.get("City") or "",
        state=_us_state_code(a.get("State") or a.get("State_Full") or ""),
        postal_code=(a.get("Zip_Code") or "")[:5],
        country="US" if PERSONA_PATH == "/" else "FR",
        raw=a,
    )


def _rand_word(n: int) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


def _rand_alnum(n: int) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _rand_password(n: int = 14) -> str:
    pool = string.ascii_letters + string.digits
    return "".join(random.choices(pool, k=n))


def _rand_paypal_password(n: int = 14) -> str:
    letters_l = string.ascii_lowercase
    letters_u = string.ascii_uppercase
    digits = string.digits
    symbols = "!@#$%^"
    chars = [
        random.choice(letters_l),
        random.choice(letters_u),
        random.choice(digits),
        random.choice(symbols),
    ]
    pool = letters_l + letters_u + digits + symbols
    chars.extend(random.choice(pool) for _ in range(max(0, n - len(chars))))
    random.shuffle(chars)
    return "".join(chars)


# ── SMS OTP provider (a.62-us.com) ────────────────────────────────────────────
_OTP_RE = re.compile(r"(?:\b|:|：)(\d{4,8})(?:\b|$)")


def _sms_gateway_text(proxy: Optional[str] = None) -> str:
    s = _make_session(proxy)
    try:
        r = s.get(SMS_API_URL, timeout=10)
        return (r.text or "").strip()
    except Exception:
        return ""


def wait_for_sms_otp(
    *,
    after_ts: float,
    timeout: int = 180,
    poll_interval: float = 4.0,
    proxy: Optional[str] = None,
    baseline_text: str = "",
) -> str:
    """Poll the SMS gateway until an OTP newer than ``after_ts`` arrives.

    The gateway returns a single line in either ``no|<msg>|...`` form (no
    new SMS yet) or ``yes|<digits or text>|...`` once one lands. We accept
    any line whose first field is not ``no``.
    """
    s = _make_session(proxy)
    deadline = time.time() + timeout
    last_text = ""
    while time.time() < deadline:
        try:
            r = s.get(SMS_API_URL, timeout=10)
            text = (r.text or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("sms poll error: %s", e)
            time.sleep(poll_interval)
            continue

        if text != last_text:
            logger.info("sms gateway: %s", text)
            last_text = text

        # The gateway may keep returning the previous PayPal SMS as
        # ``yes|...``.  Snapshot before requesting the new OTP and require a
        # payload change; otherwise we submit a stale code and PayPal returns
        # VALIDATION_FAILED.
        if baseline_text and text == baseline_text:
            time.sleep(poll_interval)
            continue

        # Format: <status>|<payload>|<rest>
        parts = text.split("|", 2)
        if len(parts) >= 2 and parts[0].lower() != "no":
            payload = parts[1]
            m = _OTP_RE.search(payload)
            if m:
                return m.group(1)
            # Some gateways return the digits as the whole payload
            digits = re.sub(r"\D", "", payload)
            if 4 <= len(digits) <= 8:
                return digits
        time.sleep(poll_interval)
    raise TimeoutError(f"sms otp not received within {timeout}s (last: {last_text!r})")


# ── PayPal protocol primitives ────────────────────────────────────────────────
_EC_RE = re.compile(r"(EC-[A-Z0-9]{17,})")
_ONBOARD_RE = re.compile(
    r'onboardingLink"\s*:\s*"([^"]*?/agreements/approve\?[^"]+)'
)
_UL_ONBOARD_RE = re.compile(
    r'href=["\']([^"\']*?ulOnboardRedirect=true[^"\']*)["\']',
    re.I,
)


def _unescape_url(u: str) -> str:
    # URL attributes in PayPal pages often contain a raw query string.  Do not
    # run generic html.unescape() here: Python follows legacy HTML rules and
    # turns substrings like ``&timestamp`` into ``×tamp`` (``&times`` entity),
    # which breaks PayPal's recaptcha_v3 iframe URL and prevents token minting.
    # Only decode the URL escaping forms we actually see in Next/Dust output.
    return (
        (u or "")
        .replace("&amp;", "&")
        .replace("&#38;", "&")
        .replace("&#x26;", "&")
        .replace("\\u0026", "&")
        .replace("\\/", "/")
    )


def _first_query_value(url: str, name: str) -> str:
    try:
        return (urllib.parse.parse_qs(urllib.parse.urlparse(url or "").query).get(name) or [""])[0]
    except Exception:
        return ""


def _build_onboard_url(
    *,
    ba_token: str,
    locale_country: str,
    locale_lang: str,
    source_url: str = "",
) -> str:
    """Build the guest onboarding /agreements/approve URL.

    PayPal pages expose several similarly named links.  The one we need before
    signup is `/agreements/approve?...ulOnboardRedirect=true`, not a later
    `/webapps/hermes?...ulOnboardRedirect=true` fallback URL.  Keeping this
    canonical avoids poisoning all later GraphQL Referer headers with hermes.
    """
    ssrt = _first_query_value(source_url, "ssrt")
    params: list[tuple[str, str]] = []
    if ssrt:
        params.append(("ssrt", ssrt))
    params.extend([
        ("ul", "1"),
        ("country.x", locale_country),
        ("locale.x", f"{locale_lang}_{locale_country}"),
        ("modxo_redirect_reason", "guest_user"),
        ("ulOnboardRedirect", "true"),
        ("ba_token", ba_token),
    ])
    return f"{PP_ORIGIN}/agreements/approve?{urllib.parse.urlencode(params)}"


def _coerce_onboard_url(
    onboard_url: str,
    *,
    ba_token: str,
    locale_country: str,
    locale_lang: str,
) -> str:
    """Return a safe `/agreements/approve` onboarding URL.

    Runtime evidence showed `_UL_ONBOARD_RE` can capture a hermes URL containing
    `ulOnboardRedirect=true`; if that URL is used as the page/referrer, PayPal
    answers signup GraphQL with an authchallenge page.  Accept only the actual
    approve endpoint and rebuild otherwise.
    """
    u = _unescape_url(onboard_url or "")
    if u.startswith("/"):
        u = PP_ORIGIN + u
    try:
        parsed = urllib.parse.urlparse(u)
    except Exception:
        parsed = urllib.parse.urlparse("")
    if parsed.netloc and "paypal.com" in parsed.netloc and parsed.path == "/agreements/approve":
        params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        seen = {k for k, _ in params}
        def _set(name: str, value: str) -> None:
            nonlocal params
            params = [(k, v) for k, v in params if k != name]
            params.append((name, value))
        if "ul" not in seen:
            params.append(("ul", "1"))
        _set("country.x", locale_country)
        _set("locale.x", f"{locale_lang}_{locale_country}")
        if "modxo_redirect_reason" not in seen:
            params.append(("modxo_redirect_reason", "guest_user"))
        _set("ulOnboardRedirect", "true")
        _set("ba_token", ba_token)
        return f"{PP_ORIGIN}/agreements/approve?{urllib.parse.urlencode(params)}"
    return _build_onboard_url(
        ba_token=ba_token,
        locale_country=locale_country,
        locale_lang=locale_lang,
        source_url=u,
    )


def _build_signup_url(
    *,
    ba_token: str,
    ec_token: str,
    locale_country: str,
    locale_lang: str,
    source_url: str = "",
) -> str:
    ssrt = _first_query_value(source_url, "ssrt")
    params: list[tuple[str, str]] = []
    if ssrt:
        params.append(("ssrt", ssrt))
    params.extend([
        ("ul", "1"),
        ("country.x", locale_country),
        ("locale.x", f"{locale_lang}_{locale_country}"),
        ("modxo_redirect_reason", "guest_user"),
        ("ba_token", ba_token),
        ("token", ec_token),
        ("rcache", "1"),
        ("cookieBannerVariant", "hidden"),
    ])
    return f"{PP_ORIGIN}/checkoutweb/signup?{urllib.parse.urlencode(params)}"


def _prime_checkout_signup(
    s: Any,
    *,
    signup_url: str,
    referer: str,
    locale_country: str,
    locale_lang: str,
    timeout: int,
) -> tuple[str, str]:
    """GET `/checkoutweb/signup` without following through to hermes.

    Browser success traces keep `/checkoutweb/signup?...token=EC-...` as the
    referer for every Weasley GraphQL call.  A plain HTTP client that follows
    redirects too aggressively may end at `/webapps/hermes` and then submits the
    signup mutation with a hermes referer.  This helper deliberately preserves
    the canonical signup URL even if PayPal replies with a redirect.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": f"{locale_lang}-{locale_country},{locale_lang};q=0.9,en;q=0.8",
        "Referer": referer,
        "Upgrade-Insecure-Requests": "1",
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Full-Version-List": '"Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Model": '""',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Arch": '"x86"',
        "Sec-CH-Device-Memory": "8",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-User": "?1",
    }
    try:
        r = s.get(signup_url, headers=headers, timeout=timeout, allow_redirects=False)
    except Exception as e:
        logger.warning("signup prime soft-failed: %s", e)
        return signup_url, ""
    text = getattr(r, "text", "") or ""
    status = int(getattr(r, "status_code", 0) or 0)
    loc = (getattr(r, "headers", {}) or {}).get("location") or (getattr(r, "headers", {}) or {}).get("Location")
    final_url = str(getattr(r, "url", signup_url) or signup_url)
    logger.info("signup prime status=%s url=%s loc=%s len=%d", status, final_url[:120], (loc or "")[:120], len(text))
    if loc:
        loc_abs = urllib.parse.urljoin(final_url, _unescape_url(loc))
        if "/checkoutweb/signup" in loc_abs:
            return loc_abs, text
        logger.warning("signup prime redirected away to %s; keep canonical signup referer", loc_abs[:160])
        return signup_url, text
    if "/checkoutweb/signup" in final_url:
        return final_url, text
    return signup_url, text


def _bootstrap(
    s: Any,
    ba_token: str,
    *,
    locale_country: str,
    locale_lang: str,
    timeout: int = 30,
) -> tuple[str, str, str]:
    """GET /agreements/approve, follow the onboarding redirect, return
    (ec_token, signup_url, signup_html). Cookies (incl. datadome) stick on
    the session for subsequent GraphQL calls."""
    # The browser trace lands on /agreements/approve from Stripe with a full
    # top-level navigation fingerprint.  When these navigation headers are
    # missing PayPal/DataDome often returns the tiny 403 "Please enable JS"
    # interstitial before the normal page has a chance to set a first
    # datadome cookie.  Keep the initial request browser-like, and force the
    # intended locale in the query; otherwise the server derives JP/ja from the
    # proxy IP and the later signup GraphQL country/locale no longer matches
    # the page state.
    locale = f"{locale_lang}_{locale_country}"
    url = (
        f"{PP_ORIGIN}/agreements/approve?"
        f"ba_token={urllib.parse.quote(ba_token)}"
        f"&country.x={urllib.parse.quote(locale_country)}"
        f"&locale.x={urllib.parse.quote(locale)}"
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": f"{locale_lang}-{locale_country},{locale_lang};q=0.9,en;q=0.8",
        "Referer": "https://chatgpt.com/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Full-Version-List": '"Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Model": '""',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Arch": '"x86"',
        "Sec-CH-Device-Memory": "8",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-User": "?1",
    }
    r1 = s.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    html1 = r1.text or ""
    if r1.status_code != 200:
        # PayPal/DataDome often returns HTTP 403 with an HTML interstitial that
        # loads ct.ddc.paypal.com/i.js + geo.ddc.paypal.com/interstitial.
        # Surface it as CaptchaRequired so callers can distinguish "current
        # pure-protocol/IP path is blocked" from ordinary transport failures.
        head = html1[:3000].lower()
        if "geo.ddc.paypal.com" in head or "ct.ddc.paypal.com" in head or "datadome" in head:
            raise CaptchaRequired(f"datadome interstitial on /agreements/approve status={r1.status_code}")
        raise RuntimeError(f"/agreements/approve failed: {r1.status_code}")
    if "<title>You are being redirected" in html1 or "datadome" in html1[:1500].lower() and "<title>" not in html1:
        # Datadome interstitial - we can't solve it
        raise CaptchaRequired("datadome interstitial on /agreements/approve")

    # Extract the onboardingLink (server-decided guest-user redirect) and
    # the EC token already baked into the page.
    m_link = _ONBOARD_RE.search(html1) or _UL_ONBOARD_RE.search(html1)
    m_ec = _EC_RE.search(html1)
    if not m_ec:
        raise RuntimeError("EC token not found in /agreements/approve response")
    ec_token = m_ec.group(1)

    if m_link:
        onboard_url = _unescape_url(m_link.group(1))
        if onboard_url.startswith("/"):
            onboard_url = PP_ORIGIN + onboard_url
    else:
        # Construct minimally if the link wasn't embedded
        onboard_url = _build_onboard_url(
            ba_token=ba_token,
            locale_country=locale_country,
            locale_lang=locale_lang,
            source_url=url,
        )
    onboard_url = _coerce_onboard_url(
        onboard_url,
        ba_token=ba_token,
        locale_country=locale_country,
        locale_lang=locale_lang,
    )

    try:
        _paypal_pay_pre_onboard_warmup(
            s,
            ba_token=ba_token,
            ec_token=ec_token,
            approve_html=html1,
            onboard_url=onboard_url,
            locale_country=locale_country,
            locale_lang=locale_lang,
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("pay pre-onboard warmup soft-failed: %s", e)

    # Hit it; server 302's to /checkoutweb/signup with EC token attached.
    # Do not follow all redirects here: if the chain reaches hermes, the final
    # URL poisons later Weasley GraphQL Referers.  We only need the first
    # checkoutweb/signup URL and its cookies.
    r2 = s.get(
        onboard_url,
        headers={
            **headers,
            "Referer": _paypal_pay_url(ba_token, onboard_url=onboard_url),
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=timeout,
        allow_redirects=False,
    )
    if r2.status_code not in {200, 301, 302, 303, 307, 308}:
        raise RuntimeError(f"/checkoutweb/signup chase failed: {r2.status_code}")
    loc = (getattr(r2, "headers", {}) or {}).get("location") or (getattr(r2, "headers", {}) or {}).get("Location")
    loc_abs = urllib.parse.urljoin(str(getattr(r2, "url", onboard_url) or onboard_url), _unescape_url(loc or "")) if loc else ""
    signup_url = ""
    if loc_abs and "/checkoutweb/signup" in loc_abs:
        signup_url = loc_abs
    else:
        m_loc_ec = _EC_RE.search(loc_abs or "")
        if m_loc_ec:
            ec_token = m_loc_ec.group(1)
        signup_url = _build_signup_url(
            ba_token=ba_token,
            ec_token=ec_token,
            locale_country=locale_country,
            locale_lang=locale_lang,
            source_url=loc_abs or onboard_url,
        )
        if loc_abs:
            logger.warning("onboard redirected to %s; using canonical signup URL %s",
                           loc_abs[:140], signup_url[:140])

    # Prime the signup page/cookies while preserving the canonical signup URL.
    signup_url, signup_html = _prime_checkout_signup(
        s,
        signup_url=signup_url,
        referer=onboard_url,
        locale_country=locale_country,
        locale_lang=locale_lang,
        timeout=timeout,
    )
    # EC may be refreshed on the redirect/page
    m_ec2 = _EC_RE.search(signup_url) or _EC_RE.search(signup_html) or _EC_RE.search(r2.text or "")
    if m_ec2:
        ec_token = m_ec2.group(1)

    return ec_token, signup_url, signup_html


def _paypal_pay_url(ba_token: str, *, onboard_url: str = "") -> str:
    """Best-effort virtual /pay URL used by PayPal's modular checkout SPA."""
    ssrt = ""
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(onboard_url or "").query)
        ssrt = (qs.get("ssrt") or [""])[0]
    except Exception:
        ssrt = ""
    if ssrt:
        return f"{PP_ORIGIN}/pay?ssrt={urllib.parse.quote(ssrt)}&token={urllib.parse.quote(ba_token)}&ul=1"
    return f"{PP_ORIGIN}/pay?token={urllib.parse.quote(ba_token)}&ul=1"


def _paypal_pay_observability_emit(
    s: Any,
    *,
    ba_token: str,
    pay_url: str,
    event_name: str,
    payload: Optional[dict[str, Any]] = None,
    timeout: int,
) -> None:
    """Send a minimal modular-checkout observability event.

    The browser trace emits several `/pay/api/trpc/observability.handleClientEmit`
    calls before clicking "Create account".  They do not carry secrets and
    typically only return `{"resp":"ok"}`, but reproducing a small subset keeps
    the server-side interaction order closer to the captured userscript run.
    """
    body = {
        "json": [{
            "eventName": event_name,
            "logLevel": "info",
            "payload": payload or {
                "analytics": {
                    "event_name": event_name,
                    "country": "US",
                    "path_name": "/pay",
                    "client_mtdt_id": ba_token,
                },
                "common": {"memberOrGuestFlow": "member"},
            },
        }]
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": PP_ORIGIN,
        "Referer": pay_url,
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        s.post(
            f"{PP_ORIGIN}/pay/api/trpc/observability.handleClientEmit?token={urllib.parse.quote(ba_token)}",
            json=body,
            headers=headers,
            timeout=max(8, min(timeout, 20)),
        )
    except Exception as e:
        logger.debug("pay observability %s soft-failed: %s", event_name, e)


def _paypal_identity_di_log(
    s: Any,
    *,
    ba_token: str,
    pay_url: str,
    timeout: int,
) -> None:
    """Replay the light `/identity/di/log` telemetry seen before onboarding."""
    now = str(int(time.time() * 1000))
    body = {
        "events": [
            {"level": "info", "event": "DFPJS_LIB_LOADED", "payload": {"timestamp": now, "comp": "dfpjs", "btz": "Asia/Shanghai", "ul_corr_id": None}},
            {"level": "info", "event": "DFPJS_VENDOR_INVOKED", "payload": {"timestamp": now, "comp": "dfpjs", "btz": "Asia/Shanghai", "ul_corr_id": None}},
            {"level": "info", "event": "DFPJS_VENDOR_RESPONSE_RECEIVED", "payload": {"timestamp": now, "comp": "dfpjs", "btz": "Asia/Shanghai", "ul_corr_id": None}},
            {"level": "info", "event": "DFPJS_EDGE_MAPPING_COMPLETE", "payload": {"timestamp": now, "comp": "dfpjs", "btz": "Asia/Shanghai", "ul_corr_id": None}},
        ],
        "meta": {},
        "tracking": [
            {"event_name": "LIB_LOADED", "component": "dfpjs", "browser_timezone": "Asia/Shanghai", "ul_corr_id": None},
            {"event_name": "VENDOR_INVOKED", "CMID": ba_token, "component": "dfpjs", "browser_timezone": "Asia/Shanghai", "ul_corr_id": None},
            {"event_name": "VENDOR_RESPONSE_RECEIVED", "CMID": ba_token, "component": "dfpjs", "browser_timezone": "Asia/Shanghai", "ul_corr_id": None},
        ],
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": PP_ORIGIN,
        "Referer": pay_url,
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        r = s.post(f"{PP_ORIGIN}/identity/di/log", json=body, headers=headers, timeout=max(8, min(timeout, 20)))
        logger.info("identity di log status=%s", getattr(r, "status_code", "?"))
    except Exception as e:
        logger.debug("identity di log soft-failed: %s", e)


def _paypal_pay_pre_onboard_warmup(
    s: Any,
    *,
    ba_token: str,
    ec_token: str,
    approve_html: str,
    onboard_url: str,
    locale_country: str,
    locale_lang: str,
    timeout: int,
) -> None:
    """Pure-HTTP subset of the PayPal `/pay` page side effects.

    In the successful capture, `/agreements/approve` renders the modular
    checkout login/create-account shell, which uses a virtual `/pay?...` URL,
    emits PayPal observability/identity/FraudNet beacons under the BA token,
    and only then navigates to the ulOnboardRedirect URL.  Skipping the side
    effects is not always fatal, but it makes the flow much easier for OAS to
    bucket as synthetic.  Keep this best-effort and non-blocking.
    """
    pay_url = _paypal_pay_url(ba_token, onboard_url=onboard_url)

    # Passive challenge JS is loaded by the page; just fetching it helps the
    # cookie/page state match the browser order.  It is not solved here.
    m = re.search(r'["\'](/auth/createchallenge/[^"\']*hcaptchapassive\.js[^"\']*)', approve_html or "", re.I)
    if m:
        ch_url = PP_ORIGIN + _unescape_url(m.group(1))
        try:
            r = s.get(
                ch_url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": pay_url,
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-Mode": "no-cors",
                    "Sec-Fetch-Dest": "script",
                },
                timeout=max(8, min(timeout, 20)),
            )
            logger.info("pay passive challenge js status=%s", getattr(r, "status_code", "?"))
        except Exception as e:
            logger.debug("pay passive challenge js soft-failed: %s", e)

    _paypal_pay_observability_emit(
        s,
        ba_token=ba_token,
        pay_url=pay_url,
        event_name="modxo_not_sdk_integration",
        payload={
            "analytics": {
                "event_name": "modxo_not_sdk_integration",
                "is_authenticated": "0",
                "country": locale_country,
                "path_name": "/pay",
                "t_device": int(time.time() * 1000),
            },
            "common": {"memberOrGuestFlow": "member"},
        },
        timeout=timeout,
    )
    _paypal_pay_observability_emit(
        s,
        ba_token=ba_token,
        pay_url=pay_url,
        event_name="identity_login_redirect_to_xo_onboarding",
        payload={
            "analytics": {
                "event_name": "identity_login_redirect_to_xo_onboarding",
                "ctx_login_intent": "checkout",
                "ctx_login_flow": "Billing Agreement",
                "product": "IWC",
                "client_mtdt_id": ba_token,
                "country": locale_country,
                "path_name": "/pay",
                "from": pay_url,
                "to": onboard_url,
                "t_device": int(time.time() * 1000),
            },
            "common": {"memberOrGuestFlow": "member"},
        },
        timeout=timeout,
    )
    _paypal_identity_di_log(s, ba_token=ba_token, pay_url=pay_url, timeout=timeout)

    # BA-token FraudNet, distinct from the later EC-token onboarding FraudNet.
    try:
        _paypal_fraudnet_warmup(
            s,
            ec_token=ba_token,
            signup_url=pay_url,
            ba_token=ba_token,
            timeout=timeout,
            app_id="IWC_NEXT_CHECKOUT",
        )
    except Exception as e:
        logger.debug("BA fraudnet warmup soft-failed: %s", e)


def _gql(
    s: Any,
    op_name: str,
    variables: dict[str, Any],
    query: str,
    *,
    signup_url: str,
    path: str = "/graphql",
    timeout: int = 30,
    extra_body: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    body = {"operationName": op_name, "variables": variables, "query": query}
    if extra_body:
        body.update(extra_body)
    url = f"{PP_ORIGIN}{path}?{op_name}" if path == "/graphql" else f"{PP_ORIGIN}{path}"
    token = str(variables.get("token") or variables.get("billingAgreementId") or "")
    country = (
        variables.get("country")
        or variables.get("countryCodeAsString")
        or (variables.get("locale") or {}).get("country")
        or "US"
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": PP_ORIGIN,
        "Referer": signup_url,
        "X-Requested-With": "fetch",
        # Captured Weasley signup GraphQL uses checkoutuinodeweb_weasley plus
        # paypal-client-context/x-country.  Missing context is accepted by some
        # nodes but tends to surface as opaque OAS_ERROR/createMemberAccount.
        "X-App-Name": "checkoutuinodeweb_weasley",
        "PayPal-Client-Context": token,
        "PayPal-Client-Metadata-Id": token,
        "X-Country": str(country),
        "X-Locale": "en_US" if str(country).upper() == "US" else f"en_{str(country).upper()}",
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Full-Version-List": '"Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Model": '""',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Arch": '"x86"',
        "Sec-CH-Device-Memory": "8",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    r = s.post(url, json=body, headers=headers, timeout=timeout)
    if r.status_code != 200:
        _dump_gql_debug(op_name, {
            "url": url,
            "status_code": r.status_code,
            "request_headers": headers,
            "cookie_names": _session_cookie_names(s),
            "request": body,
            "response_text": r.text[:2000],
        })
        raise RuntimeError(f"graphql {op_name} HTTP {r.status_code}: {r.text[:300]}")
    try:
        data = r.json()
    except Exception as e:
        text = getattr(r, "text", "") or ""
        _dump_gql_debug(op_name, {
            "url": url,
            "status_code": r.status_code,
            "request_headers": headers,
            "cookie_names": _session_cookie_names(s),
            "request": body,
            "response_headers": dict(getattr(r, "headers", {}) or {}),
            "response_text": text,
            "parse_error": repr(e),
        })
        head = text[:1200].lower()
        if "authchallenge" in head or "recaptcha" in head or "captcha" in head:
            raise CaptchaRequired(
                f"{op_name}: PayPal returned captcha/html instead of JSON",
                html=text,
                op_name=op_name,
            )
        raise RuntimeError(f"graphql {op_name} JSON parse failed: {e}: {text[:200]}")
    if isinstance(data, dict) and data.get("errors"):
        first = data["errors"][0]
        msg = first.get("message", "")
        _dump_gql_debug(op_name, {
            "url": url,
            "status_code": r.status_code,
            "request_headers": headers,
            "cookie_names": _session_cookie_names(s),
            "request": body,
            "response": data,
        })
        if "captcha" in msg.lower() or "RECAPTCHA" in msg:
            raise CaptchaRequired(f"{op_name}: {msg}")
        # Surface but let caller inspect — some mutations return both
        # `errors` and useful `data`.
        logger.warning("graphql %s returned errors: %s", op_name, msg)
    return data


# GraphQL strings — verbatim from the captured /root/no_card_paypal_plus flow
Q_DEFERRED = """query DeferredFeature($channel: String!, $countryCodeAsString: String!, $isBaslAsString: String!, $isForcedGuest: String!, $token: String!, $integrationType: String!) {
  otpLoginContext(token: $token, integrationType: $integrationType) {
    __typename
    context
  }
  elmoExperiment(
    app: "checkoutuinodeweb"
    filters: [{key: "Country", value: $countryCodeAsString}, {key: "Channel", value: $channel}, {key: "IsBasl", value: $isBaslAsString}, {key: "IsGuestOnly", value: $isForcedGuest}]
    res: "weasley:deferredFeature:memberAsDefault"
  ) {
    __typename
    treatments {
      __typename
      experimentId
      experimentName
      factors {
        __typename
        key
        value
      }
      treatmentId
      treatmentName
    }
  }
}
"""

Q_GRIFFIN_METADATA = """query GriffinMetadataQuery($countryCode: CountryCodes!, $languageCode: CheckoutContentLanguageCode!, $shippingCountryCode: CountryCodes!) {
  localeMetadata {
    address(countryCode: $countryCode, languageCode: $languageCode) {
      layout { maxLength minLength isRequired name regex __typename }
      strings {
        cityLabel line1Label line2Label optionalLabel postcodeLabel stateLabel
        stateList { displayText value __typename }
        __typename
      }
      __typename
    }
    shippingAddress: address(countryCode: $shippingCountryCode, languageCode: $languageCode) {
      layout { maxLength minLength isRequired name regex __typename }
      strings {
        cityLabel line1Label line2Label optionalLabel postcodeLabel stateLabel
        stateList { displayText value __typename }
        __typename
      }
      __typename
    }
    currencyCode(countryCode: $countryCode)
    phone(countryCode: $countryCode) {
      masks { mobile __typename }
      patterns { default __typename }
      __typename
    }
    __typename
  }
}
"""

Q_CHECKOUT_SESSION = """query CheckoutSessionDataQuery($token: String!) {
  checkoutSession(token: $token) {
    allowedCardIssuers
    cart {
      cancelUrl { href __typename }
      intent
      billingAddress { city country line1 line2 postalCode state formattedFullAddress __typename }
      shippingAddress { city country firstName isStoreAddress lastName line1 line2 postalCode state formattedFullAddress __typename }
      __typename
    }
    checkoutSessionType
    merchant { country merchantId name __typename }
    __typename
  }
}
"""

Q_ADDRESS_AUTOCOMPLETE = """query AddressAutocompleteQuery($count: Int, $countries: [CountryCodes], $input: String!, $language: CheckoutContentLanguageCode, $location: GeoLocation, $radius: Int, $sessionId: String!) {
  addressAutoComplete(count: $count, countries: $countries, input: $input, language: $language, location: $location, radius: $radius, sessionId: $sessionId) {
    suggestions { addressText mainText placeId secondaryText __typename }
    __typename
  }
}
"""

Q_ADDRESS_FROM_PLACE = """query AddressFromAutocompletePlaceIdQuery($language: CheckoutContentLanguageCode, $placeId: ID!, $sessionId: String!) {
  addressFromAutoCompletePlaceId(language: $language, placeId: $placeId, sessionId: $sessionId) {
    address { line1 line2 city state postalCode country __typename }
    __typename
  }
}
"""

Q_INIT_OTP = """mutation InitiateRiskBasedTwoFactorPhoneConfirmationMutation($phoneNumber: String!, $locale: LocaleInput!, $phoneCountry: CountryCodes!, $token: String!) {
  initiateRiskBasedTwoFactorPhoneConfirmation(
    locale: $locale
    phoneCountry: $phoneCountry
    phoneNumber: $phoneNumber
    token: $token
  ) {
    authId
    challengeId
    state
    __typename
  }
}
"""

Q_CONFIRM_OTP = """mutation ConfirmRiskBasedTwoFactorPhoneConfirmationMutation($pin: String!, $authId: String!, $challengeId: String!, $token: String!) {
  confirmRiskBasedTwoFactorPhoneConfirmation(
    pin: $pin
    authId: $authId
    challengeId: $challengeId
    token: $token
  ) {
    authId
    challengeId
    state
    __typename
  }
}
"""

# Verbatim from capture — alias `onboardAccount: signUpNewMember(...)` is what
# we read back in the response. The selection set pulls buyer.auth.accessToken
# and userId; for the no-card path the funding/3DS fragments will be null.
Q_SIGNUP = """mutation SignUpNewMemberMutation($bank: BankAccountInput, $billingAddress: AddressInput, $card: CardInput, $contentIdentifier: String, $country: CountryCodes, $countrySpecificFirstName: String, $countrySpecificLastName: String, $crsData: CommonReportingStandardsInput, $currencyConversionType: CheckoutCurrencyConversionType, $dateOfBirth: DateOfBirth, $email: String!, $firstName: String!, $gender: Gender, $identityDocument: IdentityDocumentInput, $lastName: String!, $middleName: String, $marketingOptOut: Boolean, $nationality: CountryCodes, $occupation: Occupation, $password: String, $phone: PhoneInput!, $placeOfBirth: CountryCodes, $secondaryIdentityDocument: IdentityDocumentInput, $selectedInstallmentOption: InstallmentsInput, $shareAddressWithDonatee: Boolean, $shippingAddress: AddressInput, $supportedThreeDsExperiences: [ThreeDSPaymentExperience], $token: String!, $residentialAddress: AddressInput, $isSignupIncentiveOptIn: Boolean, $isSignupIncentiveOptInStretch: Boolean, $legalAgreements: LegalAgreementsInput, $collectedConsents: [CollectedConsent]) {
  onboardAccount: signUpNewMember(
    bank: $bank
    billingAddress: $billingAddress
    card: $card
    contentIdentifier: $contentIdentifier
    countrySpecificFirstName: $countrySpecificFirstName
    countrySpecificLastName: $countrySpecificLastName
    country: $country
    crsData: $crsData
    currencyConversionType: $currencyConversionType
    dateOfBirth: $dateOfBirth
    email: $email
    firstName: $firstName
    gender: $gender
    identityDocument: $identityDocument
    lastName: $lastName
    middleName: $middleName
    marketingOptOut: $marketingOptOut
    nationality: $nationality
    occupation: $occupation
    password: $password
    phone: $phone
    placeOfBirth: $placeOfBirth
    secondaryIdentityDocument: $secondaryIdentityDocument
    selectedInstallmentOption: $selectedInstallmentOption
    shareAddressWithDonatee: $shareAddressWithDonatee
    shippingAddress: $shippingAddress
    token: $token
    residentialAddress: $residentialAddress
    isSignupIncentiveOptIn: $isSignupIncentiveOptIn
    isSignupIncentiveOptInStretch: $isSignupIncentiveOptInStretch
    legalAgreements: $legalAgreements
    collectedConsents: $collectedConsents
  ) {
    ...buyer
    flags {
      is3DSecureRequired
      __typename
    }
    ...fundingOptions
    paymentContingencies {
      ...threeDomainSecure
      ...threeDSContingencyData
      __typename
    }
    __typename
  }
}

fragment buyer on CheckoutSession {
  buyer {
    auth {
      accessToken
      __typename
    }
    userId
    __typename
  }
  __typename
}

fragment fundingOptions on CheckoutSession {
  fundingOptions {
    allPlans {
      fundingSources {
        fundingInstrument {
          id
          __typename
        }
        amount {
          currencyCode
          currencyValue
          __typename
        }
        __typename
      }
      fundingContingencies {
        ... on OpenBankingContingency {
          encryptedId
          contingencyReasons
          contingencyType
          __typename
        }
        __typename
      }
      __typename
    }
    fundingInstrument {
      id
      lastDigits
      name
      nameDescription
      type
      __typename
    }
    __typename
  }
  __typename
}

fragment threeDomainSecure on PaymentContingencies {
  threeDomainSecure(experiences: $supportedThreeDsExperiences) {
    status
    redirectUrl {
      href
      __typename
    }
    method
    parameter
    experience
    requestParams {
      key
      value
      __typename
    }
    __typename
  }
  __typename
}

fragment threeDSContingencyData on PaymentContingencies {
  threeDSContingencyData {
    name
    causeName
    resolution {
      type
      resolutionName
      paymentCard {
        billingAddress {
          line1
          line2
          city
          state
          country
          postalCode
          __typename
        }
        expireYear
        expireMonth
        currencyCode
        cardProductClass
        id
        encryptedNumber
        type
        number
        bankIdentificationNumber
        __typename
      }
      contingencyContext {
        deviceDataCollectionUrl {
          href
          __typename
        }
        jwtSpecification {
          jwtDuration
          jwtIssuer
          jwtOrgUnitId
          type
          __typename
        }
        authenticationProvider
        cardBrandProcessed
        reason
        referenceId
        source
        __typename
      }
      __typename
    }
    __typename
  }
  __typename
}
"""

Q_AUTHORIZE = (
    "mutation authorize($billingAgreementId: String!, $addressId: String, "
    "$fundingPreference: billingFundingPreferenceInput, "
    "$legalAgreements: billingLegalAgreementsInput) { "
    "billing { authorize( billingAgreementId: $billingAgreementId "
    "addressId: $addressId fundingPreference: $fundingPreference "
    "legalAgreements: $legalAgreements ) { billingAgreementToken "
    "paymentAction returnURL { href __typename } buyer { userId __typename } "
    "__typename } __typename } }"
)


# ── Persona → SignUpNewMember variables ───────────────────────────────────────
# Tiny calling-code prefix table — longest match wins. Extend as needed.
_CC_TABLE = (
    "1",   # NANP (US/CA)
    "33",  # FR
    "44",  # GB
    "49",  # DE
    "39",  # IT
    "34",  # ES
    "61",  # AU
    "81",  # JP
    "82",  # KR
    "852", # HK
    "86",  # CN
    "91",  # IN
    "65",  # SG
    "62",  # ID
    "60",  # MY
    "63",  # PH
    "66",  # TH
    "84",  # VN
    "55",  # BR
    "52",  # MX
)


def _phone_split(e164: str) -> tuple[str, str]:
    """Returns (calling_code, subscriber). e.g. +1XXXXXXXXXX -> ("1", "XXXXXXXXXX")."""
    raw = (e164 or "").strip()
    s = re.sub(r"\D", "", raw)
    # userscript CONFIG.phone is the US subscriber number only.
    if not raw.startswith("+") and len(s) == 10:
        return "1", s
    for cc in sorted(_CC_TABLE, key=len, reverse=True):
        if s.startswith(cc) and len(s) - len(cc) >= 7:
            return cc, s[len(cc):]
    raise ValueError(f"unparseable phone: {e164}")


def _extract_content_identifier(html: str, locale_country: str, locale_lang: str) -> str:
    """Best-effort extraction of PayPal's dynamic signup terms identifier."""
    for pat in (
        r'"contentIdentifier"\s*:\s*"([^"]*signupTerms[^"]*)"',
        r'\\"contentIdentifier\\"\s*:\s*\\"([^"\\]*signupTerms[^"\\]*)\\"',
        r'([A-Z]{2}:[a-z]{2}:[0-9a-f]{16,64}:compliance\.signupTerms)',
    ):
        m = re.search(pat, html or "", re.I)
        if m:
            return m.group(1).replace("\\/", "/")
    # Current PayPal checkoutuinodeweb capture uses this stable US/en terms
    # content identifier.  The short "US:en:compliance.signupTerms" fallback is
    # syntactically accepted by GraphQL but can fail later inside OAS.
    if locale_country.upper() == "US" and locale_lang.lower() == "en":
        return "US:en:f411614ea3eaac38abc54763fcfca00e:compliance.signupTerms"
    return f"{locale_country}:{locale_lang}:compliance.signupTerms"


def _paypal_fn_sync_data(
    ec_token: str,
    *,
    source: str = "IWC_LOGIN_APP",
    include_d: bool = True,
) -> str:
    """Generate PayPal FraudNet ``fn_sync_data`` close to the captured shape."""
    now_ms = int(time.time() * 1000)
    dc = {
        "screen": {
            "colorDepth": 24,
            "pixelDepth": 24,
            "height": 900,
            "width": 1440,
            "availHeight": 820,
            "availWidth": 1440,
        },
        "ua": USER_AGENT,
    }
    ts2_parts = [
        ("Di0", random.randint(12_000, 24_000)),
        ("Di1", random.randint(5, 18)),
        ("Di2", random.randint(80, 180)),
        ("Ui0", 24),
        ("Ui1", random.randint(40, 80)),
        ("Ui2", random.randint(45, 95)),
        ("Di3", random.randint(2_000, 5_000)),
        ("Di4", 24),
        ("Di5", random.randint(60, 140)),
        ("Uh", random.randint(2_500, 5_500)),
    ]
    rdt_chunks = []
    base_a = random.randint(18_000, 56_000)
    for _ in range(20):
        a = max(1000, base_a + random.randint(-28_000, 28_000))
        b = a + random.randint(-250, 250)
        c = max(1000, a - random.randint(250, 700))
        rdt_chunks.append(f"{a},{b},{c}")
    rdt_tail = f"{random.randint(8_000, 28_000)},{random.randint(20, 80)}"
    payload: dict[str, Any] = {
        "SC_VERSION": "2.0.4",
        "syncStatus": "data",
        "f": ec_token,
        "s": source,
        "chk": {
            "ts": now_ms,
            "eteid": [
                random.randint(-12_000_000_000, -1_000_000_000),
                random.randint(1_000_000_000, 9_000_000_000),
                random.randint(1_000_000_000, 9_000_000_000),
                random.randint(-12_000_000_000, -1_000_000_000),
                random.randint(1_000_000_000, 9_000_000_000),
                random.randint(1_000_000_000, 9_000_000_000),
                None,
                None,
            ],
            "tts": random.randint(20, 80),
        },
        "dc": json.dumps(dc, separators=(",", ":")),
        "wv": False,
        "web_integration_type": "WEB_REDIRECT",
        "cookie_enabled": True,
    }
    # The browser sends a short fn_sync_data without ``d`` to idapps
    # getOtpChallenge, then a longer payload containing typing-speed/rDT data
    # on SignUpNewMember.  Keeping that split avoids making the captcha
    # challenge path look like the final submit path.
    if include_d:
        payload["d"] = {
            "ts2": "".join(f"{k}:{v}" for k, v in ts2_parts),
            "rDT": ":".join(rdt_chunks) + ":" + rdt_tail,
        }
    return urllib.parse.quote(json.dumps(payload, separators=(",", ":")))


def _risk_headers(*, same_site: bool = True, referer: str = "https://www.paypal.com/") -> dict[str, str]:
    """Headers used by PayPal FraudNet/DataCollector endpoints."""
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": PP_ORIGIN,
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Full-Version-List": '"Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Model": '""',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Arch": '"x86"',
        "Sec-Fetch-Site": "same-site" if same_site else "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }


def _rand_token(n: int = 96) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(random.choices(alphabet, k=n))


def _risk_cookie_value(s: Any, *preferred_names: str, fallback_len: int = 96) -> str:
    cookies = _session_cookie_dict(s)
    for name in preferred_names:
        if cookies.get(name):
            return str(cookies[name])
    # Some FraudNet cookie names are deployment-random.  Prefer a long
    # paypal.com cookie that looks like the vf token if the exact name is not
    # stable in this run.
    for name, val in cookies.items():
        if name in {"KHcl0EuY7AKSMgfvHl7J5E7hPtK", "sc_f", "ddi"}:
            continue
        sval = str(val or "")
        if len(sval) >= 60 and re.fullmatch(r"[A-Za-z0-9_-]+", sval):
            return sval
    return _rand_token(fallback_len)


def _browser_env_payload(*, page_url: str, referer: str = "") -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "connectionData": {"effectiveType": "4g", "rtt": "50", "downlink": "10"},
        "navigator": {
            "appName": "Netscape",
            "appVersion": "5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "cookieEnabled": True,
            "language": "en-US",
            "onLine": True,
            "platform": "Win32",
            "product": "Gecko",
            "productSub": "20030107",
            "userAgent": USER_AGENT,
            "vendor": "Google Inc.",
            "vendorSub": "",
        },
        "screen": {
            "colorDepth": 24,
            "pixelDepth": 24,
            "height": 900,
            "width": 1440,
            "availHeight": 820,
            "availWidth": 1440,
        },
        "window": {
            "outerHeight": 821,
            "outerWidth": 1440,
            "innerHeight": 734,
            "innerWidth": 1440,
            "devicePixelRatio": 2,
        },
        "referer": referer or page_url,
        "URL": page_url,
        "rvr": "3.14.0-FP",
        "tnt": "PP",
        "activeXDefined": False,
        "flashVersion": {"major": 0, "minor": 0, "release": 0},
        # The reference browser ran behind a non-US proxy while keeping a
        # desktop Chrome/Windows fingerprint; this timezone value is accepted
        # by the captured flow and is only telemetry, not account locale.
        "tz": 28800000,
        "tzName": "Asia/Shanghai",
        "dst": True,
        "wit": 2,
        "time": now_ms,
    }


def _paypal_fraudnet_warmup(
    s: Any,
    *,
    ec_token: str,
    signup_url: str,
    ba_token: str,
    timeout: int,
    app_id: str = "CHECKOUTUINODEWEB_ONBOARDING_LITE",
) -> None:
    """Replay the lightweight c.paypal FraudNet side effects seen before signup.

    The successful browser trace does more than GraphQL: after the signup page
    loads it posts p1/p2/w to ``c.paypal.com`` and receives ``sc_f``/``ddi``/
    dynamic verifier cookies for the same EC token.  Missing those cookies
    correlates with opaque ``OAS_ERROR`` at ``createMemberAccount``.
    """
    page_url = signup_url
    pay_referer = f"{PP_ORIGIN}/pay?token={ba_token}&ul=1" if ba_token else signup_url
    headers = _risk_headers(referer="https://www.paypal.com/")

    try:
        s.get(
            f"https://c6.paypal.com/v1/r/d/b/p3?f={urllib.parse.quote(ec_token)}&s={urllib.parse.quote(app_id)}",
            headers={k: v for k, v in headers.items() if k.lower() != "content-type"},
            timeout=max(8, min(timeout, 20)),
        )
    except Exception as e:
        logger.debug("fraudnet p3 soft-failed: %s", e)

    ddi = _risk_cookie_value(s, "ddi", fallback_len=120)
    vf = _risk_cookie_value(s, "KHcl0EuY7AKSMgfvHl7J5E7hPtK", fallback_len=96)
    sc = _risk_cookie_value(s, "sc_f", fallback_len=96)
    base = _browser_env_payload(page_url=page_url, referer=pay_referer)
    p1_payload = {
        **base,
        "trt": False,
        "lst": {"ddiLst": True, "ddi": ddi, "v": None, "vf": vf},
        "pt1": {
            "i": "NaN",
            "pp1": f"{random.randint(4, 12)}.00",
            "cd1": "1.00",
            "tb": 1,
            "sf": "0000",
            "ph1": f"{random.randint(7000, 14000)}.00",
        },
        "asynchk": {
            "ph2": "".join(random.choices("0123456789abcdef", k=64)),
            "o": ["ua", "colorDepth", "width", "tz", "platform", "plugins"],
        },
        "hlb": {
            "wd": True,
            "chromeWSRT": "n/a",
            "plgSize": 5,
            "lgSize": 2,
            "rtt": 50,
        },
        "pkc": {"uvpa": 2, "cma": 1, "cc": 3, "ht": 3, "pkp": 3},
    }
    p1_body = {"appId": app_id, "correlationId": ec_token, "payload": p1_payload}
    try:
        r = s.post("https://c.paypal.com/v1/r/d/b/p1", json=p1_body, headers=headers, timeout=timeout)
        logger.info("fraudnet p1 status=%s cookies=%s", getattr(r, "status_code", "?"), _session_cookie_names(s))
        try:
            data = r.json()
            if isinstance(data, dict):
                sc = data.get("sc") or sc
                ddi = data.get("ddi") or ddi
                vf = data.get("vf") or vf
        except Exception:
            pass
    except Exception as e:
        logger.warning("fraudnet p1 soft-failed: %s", e)

    p2_payload = {
        "URL": page_url,
        "tnt": "PP",
        "data": {
            "plugins": [
                {
                    "mT": [{"t": "application/pdf", "s": "pdf"}, {"t": "text/pdf", "s": "pdf"}],
                    "n": name,
                    "v": "",
                    "fn": "internal-pdf-viewer",
                    "d": "Portable Document Format",
                }
                for name in [
                    "Chrome PDF Viewer",
                    "Chromium PDF Viewer",
                    "Microsoft Edge PDF Viewer",
                    "PDF Viewer",
                    "WebKit built-in PDF",
                ]
            ],
            "cv": {"h": "//GlaGjwAAAAZJREFUAwCRmNE2FwdlIAAAAABJRU5ErkJggg==", "f": 1, "t": "4.00"},
            "vm": {
                "cores": 16,
                "gpu": {
                    "vendor": "Google Inc. (NVIDIA)",
                    "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 650 Ti Direct3D11 vs_5_0 ps_5_0, D3D11-30.0.14.7414)",
                },
                "jsMem": {
                    "usedJSHeapSize": random.randint(35_000_000, 80_000_000),
                    "totalJSHeapSize": random.randint(90_000_000, 140_000_000),
                    "jsHeapSizeLimit": 4_294_967_296,
                },
                "perfNav": {"navigationStart": int(time.time() * 1000) - random.randint(2500, 6500)},
            },
            "fts": int(time.time() * 1000),
        },
        "sc": {"httpCookie": sc, "sc-lst": sc},
        "pvc": 0,
        "pt2": {"pp2": "5.00", "cd2": "1.00", "cp": 1},
    }
    try:
        r = s.post(
            "https://c.paypal.com/v1/r/d/b/p2",
            json={"appId": app_id, "correlationId": ec_token, "payload": p2_payload},
            headers=headers,
            timeout=timeout,
        )
        logger.info("fraudnet p2 status=%s", getattr(r, "status_code", "?"))
    except Exception as e:
        logger.warning("fraudnet p2 soft-failed: %s", e)

    try:
        r = s.post(
            "https://c.paypal.com/v1/r/d/b/w",
            json={
                "appId": app_id,
                "correlationId": ec_token,
                "payload": {
                    "pkc": {"uvpa": 2, "cma": 1, "cc": 3, "ht": 3, "pkp": 3},
                    "slt": random.randint(25, 450),
                    "uvpat": random.randint(25, 450),
                    "cmat": random.randint(25, 450),
                    "capt": 0,
                },
            },
            headers=headers,
            timeout=timeout,
        )
        logger.info("fraudnet w status=%s", getattr(r, "status_code", "?"))
    except Exception as e:
        logger.warning("fraudnet w soft-failed: %s", e)


def _paypal_ddbm2_node_warmup(
    s: Any,
    *,
    signup_url: str,
    ba_token: str,
    timeout: int,
) -> None:
    """Run DataDome ddbm2 tags.js in Node and post the generated jspl body.

    This is the pure-protocol counterpart of the browser's:
      GET  https://ddbm2.paypal.com/tags.js
      POST https://ddbm2.paypal.com/js/  (body: jspl=...)

    The POST response carries a fresh `datadome=...` cookie in JSON; we must copy
    it into the PayPal cookie jar because ddbm2 returns it as a JSON field rather
    than a Set-Cookie header.
    """
    if str(os.environ.get("PPS_SKIP_DDBM2", "")).lower() in {"1", "true", "yes", "on"}:
        return
    node = shutil.which("node")
    if not node:
        logger.warning("ddbm2 node warmup skipped: node not found")
        return
    helper = os.path.join(os.path.dirname(__file__), "ddbm2_node.js")
    if not os.path.exists(helper):
        logger.warning("ddbm2 node warmup skipped: helper missing at %s", helper)
        return

    headers_js = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": signup_url,
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Dest": "script",
    }
    try:
        tags = s.get("https://ddbm2.paypal.com/tags.js", headers=headers_js, timeout=max(8, min(timeout, 20)))
        tags_js = tags.text or ""
        if getattr(tags, "status_code", 0) != 200 or "DataDome" not in tags_js[:1000]:
            logger.warning("ddbm2 tags.js unexpected status=%s len=%d",
                           getattr(tags, "status_code", "?"), len(tags_js))
            return
    except Exception as e:
        logger.warning("ddbm2 tags.js fetch soft-failed: %s", e)
        return

    payload = {
        "tagsJs": tags_js,
        "pageUrl": signup_url,
        "referrer": _paypal_pay_url(ba_token, onboard_url=signup_url) if ba_token else "https://www.paypal.com/",
        "cookie": _session_cookie_header(s) or "datadome=.keep",
        "userAgent": USER_AGENT,
        "ddjsKey": os.environ.get("PPS_DDBM2_DDJSKEY") or "2D56F91C2AD1A8EB7C6A5CA65F5567",
    }
    try:
        node_env = dict(os.environ)
        node_env["NODE_PATH"] = node_env.get("NODE_PATH") or "/app/webui/frontend/node_modules:/usr/local/lib/node_modules"
        proc = subprocess.run(
            [node, helper],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=max(8, min(timeout, 20)),
            check=False,
            env=node_env,
        )
        stdout = (proc.stdout or "").strip()
        if proc.returncode != 0:
            logger.warning("ddbm2 node helper rc=%s stderr=%s", proc.returncode, (proc.stderr or "")[:300])
            return
        out = json.loads(stdout.splitlines()[-1]) if stdout else {}
        body = str(out.get("body") or "")
        if not body.startswith("jspl="):
            logger.warning("ddbm2 node helper produced no jspl: %s", stdout[:300])
            return
    except Exception as e:
        logger.warning("ddbm2 node helper soft-failed: %s", e)
        return

    headers_post = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": PP_ORIGIN,
        "Referer": "https://www.paypal.com/",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        r = s.post("https://ddbm2.paypal.com/js/", data=body, headers=headers_post, timeout=max(8, min(timeout, 20)))
        text = r.text or ""
        cookie_str = ""
        try:
            data = r.json()
            cookie_str = str((data or {}).get("cookie") or "")
        except Exception:
            pass
        m = re.search(r"(?:^|;\s*)datadome=([^;]+)", cookie_str)
        if m:
            dd_val = m.group(1)
            try:
                s.cookies.set("datadome", dd_val, domain=".paypal.com", path="/")
            except Exception:
                pass
            logger.info("ddbm2 /js status=%s body_len=%d datadome_len=%d",
                        getattr(r, "status_code", "?"), len(body), len(dd_val))
        else:
            logger.warning("ddbm2 /js no datadome cookie status=%s body=%s",
                           getattr(r, "status_code", "?"), text[:300])
    except Exception as e:
        logger.warning("ddbm2 /js post soft-failed: %s", e)


def _paypal_fraudnet_field_events(
    s: Any,
    *,
    ec_token: str,
    field_ids: list[str],
    timeout: int,
    app_id: str = "CHECKOUTUINODEWEB_ONBOARDING_LITE",
) -> None:
    """Emit FraudNet field typing events matching the browser signup form."""
    headers = _risk_headers(referer="https://www.paypal.com/")
    headers.pop("Content-Type", None)
    elapsed = random.randint(700, 1400)
    for field_id in field_ids:
        if field_id in {"password", "cardCvv"}:
            ts = (
                f"Di0:{elapsed}Di1:{random.randint(7, 45)}Di2:{random.randint(80, 420)}"
                f"Ui0:{random.randint(20, 45)}Ui1:{random.randint(45, 120)}"
                f"Uh:{random.randint(1200, 6500)}"
            )
        elif field_id == "cardNumber":
            ts = f"Dk91:{elapsed}Di0:{random.randint(120, 320)}Uk91:{random.randint(80, 180)}Uh:{random.randint(1200, 2200)}"
        else:
            ts = f"Dk000:{elapsed}Uk000:{random.randint(4, 13)}Uh:{random.randint(850, 1300)}"
        d = {
            "tsobj": {
                "elid": field_id,
                "sid": app_id,
                "tst": app_id,
                "wsps": False,
                "ts": ts,
                "pf": {"psu": False, "val": False},
            }
        }
        try:
            r = s.get(
                "https://c.paypal.com/v1/r/d/b/w",
                params={
                    "f": ec_token,
                    "s": app_id,
                    "d": json.dumps(d, separators=(",", ":")),
                },
                headers=headers,
                timeout=max(8, min(timeout, 20)),
            )
            logger.info("fraudnet field %-10s status=%s", field_id, getattr(r, "status_code", "?"))
        except Exception as e:
            logger.debug("fraudnet field %s soft-failed: %s", field_id, e)
        elapsed += random.randint(700, 4500)

    # rDT beacon after typing burst, same endpoint as the browser trace.
    try:
        chunks = []
        base = random.randint(8_000, 52_000)
        for _ in range(18):
            a = max(1000, base + random.randint(-28_000, 28_000))
            chunks.append(f"{a},{a-random.randint(80,260)},{a-random.randint(300,650)}")
        r = s.get(
            "https://c.paypal.com/v1/r/d/b/w",
            params={
                "f": ec_token,
                "s": app_id,
                "d": json.dumps({"rDT": ":".join(chunks) + f":{random.randint(9000,26000)},{random.randint(20,80)}"}, separators=(",", ":")),
            },
            headers=headers,
            timeout=max(8, min(timeout, 20)),
        )
        logger.info("fraudnet rDT status=%s", getattr(r, "status_code", "?"))
    except Exception as e:
        logger.debug("fraudnet rDT soft-failed: %s", e)


def _paypal_weasley_log(
    s: Any,
    *,
    ec_token: str,
    signup_url: str,
    event_names: list[str],
    locale_country: str = "US",
    locale_lang: str = "en",
    timeout: int = 30,
    extra_payload: Optional[dict[str, Any]] = None,
) -> None:
    """Emit a small Weasley `/xoplatform/logger` batch.

    The userscript's visible actions (page interactive, field fill, submit,
    OTP modal open/confirm) generate these client-side logger beacons before
    each GraphQL mutation.  They are not decisive API calls, so keep them
    best-effort, but preserving the order helps the pure-protocol replay match
    the browser flow without launching a browser.
    """
    now = int(time.time() * 1000)
    locale = f"{locale_lang}_{locale_country}"
    events: list[dict[str, Any]] = []
    for i, name in enumerate(event_names):
        payload: dict[str, Any] = {
            "clientCountry": locale_country,
            "clientLocale": locale,
            "clientTimestamp": now + i,
            "timestamp": str(now + i),
            "token": ec_token,
        }
        if extra_payload:
            payload.update(extra_payload)
        events.append({"level": "info", "event": name, "payload": payload})
    if not events:
        return
    body = {
        "events": events,
        "meta": {
            "integrationData": {
                "contextId": ec_token,
                "contextType": ec_token,
                "integrationMethod": "FULLPAGE",
                "integrationType": "EC",
            }
        },
        "tracking": [],
        "metrics": [],
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": PP_ORIGIN,
        "Referer": signup_url,
        "X-Requested-With": "fetch",
        "X-App-Name": "checkoutuinodeweb_weasley",
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Full-Version-List": '"Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Model": '""',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Arch": '"x86"',
        "Sec-CH-Device-Memory": "8",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        r = s.post(
            f"{PP_ORIGIN}/xoplatform/logger/api/logger/",
            json=body,
            headers=headers,
            timeout=max(8, min(timeout, 20)),
        )
        logger.info("weasley logger events=%d status=%s", len(events), getattr(r, "status_code", "?"))
    except Exception as e:
        logger.debug("weasley logger soft-failed: %s", e)


def _browser_warm_signup_form(
    s: Any,
    *,
    signup_url: str,
    variables: dict[str, Any],
    proxy: Optional[str],
    user_data_dir: Optional[str],
    timeout_ms: int = 45000,
) -> dict[str, str]:
    """Use Camoufox to fill (not submit) the PayPal signup form.

    This is an observability/warm-up bridge: the decisive account creation is
    still the GraphQL replay below, but the browser gets to emit the same
    ddbm2/FraudNet/Tealeaf field telemetry that the userscript generated.
    """
    try:
        from camoufox.sync_api import Camoufox  # type: ignore
        from browserforge.fingerprints import Screen  # type: ignore
    except Exception as e:
        logger.warning("browser form warmup unavailable: %s", e)
        return {}

    cf_proxy = _camoufox_proxy_kwargs(proxy)
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    kwargs: dict[str, Any] = {
        "headless": not has_display,
        "humanize": False,
        "os": "windows",
        "screen": Screen(max_width=1920, max_height=1080),
        "geoip": bool(proxy),
        "locale": "en-US",
    }
    if cf_proxy:
        kwargs["proxy"] = cf_proxy
    if user_data_dir:
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = user_data_dir

    card = variables.get("card") or {}
    billing = variables.get("billingAddress") or {}
    phone = variables.get("phone") or {}
    expiry_for_ui = str(card.get("expirationDate") or "").replace("/", " / ")
    if re.fullmatch(r"\d{2} / \d{4}", expiry_for_ui):
        expiry_for_ui = expiry_for_ui[:5] + expiry_for_ui[-2:]

    fill_map = {
        "email": variables.get("email") or "",
        "phone": str(phone.get("number") or ""),
        "cardNumber": str(card.get("cardNumber") or ""),
        "cardExpiry": expiry_for_ui,
        "cardCvv": str(card.get("securityCode") or ""),
        "password": variables.get("password") or "",
        "firstName": variables.get("firstName") or "",
        "lastName": variables.get("lastName") or "",
        "billingLine1": billing.get("line1") or "",
        "billingCity": billing.get("city") or "",
        "billingPostalCode": billing.get("postalCode") or "",
    }
    cookies_out: dict[str, str] = {}
    try:
        with Camoufox(**kwargs) as launched:
            if hasattr(launched, "new_context"):
                ctx = launched.new_context()
            else:
                ctx = launched
            try:
                cur_cookies = _session_cookie_dict(s)
                if cur_cookies:
                    ctx.add_cookies([
                        {
                            "name": str(k),
                            "value": str(v),
                            "domain": ".paypal.com",
                            "path": "/",
                            "secure": True,
                        }
                        for k, v in cur_cookies.items()
                        if k and v is not None
                    ])
            except Exception as e:
                logger.debug("browser form warmup add_cookies soft-failed: %s", e)
            page = (ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page())
            logger.info("browser form warmup: goto signup %s", signup_url[:140])
            page.goto(signup_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3500)

            fill_script = """
            ([id, value]) => {
              const sels = [
                '#' + CSS.escape(id),
                'input[name="' + id + '"]',
                'input[autocomplete="' + id + '"]'
              ];
              let el = null;
              for (const sel of sels) {
                try { el = document.querySelector(sel); } catch (_) {}
                if (el) break;
              }
              if (!el) return false;
              const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const desc = Object.getOwnPropertyDescriptor(proto, 'value');
              try { el.focus(); } catch (_) {}
              if (desc && desc.set) desc.set.call(el, value); else el.value = value;
              for (const ev of ['keydown', 'input', 'keyup', 'change', 'blur']) {
                try { el.dispatchEvent(new Event(ev, {bubbles: true})); } catch (_) {}
              }
              return true;
            }
            """
            for fid, val in fill_map.items():
                if not val:
                    continue
                try:
                    ok = page.evaluate(fill_script, [fid, str(val)])
                    logger.info("browser form warmup fill %-18s %s", fid, "ok" if ok else "miss")
                    page.wait_for_timeout(random.randint(250, 750))
                except Exception as e:
                    logger.debug("browser form warmup fill %s soft-failed: %s", fid, e)
            state = billing.get("state") or ""
            if state:
                try:
                    ok = page.evaluate(
                        """(value) => {
                          const el = document.querySelector('#billingState, select[name="billingState"], #billingAdministrativeArea');
                          if (!el) return false;
                          el.value = value;
                          el.dispatchEvent(new Event('change', {bubbles:true}));
                          el.dispatchEvent(new Event('blur', {bubbles:true}));
                          return true;
                        }""",
                        state,
                    )
                    logger.info("browser form warmup state %s", "ok" if ok else "miss")
                except Exception:
                    pass
            # Dwell so FraudNet/Tealeaf/ddbm2 has time to flush event counters.
            page.wait_for_timeout(7000)
            try:
                cookie_list = ctx.cookies()
            except Exception:
                cookie_list = []
            for c in cookie_list:
                if "paypal.com" in c.get("domain", ""):
                    cookies_out[c["name"]] = c["value"]
            logger.info("browser form warmup cookies=%d names=%s", len(cookies_out), sorted(cookies_out.keys()))
    except Exception as e:
        logger.warning("browser form warmup soft-failed: %s", e)
        try:
            # Best-effort breadcrumbs for later inspection.
            pass
        except Exception:
            pass
    return cookies_out


def _resolve_google_like_address(
    s: Any,
    *,
    signup_url: str,
    ec_token: str,
    locale_country: str,
    locale_lang: str,
    timeout: int,
) -> Optional[dict[str, Any]]:
    """Ask PayPal's autocomplete endpoint for a real US place, like the UI."""
    if locale_country.upper() != "US":
        return None
    session_id = _rand_word(12)
    suggestion = None
    # The reference trace typed "as" then "asd"; use the same prefix family.
    for prefix in ("as", "asd"):
        try:
            resp = _gql(
                s,
                "AddressAutocompleteQuery",
                {
                    "count": 4,
                    "countries": [locale_country],
                    "input": prefix,
                    "language": locale_lang,
                    "radius": 1500,
                    "sessionId": session_id,
                },
                Q_ADDRESS_AUTOCOMPLETE,
                signup_url=signup_url,
                timeout=timeout,
            )
            suggestions = (((resp.get("data") or {}).get("addressAutoComplete") or {}).get("suggestions") or [])
            if suggestions:
                suggestion = suggestions[0]
        except Exception as e:
            logger.warning("AddressAutocomplete soft-failed input=%s: %s", prefix, e)
    if not suggestion or not suggestion.get("placeId"):
        return None
    try:
        resp = _gql(
            s,
            "AddressFromAutocompletePlaceIdQuery",
            {
                "language": locale_lang,
                "placeId": suggestion["placeId"],
                "sessionId": session_id,
            },
            Q_ADDRESS_FROM_PLACE,
            signup_url=signup_url,
            timeout=timeout,
        )
        addr = (((resp.get("data") or {}).get("addressFromAutoCompletePlaceId") or {}).get("address") or {})
    except Exception as e:
        logger.warning("AddressFromAutocomplete soft-failed: %s", e)
        return None
    if not addr.get("line1") or not addr.get("postalCode"):
        return None
    out = {
        "country": addr.get("country") or locale_country,
        "line1": addr.get("line1") or "",
        "city": addr.get("city") or "",
        "state": addr.get("state") or "",
        "postalCode": addr.get("postalCode") or "",
        "autoCompleteType": "GOOGLE",
        "isUserModified": False,
    }
    logger.info(
        "address autocomplete selected: %s, %s %s",
        out["line1"],
        out["city"],
        out["state"],
    )
    return out


def _decode_otp_login_context(deferred_resp: dict[str, Any]) -> dict[str, Any]:
    raw = (((deferred_resp.get("data") or {}).get("otpLoginContext") or {}).get("context") or "")
    if not raw:
        return {}
    try:
        padded = raw + ("=" * (-len(raw) % 4))
        return json.loads(base64.b64decode(padded).decode("utf-8"))
    except Exception as e:
        logger.warning("otpLoginContext decode failed: %s", e)
        return {}


def _idapps_get_otp_challenge(
    s: Any,
    *,
    signup_url: str,
    ec_token: str,
    email: str,
    otp_context: dict[str, Any],
    timeout: int,
) -> str:
    """Replay the idapps getOtpChallenge warm-up from the browser flow."""
    ctx_id = otp_context.get("ctxId") or ""
    csrf_nonce = otp_context.get("csrfNonce") or ""
    if not ctx_id or not csrf_nonce:
        return ""
    fn_sync = _paypal_fn_sync_data(ec_token, include_d=False)
    r_data = urllib.parse.quote(json.dumps({"fn_sync_data": fn_sync}, separators=(",", ":")))
    body = {
        "operationName": "getOtpChallengeOperation",
        "query": "",
        "csrfNonce": csrf_nonce,
        "variables": {
            "clientInfo": {
                "fnId": ec_token,
                "ctxId": ctx_id,
                "rData": r_data,
            },
            "credentials": {
                "credentialValue": email,
                "credentialType": "EMAIL",
            },
            "challengeInfo": {"autoSmsOtp": False},
        },
        "fn_sync_data": fn_sync,
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Cache-Control": "max-age=0",
        "Accept": "*/*",
        "Content-Type": "application/json",
        "Origin": PP_ORIGIN,
        "Referer": signup_url,
        "X-Requested-With": "fetch",
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Full-Version-List": '"Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Model": '""',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Arch": '"x86"',
        "Sec-CH-Device-Memory": "8",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        r = s.post(f"{PP_ORIGIN}/idapps/graphql", json=body, headers=headers, timeout=timeout)
        text = r.text or ""
        logger.info("idapps getOtpChallenge status=%s len=%d", r.status_code, len(text))
        try:
            with open("/tmp/pps_idapps_getOtpChallenge_last.html", "w", encoding="utf-8") as f:
                f.write(text)
            with open("/tmp/pps_idapps_getOtpChallenge_last.json", "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "url": f"{PP_ORIGIN}/idapps/graphql",
                        "status_code": r.status_code,
                        "request_headers": headers,
                        "cookie_names": _session_cookie_names(s),
                        "request": body,
                        "response_head": text[:500],
                        "response_kind": (
                            "json" if text.lstrip().startswith("{")
                            else "html" if text.lstrip().startswith("<")
                            else "text"
                        ),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass
        return text if r.status_code == 200 else ""
    except Exception as e:
        logger.warning("idapps getOtpChallenge soft-failed: %s", e)
        return ""


def _html_input_value(html: str, name: str) -> str:
    m = re.search(
        r'name=["\']' + re.escape(name) + r'["\'][^>]*value=["\']([^"\']*)',
        html or "",
        re.I,
    )
    return html_lib.unescape(m.group(1)) if m else ""


def _html_attr_value(html: str, attr: str) -> str:
    m = re.search(r'\b' + re.escape(attr) + r'=["\']([^"\']*)', html or "", re.I)
    return html_lib.unescape(m.group(1)) if m else ""


def _extract_recaptcha_iframe_src(challenge_html: str) -> str:
    m = re.search(
        r'<iframe[^>]+src=["\']([^"\']*recaptcha/recaptcha_v[23]\.html[^"\']*)',
        challenge_html or "",
        re.I,
    )
    if m:
        return _unescape_url(m.group(1))
    # Newer PayPal auth pages sometimes expose the passive Enterprise v3 bridge
    # through a script data-src rather than an iframe tag.
    m = re.search(
        r'\bdata-src=["\']([^"\']*grcenterprise_v3_static\.html[^"\']*)',
        challenge_html or "",
        re.I,
    )
    return _unescape_url(m.group(1)) if m else ""


def _extract_recaptcha_site_key(challenge_html: str, iframe_src: str = "") -> str:
    candidates = [
        _first_query_value(iframe_src, "siteKey"),
        _first_query_value(iframe_src, "sitekey"),
        _html_input_value(challenge_html, "_adsRecaptchaSiteKey"),
        _html_attr_value(challenge_html, "data-sitekey"),
        _html_attr_value(challenge_html, "data-site-key"),
    ]
    for c in candidates:
        if c:
            return c
    m = re.search(r'\bsiteKey=([^"&\s<>]+)', challenge_html or "", re.I)
    return html_lib.unescape(m.group(1)) if m else ""


def _extract_hcaptcha_passive_iframe_src(challenge_html: str) -> str:
    m = re.search(
        r'<iframe[^>]+src=["\']([^"\']*hcaptcha/hcaptchapassive(?:_eval)?\.html[^"\']*)',
        challenge_html or "",
        re.I,
    )
    return _unescape_url(m.group(1)) if m else ""


def _extract_hcaptcha_site_key(challenge_html: str, iframe_src: str = "") -> str:
    candidates = [
        _first_query_value(iframe_src, "siteKey"),
        _first_query_value(iframe_src, "sitekey"),
        _html_attr_value(challenge_html, "data-sitekey"),
        _html_attr_value(challenge_html, "data-site-key"),
    ]
    for c in candidates:
        if c:
            return c
    m = re.search(r'\bsiteKey=([0-9a-f-]{20,})', challenge_html or "", re.I)
    return html_lib.unescape(m.group(1)) if m else ""


def _authchallenge_captcha_type(challenge_html: str) -> str:
    return (_html_attr_value(challenge_html, "data-captcha-type") or "").strip().lower()


def _captcha_gateway_config(prefix: str = "PPS_PAYPAL") -> tuple[str, str]:
    """Return (api_url, api_key) for createTask/getTaskResult-compatible gateway.

    The PayPal no-card flow is supposed to remain protocol-only.  Browser
    fallback is therefore not the default escape hatch; a captcha gateway is
    still just HTTP from this process' point of view and mirrors the existing
    Team/Stripe pure-protocol captcha integration.
    """
    api_key = (
        os.environ.get(f"{prefix}_CAPTCHA_API_KEY")
        or os.environ.get(f"{prefix}_CAPTCHA_CLIENT_KEY")
        or os.environ.get("CTF_CAPTCHA_API_KEY")
        or os.environ.get("CTF_CAPTCHA_CLIENT_KEY")
        or os.environ.get("CAPTCHA_API_KEY")
        or os.environ.get("CAPTCHA_CLIENT_KEY")
        or ""
    ).strip()
    api_url = (
        os.environ.get(f"{prefix}_CAPTCHA_API_URL")
        or os.environ.get("CTF_CAPTCHA_API_URL")
        or os.environ.get("CAPTCHA_API_URL")
        or ""
    ).strip().rstrip("/")
    if "YOUR_CAPTCHA_PROVIDER" in api_url:
        api_url = ""
    if api_key in {"YOUR_CAPTCHA_API_KEY", "YOUR_CLIENT_KEY", "YOUR_CAPTCHA_CLIENT_KEY"}:
        api_key = ""
    return api_url, api_key


def _captcha_proxy_fields(proxy: Optional[str]) -> dict[str, Any]:
    """Translate the current checkout proxy to createTask proxy fields.

    Some hCaptcha/reCAPTCHA providers bind high-risk PayPal tokens to the
    solving IP.  Keep Proxyless as a fallback, but when the caller already has
    a working relay/proxy, let compatible gateways try the same egress first.
    """
    raw = (proxy or os.environ.get("PPS_PAYPAL_CAPTCHA_PROXY") or "").strip()
    if not raw:
        return {}
    try:
        u = urllib.parse.urlparse(raw)
    except Exception:
        return {}
    if not u.hostname or not u.port:
        return {}
    scheme = (u.scheme or "").lower()
    if scheme in {"socks5h", "socks5"}:
        proxy_type = "socks5"
    elif scheme in {"http", "https"}:
        proxy_type = "http"
    else:
        return {}
    out: dict[str, Any] = {
        "proxyType": proxy_type,
        "proxyAddress": u.hostname,
        "proxyPort": int(u.port),
    }
    if u.username:
        out["proxyLogin"] = urllib.parse.unquote(u.username)
    if u.password:
        out["proxyPassword"] = urllib.parse.unquote(u.password)
    return out


def _poll_captcha_gateway(
    *,
    task: dict[str, Any],
    timeout: int,
    label: str,
    token_fields: tuple[str, ...] = ("gRecaptchaResponse", "token"),
) -> tuple[str, dict[str, Any]]:
    """Submit a captcha task to the configured HTTP gateway and poll the token."""
    api_url, api_key = _captcha_gateway_config()
    if not api_url or not api_key:
        logger.info("%s: no pure HTTP captcha provider configured", label)
        return "", {}

    try:
        create = requests.post(
            f"{api_url}/createTask",
            json={"clientKey": api_key, "task": task},
            timeout=20,
        )
        cdata = create.json()
    except Exception as e:
        logger.warning("%s createTask failed: %s", label, e)
        return "", {}
    if cdata.get("errorId"):
        logger.warning("%s createTask rejected: %s", label, cdata.get("errorDescription") or cdata)
        return "", {}
    task_id = cdata.get("taskId")
    if not task_id:
        logger.warning("%s createTask returned no taskId: %s", label, cdata)
        return "", {}

    deadline = time.time() + max(30, timeout)
    while time.time() < deadline:
        time.sleep(3)
        try:
            poll = requests.post(
                f"{api_url}/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
                timeout=15,
            )
            pdata = poll.json()
        except Exception:
            continue
        if pdata.get("errorId"):
            logger.warning("%s getTaskResult rejected: %s", label, pdata.get("errorDescription") or pdata)
            return "", pdata
        if pdata.get("status") != "ready":
            continue
        sol = pdata.get("solution") or {}
        for field in token_fields:
            token = sol.get(field) or pdata.get(field)
            if token:
                logger.info("%s token ready len=%d taskId=%s", label, len(str(token)), task_id)
                return str(token), sol if isinstance(sol, dict) else {}
        logger.warning("%s ready but no token fields: %s", label, pdata)
        return "", sol if isinstance(sol, dict) else {}
    logger.warning("%s token timeout taskId=%s", label, task_id)
    return "", {}


def _extract_auth_fpti(challenge_html: str) -> dict[str, str]:
    """Extract PayPal analytics fpti query fields from authchallenge HTML."""
    m = re.search(r"PAYPAL\.analytics\.setup\(\{data:'([^']+)'", challenge_html or "", re.I)
    if not m:
        return {}
    try:
        return {str(k): str(v) for k, v in urllib.parse.parse_qsl(m.group(1), keep_blank_values=True)}
    except Exception:
        return {}


def _paypal_auth_logclientdata(
    s: Any,
    *,
    challenge_html: str,
    csrf: str,
    session_id: str,
    ec_token: str,
    captcha_state: str,
    signup_url: str,
    timeout: int,
) -> None:
    """Replay /auth/logclientdata captcha-state telemetry.

    The successful capture posts these state transitions around the v3 solve;
    omitting them consistently makes /auth/validatecaptcha escalate to the v2
    HTML page in this environment.
    """
    fpti = _extract_auth_fpti(challenge_html)
    if not fpti:
        fpti = {
            "pgrp": "main:authchallenge::checkoutweb:signup",
            "page": "main:authchallenge::checkoutweb:signup",
            "qual": "",
            "pgtf": "Nodejs",
            "s": "ci",
            "env": "live",
            "comp": "checkoutuinodeweb",
            "tsrce": "xorouternodeweb",
            "cu": "1",
            "ef_policy": "gdpr_v2.1",
            "c_prefs": "T=1,P=1,F=1,type=explicit_banner",
            "pxpguid": (_session_cookie_dict(s).get("ts_c") or _rand_token(32))[:64],
            "pgst": str(int(time.time() * 1000)),
            "calc": "".join(random.choices("0123456789abcdef", k=13)),
            "csci": "".join(random.choices("0123456789abcdef", k=32)),
            "nsid": session_id,
            "rsta": "en_US",
            "ccpg": "US",
        }
    fpti.update({
        "pgrp": fpti.get("pgrp") or "main:authchallenge::checkoutweb:signup",
        "page": fpti.get("page") or "main:authchallenge::checkoutweb:signup",
        "comp": fpti.get("comp") or "checkoutuinodeweb",
        "tsrce": fpti.get("tsrce") or "xorouternodeweb",
        "flnm": "Weasley",
        "fltk": ec_token,
        "captchaState": captcha_state,
        "nsid": fpti.get("nsid") or session_id,
        "rsta": fpti.get("rsta") or "en_US",
        "ccpg": fpti.get("ccpg") or "US",
    })
    cookies = _session_cookie_dict(s)
    ga = (cookies.get("_ga") or "")
    if ga.startswith("GA") and "." in ga:
        parts = ga.split(".")
        if len(parts) >= 4:
            fpti.setdefault("gacook", ".".join(parts[-2:]))
    if captcha_state != "CLIENT_SIDE_RECAPTCHAV3_SERVED":
        fpti.setdefault("message", "")
    if captcha_state == "CLIENT_SIDE_PPCAPTCHA_SOLVED":
        fpti.setdefault("adsCaptcha", "explicit")
    body = {"fpti": fpti, "_csrf": csrf, "_sessionID": session_id}
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": PP_ORIGIN,
        "Referer": signup_url,
        "X-Requested-With": "fetch",
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Full-Version-List": '"Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Model": '""',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Arch": '"x86"',
        "Sec-CH-Device-Memory": "8",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        r = s.post(f"{PP_ORIGIN}/auth/logclientdata", json=body, headers=headers, timeout=timeout)
        logger.info("auth logclientdata %-45s status=%s", captcha_state, getattr(r, "status_code", "?"))
    except Exception as e:
        logger.debug("auth logclientdata %s soft-failed: %s", captcha_state, e)


def _camoufox_proxy_kwargs(proxy: Optional[str]) -> Optional[dict[str, str]]:
    if not proxy:
        return None
    try:
        u = urllib.parse.urlparse(proxy)
        if u.scheme in ("socks5", "socks5h") and u.username:
            return {"server": f"socks5://{u.hostname}:{u.port}"}
        return {
            "server": f"{u.scheme}://{u.hostname}:{u.port}",
            "username": u.username or "",
            "password": u.password or "",
        }
    except Exception:
        return None


def _playwright_proxy_kwargs(proxy: Optional[str]) -> Optional[dict[str, str]]:
    if not proxy:
        return None
    try:
        u = urllib.parse.urlparse(proxy)
        server_scheme = "socks5" if u.scheme in ("socks5", "socks5h") else u.scheme
        out = {"server": f"{server_scheme}://{u.hostname}:{u.port}"}
        if u.username:
            out["username"] = urllib.parse.unquote(u.username)
        if u.password:
            out["password"] = urllib.parse.unquote(u.password)
        return out
    except Exception:
        return None


def _session_cookie_dict(s: Any) -> dict[str, str]:
    """Best-effort cookie snapshot from requests/curl_cffi sessions."""
    jar = getattr(s, "cookies", None)
    if jar is None:
        return {}
    try:
        return {str(k): str(v) for k, v in (jar.get_dict() or {}).items()}
    except Exception:
        pass
    out: dict[str, str] = {}
    try:
        for c in jar:
            name = getattr(c, "name", "")
            value = getattr(c, "value", "")
            if name:
                out[str(name)] = str(value)
    except Exception:
        pass
    return out


def _session_cookie_header(s: Any) -> str:
    cookies = _session_cookie_dict(s)
    if not cookies:
        return ""
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v is not None)


def _mint_recaptcha_token_via_chromium(
    iframe_src: str,
    *,
    proxy: Optional[str],
    parent_url: str,
    paypal_cookies: Optional[dict[str, str]],
    timeout_ms: int,
) -> tuple[str, dict[str, Any]]:
    """Mint PayPal recaptcha token in Playwright Chromium.

    Camoufox is excellent for DataDome, but PayPal's reCAPTCHA Enterprise v3
    currently scores Firefox/Camoufox low on this flow and escalates to v2.
    The reference trace is Chrome 146, so try Chromium first for the token.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception:
        return "", {}

    bridge_url = parent_url or "https://www.paypal.com/recaptcha-bridge"
    iframe_json = json.dumps(iframe_src)
    bridge_html = f"""<!doctype html><html><body>
<script>
window.__pp_msgs = [];
window.addEventListener('message', function(e) {{
  window.__pp_msgs.push(e.data);
  try {{
    document.documentElement.setAttribute('data-pp-msgs', JSON.stringify(window.__pp_msgs));
  }} catch (_) {{}}
}});
</script>
<iframe id="r" referrerpolicy="origin" src={iframe_json} width="800" height="600"></iframe>
</body></html>"""
    pw_proxy = _playwright_proxy_kwargs(proxy)
    launch_kwargs: dict[str, Any] = {
        # Chromium headless shell is the browser Playwright installed in this
        # container and reliably executes recaptcha enterprise; headed Chrome
        # under xvfb was observed to stall before iframe messages arrive.
        "headless": True,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    }
    if pw_proxy:
        launch_kwargs["proxy"] = pw_proxy
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_kwargs)
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
                timezone_id="America/Chicago",
                java_script_enabled=True,
            )
            ctx.add_init_script(
                "try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (_) {}"
            )
            if paypal_cookies:
                ctx.add_cookies([
                    {
                        "name": str(k),
                        "value": str(v),
                        "domain": ".paypal.com",
                        "path": "/",
                        "secure": True,
                    }
                    for k, v in paypal_cookies.items()
                    if k and v is not None
                ])
            page = ctx.new_page()

            def _fulfill_bridge(route: Any) -> None:
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    headers={"referrer-policy": "origin"},
                    body=bridge_html,
                )

            page.route(bridge_url, _fulfill_bridge)
            if "/checkoutweb/signup" in bridge_url:
                page.route("https://www.paypal.com/checkoutweb/signup**", _fulfill_bridge)
            page.goto(bridge_url, wait_until="domcontentloaded", timeout=timeout_ms)
            deadline = time.time() + timeout_ms / 1000.0
            last_msgs: list[Any] = []
            while time.time() < deadline:
                try:
                    msgs = page.evaluate(
                        """() => {
                          const raw = document.documentElement.getAttribute('data-pp-msgs') || '[]';
                          try { return JSON.parse(raw); } catch (_) { return []; }
                        }"""
                    ) or []
                except Exception:
                    msgs = []
                if msgs != last_msgs:
                    last_msgs = msgs
                for raw in msgs:
                    try:
                        obj = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        continue
                    token = (obj or {}).get("token") if isinstance(obj, dict) else ""
                    if token and token not in {"NOT_REACHABLE", "RENDER_FAILURE"}:
                        browser.close()
                        return token, (obj.get("renderData") or {})
                page.wait_for_timeout(750)
            logger.warning("recaptcha chromium timeout; msgs=%r", last_msgs[-3:])
            browser.close()
    except Exception as e:
        logger.warning("recaptcha chromium failed: %s", e)
    return "", {}


def _mint_recaptcha_v3_token_via_camoufox(
    iframe_src: str,
    *,
    proxy: Optional[str],
    parent_url: str = "",
    paypal_cookies: Optional[dict[str, str]] = None,
    user_data_dir: Optional[str] = None,
    timeout_ms: int = 45000,
) -> tuple[str, dict[str, Any]]:
    """Use a real browser to execute PayPal's recaptcha_v3 bridge iframe."""
    if not iframe_src:
        return "", {}
    token, render = _mint_recaptcha_token_via_chromium(
        iframe_src,
        proxy=proxy,
        parent_url=parent_url,
        paypal_cookies=paypal_cookies,
        timeout_ms=timeout_ms,
    )
    if token:
        return token, render
    from camoufox.sync_api import Camoufox  # type: ignore
    from browserforge.fingerprints import Screen  # type: ignore

    cf_proxy = _camoufox_proxy_kwargs(proxy)
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    kwargs: dict[str, Any] = {
        # Use headed mode when xvfb is available.  PayPal's v3 path escalates
        # low-score tokens to a visible v2 challenge; headed Camoufox under
        # xvfb matches the successful seed browser more closely than headless.
        "headless": not has_display,
        "humanize": False,
        "os": "windows",
        "screen": Screen(max_width=1920, max_height=1080),
        # Keep using the caller's proxy for network traffic, but avoid
        # Camoufox's pre-launch public-IP lookup.  The local gost relay is
        # intentionally short-lived in this pipeline; if that lookup races the
        # relay startup/shutdown, Camoufox raises InvalidIP before the browser
        # even opens.  recaptcha only needs the browser traffic to go through
        # the proxy, not Camoufox-side timezone/geo inference.
        "geoip": False,
        "locale": "en-US",
    }
    if cf_proxy:
        kwargs["proxy"] = cf_proxy
    if user_data_dir:
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = user_data_dir

    bridge_url = parent_url or "https://www.paypal.com/recaptcha-bridge"
    iframe_json = json.dumps(iframe_src)
    bridge_html = f"""<!doctype html><html><body>
<script>
window.__pp_msgs = [];
window.addEventListener('message', function(e) {{
  window.__pp_msgs.push(e.data);
  try {{
    document.documentElement.setAttribute('data-pp-msgs', JSON.stringify(window.__pp_msgs));
  }} catch (_) {{}}
  console.log('[recaptcha-msg]', e.data);
}});
</script>
<iframe id="r" referrerpolicy="origin" src={iframe_json} width="800" height="600"></iframe>
</body></html>"""

    try:
        with Camoufox(**kwargs) as launched:
            if hasattr(launched, "new_context"):
                ctx = launched.new_context()
            else:
                ctx = launched
            if paypal_cookies:
                try:
                    ctx.add_cookies([
                        {
                            "name": str(k),
                            "value": str(v),
                            "domain": ".paypal.com",
                            "path": "/",
                            "secure": True,
                        }
                        for k, v in paypal_cookies.items()
                        if k and v is not None
                    ])
                except Exception as e:
                    logger.warning("recaptcha bridge add_cookies soft-failed: %s", e)
            page = (ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page())
            def _fulfill_bridge(route: Any) -> None:
                route.fulfill(
                    status=200,
                    content_type="text/html",
                    headers={"referrer-policy": "origin"},
                    body=bridge_html,
                )

            # Route both the exact parent URL and checkoutweb/signup wildcard.
            # Using the real signup URL as the parent gives the iframe the same
            # referrer shape as PayPal's browser flow, while still letting us
            # inject a tiny bridge document.
            page.route(bridge_url, _fulfill_bridge)
            if "/checkoutweb/signup" in bridge_url:
                page.route("https://www.paypal.com/checkoutweb/signup**", _fulfill_bridge)
            page.goto(bridge_url, wait_until="domcontentloaded", timeout=timeout_ms)
            deadline = time.time() + timeout_ms / 1000.0
            last_msgs: list[Any] = []
            while time.time() < deadline:
                try:
                    # Camoufox/Firefox evaluates in an isolated world where
                    # page-world globals such as window.__pp_msgs may be
                    # invisible.  Mirror messages to a DOM attribute in the
                    # bridge listener and read that attribute from here.
                    msgs = page.evaluate(
                        """() => {
                          const raw = document.documentElement.getAttribute('data-pp-msgs') || '[]';
                          try { return JSON.parse(raw); } catch (_) { return []; }
                        }"""
                    ) or []
                except Exception:
                    try:
                        msgs = page.evaluate(
                            "() => (window.wrappedJSObject && window.wrappedJSObject.__pp_msgs) || []"
                        ) or []
                    except Exception:
                        msgs = []
                if msgs != last_msgs:
                    last_msgs = msgs
                for raw in msgs:
                    try:
                        obj = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        continue
                    token = (obj or {}).get("token") if isinstance(obj, dict) else ""
                    if token and token not in {"NOT_REACHABLE", "RENDER_FAILURE"}:
                        return token, (obj.get("renderData") or {})
                page.wait_for_timeout(750)
            logger.warning("recaptcha bridge timeout; msgs=%r", last_msgs[-3:])
    except Exception as e:
        logger.warning("recaptcha bridge failed: %s", e)
    return "", {}


def _solve_recaptcha_v3_token_protocol(
    *,
    site_key: str,
    page_url: str,
    action: str,
    timeout: int,
    proxy: Optional[str] = None,
) -> str:
    """Obtain PayPal reCAPTCHA Enterprise v3 token without page automation."""
    manual = (os.environ.get("PPS_PAYPAL_RECAPTCHA_TOKEN") or os.environ.get("PPS_PAYPAL_GRC_TOKEN") or "").strip()
    if manual:
        logger.info("recaptcha: using pre-supplied PPS_PAYPAL_RECAPTCHA_TOKEN len=%d", len(manual))
        return manual
    if not site_key:
        return ""
    proxy_fields = _captcha_proxy_fields(proxy)
    task_type = "RecaptchaV3EnterpriseTask" if proxy_fields else "RecaptchaV3EnterpriseTaskProxyless"
    task = {
        "type": task_type,
        "websiteURL": page_url or PP_ORIGIN,
        "websiteKey": site_key,
        "pageAction": action or "default",
        "isEnterprise": True,
        "userAgent": USER_AGENT,
    }
    task.update(proxy_fields)
    token, _solution = _poll_captcha_gateway(
        task=task,
        timeout=timeout,
        label="recaptcha",
        token_fields=("gRecaptchaResponse", "token"),
    )
    return token


def _validate_paypal_recaptcha(
    s: Any,
    *,
    challenge_html: str,
    signup_url: str,
    proxy: Optional[str],
    user_data_dir: Optional[str] = None,
    timeout: int,
) -> bool:
    """Replay /auth/validatecaptcha using protocol-supplied grcV3 token."""
    csrf = _html_input_value(challenge_html, "_csrf")
    request_id = _html_input_value(challenge_html, "_requestId")
    hsh = _html_input_value(challenge_html, "_hash")
    session_id = _html_input_value(challenge_html, "_sessionID")
    jse = _html_attr_value(challenge_html, "data-jse")
    iframe_src = _extract_recaptcha_iframe_src(challenge_html)
    site_key = _extract_recaptcha_site_key(challenge_html, iframe_src)
    action = _first_query_value(iframe_src, "action") or _html_attr_value(challenge_html, "data-action") or "default"
    ec_token = ""
    try:
        m_ec = _EC_RE.search(signup_url or "")
        ec_token = m_ec.group(1) if m_ec else (_extract_auth_fpti(challenge_html).get("fltk") or "")
    except Exception:
        ec_token = ""
    if not all([csrf, request_id, hsh, session_id, jse, iframe_src]):
        logger.warning(
            "validatecaptcha fields missing csrf=%s request=%s hash=%s session=%s jse=%s iframe=%s",
            bool(csrf), bool(request_id), bool(hsh), bool(session_id), bool(jse), bool(iframe_src),
        )
        return False
    for state in ("CLIENT_SIDE_RECAPTCHAV3_SERVED", "CLIENT_SIDE_RECAPTCHAV3_ENTERPRISE_API_JS_LOADED"):
        _paypal_auth_logclientdata(
            s,
            challenge_html=challenge_html,
            csrf=csrf,
            session_id=session_id,
            ec_token=ec_token,
            captcha_state=state,
            signup_url=signup_url,
            timeout=timeout,
        )
    token = _solve_recaptcha_v3_token_protocol(
        site_key=site_key,
        page_url=iframe_src or signup_url,
        action=action,
        timeout=max(60, timeout * 3),
        proxy=proxy,
    )
    render: dict[str, Any] = {}
    allow_browser_recaptcha = (
        str(os.environ.get("PPS_ALLOW_BROWSER_RECAPTCHA", "")).lower() in {"1", "true", "yes", "on"}
        or str(os.environ.get("PPS_PURE_PROTOCOL", "")).lower() not in {"1", "true", "yes", "on"}
    )
    is_visible_v2 = "recaptcha_v2.html" in (iframe_src or "")
    if not token and allow_browser_recaptcha and not is_visible_v2:
        token, render = _mint_recaptcha_v3_token_via_camoufox(
            iframe_src,
            proxy=proxy,
            parent_url=signup_url,
            paypal_cookies=_session_cookie_dict(s),
            user_data_dir=user_data_dir,
        )
    if not token:
        logger.warning(
            "validatecaptcha: no recaptcha token minted (site_key=%s action=%s pure=%s v2=%s browser_allowed=%s)",
            bool(site_key),
            action,
            os.environ.get("PPS_PURE_PROTOCOL", ""),
            is_visible_v2,
            allow_browser_recaptcha,
        )
        try:
            with open("/tmp/pps_validatecaptcha_meta.json", "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "kind": "recaptchav3",
                        "status_code": None,
                        "site_key": site_key,
                        "action": action,
                        "iframe_src": iframe_src[:500],
                        "visible_v2": is_visible_v2,
                        "error": "no_token",
                        "pure_protocol": os.environ.get("PPS_PURE_PROTOCOL", ""),
                        "browser_allowed": allow_browser_recaptcha,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass
        return False
    for state in ("CLIENT_SIDE_RECAPTCHAV3_SOLVED", "CLIENT_SIDE_PPCAPTCHA_SOLVED"):
        _paypal_auth_logclientdata(
            s,
            challenge_html=challenge_html,
            csrf=csrf,
            session_id=session_id,
            ec_token=ec_token,
            captcha_state=state,
            signup_url=signup_url,
            timeout=timeout,
        )
    start = str(render.get("renderStartTime") or int(time.time() * 1000) - 3000)
    end = str(render.get("renderEndTime") or int(time.time() * 1000))
    form = {
        "_csrf": csrf,
        "_requestId": request_id,
        "_hash": hsh,
        "_sessionID": session_id,
        "jse": jse,
        "grcV3EntToken": token,
        "grcV3RenderEndTime": end,
        "grcV3RenderStartTime": start,
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": PP_ORIGIN,
        "Referer": signup_url,
        "X-Requested-With": "fetch",
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Full-Version-List": '"Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Model": '""',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Arch": '"x86"',
        "Sec-CH-Device-Memory": "8",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        r = s.post(f"{PP_ORIGIN}/auth/validatecaptcha", data=form, headers=headers, timeout=timeout)
        text = r.text or ""
        try:
            with open("/tmp/pps_validatecaptcha_last.json", "w", encoding="utf-8") as f:
                f.write(text)
            with open("/tmp/pps_validatecaptcha_meta.json", "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "status_code": r.status_code,
                        "content_type": r.headers.get("content-type", ""),
                        "token_len": len(token),
                        "render": render,
                        "response_head": text[:500],
                        "response_kind": (
                            "json" if text.lstrip().startswith("{")
                            else "recaptcha_v2" if "recaptcha_v2.html" in text
                            else "html" if text.lstrip().startswith("<")
                            else "text"
                        ),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass
        logger.info("validatecaptcha status=%s len=%d token_len=%d", r.status_code, len(text), len(token))
        return r.status_code == 200 and text.lstrip().startswith("{") and "errors" not in text[:500].lower()
    except Exception as e:
        logger.warning("validatecaptcha soft-failed: %s", e)
        return False


def _solve_hcaptcha_token_protocol(
    *,
    site_key: str,
    page_url: str,
    timeout: int,
    proxy: Optional[str] = None,
    alt_page_urls: Optional[list[str]] = None,
) -> tuple[str, dict[str, Any]]:
    """Obtain an hCaptcha token without driving PayPal pages in a browser.

    This is intentionally HTTP-only: either consume a pre-supplied one-shot token
    (`PPS_PAYPAL_HCAPTCHA_TOKEN`) or a createTask/getTaskResult-compatible
    captcha gateway configured through env.  If neither is configured, return an
    empty token so the caller can fail loudly instead of silently falling back
    to web automation.  If a provider is configured, prefer it over the local
    Node bridge because PayPal's passive hCaptcha often escalates the local DOM
    emulation to a visible challenge.
    """
    manual = (os.environ.get("PPS_PAYPAL_HCAPTCHA_TOKEN") or "").strip()
    if manual:
        logger.info("hcaptchapassive: using PPS_PAYPAL_HCAPTCHA_TOKEN len=%d", len(manual))
        return manual, {"source": "manual"}

    api_url, api_key = _captcha_gateway_config()

    page_candidates: list[str] = []
    for u in [page_url, *(alt_page_urls or []), PP_ORIGIN]:
        u = (u or "").strip()
        if u and u not in page_candidates:
            page_candidates.append(u)

    if api_url and api_key:
        # Different createTask-compatible providers bind PayPal's passive
        # hCaptcha either to the iframe document on paypalobjects.com or to the
        # parent checkoutweb URL.  Try both, try enterprise/non-enterprise, and
        # when a checkout proxy is available try same-IP proxy tasks before
        # Proxyless tasks.  This is still strictly HTTP-only.
        strategies: list[dict[str, Any]] = []
        proxy_fields = _captcha_proxy_fields(proxy)
        for u in page_candidates:
            for use_proxy in ([True, False] if proxy_fields else [False]):
                base = {
                    "type": "HCaptchaTask" if use_proxy else "HCaptchaTaskProxyless",
                    "websiteURL": u,
                    "websiteKey": site_key,
                    "isInvisible": True,
                    "userAgent": USER_AGENT,
                }
                if use_proxy:
                    base.update(proxy_fields)
                strategies.append({**base, "isEnterprise": True})
                strategies.append(dict(base))

        for idx, task in enumerate(strategies, 1):
            logger.info(
                "hcaptchapassive captcha strategy %d/%d enterprise=%s url=%s",
                idx,
                len(strategies),
                task.get("isEnterprise", False),
                str(task.get("websiteURL") or "")[:80],
            )
            token, _solution = _poll_captcha_gateway(
                task=task,
                timeout=timeout,
                label="hcaptchapassive",
                token_fields=("gRecaptchaResponse", "hcaptchaToken", "token"),
            )
            if token:
                return token, {"source": "gateway", "task": task}
    else:
        logger.info("hcaptchapassive: no pure HTTP captcha provider configured")

    node_token, node_render = _mint_hcaptcha_passive_token_via_node(
        iframe_url=page_url,
        parent_url=alt_page_urls[0] if alt_page_urls else "",
        timeout=min(max(30, timeout), 60),
        proxy=proxy,
    )
    if node_token:
        return node_token, {"source": "node", **node_render}
    return "", {}


def _mint_hcaptcha_passive_token_via_node(
    *,
    iframe_url: str,
    parent_url: str,
    timeout: int,
    proxy: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Best-effort pure-Node hCaptcha passive token mint.

    This uses the real PayPal passive bridge HTML + hCaptcha JS in happy-dom.
    It does not interact with a browser, but it does depend on the hCaptcha
    runtime being able to execute in a Node DOM emulation environment.  The
    helper is intentionally isolated so a failure still allows the caller to
    continue to a provider or hard-fail with a useful debug artifact.
    """
    helper = Path(__file__).with_name("hcaptcha_passive_node.js")
    if not helper.exists():
        logger.info("hcaptchapassive node helper missing: %s", helper)
        return "", {}
    node_candidates = [
        os.environ.get("OPENAI_SENTINEL_NODE_PATH", "").strip(),
        os.environ.get("NODE", "").strip(),
        "node",
    ]
    node_bin = next((x for x in node_candidates if x), "node")
    iframe_url = (iframe_url or "").strip()
    if not iframe_url:
        return "", {}
    payload = {
        "iframeUrl": iframe_url,
        "parentUrl": parent_url or PP_ORIGIN,
        "userAgent": USER_AGENT,
        "timeoutMs": int(max(10, timeout) * 1000),
    }
    env = os.environ.copy()
    # Preserve both repo-local and container-local frontend deps paths so the
    # helper can require happy-dom even before the image is rebuilt.
    node_paths = [
        env.get("NODE_PATH", "").strip(),
        "/app/webui/frontend/node_modules",
        "/root/Gpt-Agreement-Payment/webui/frontend/node_modules",
        "/usr/local/lib/node_modules",
    ]
    env["NODE_PATH"] = ":".join([p for p in node_paths if p])
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
    try:
        proc = subprocess.run(
            [node_bin, str(helper)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=max(120, int(timeout) + 30),
            env=env,
            cwd=str(helper.parent),
        )
    except Exception as e:
        logger.info("hcaptchapassive node helper launch failed: %s", e)
        return "", {}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stderr:
        logger.debug("hcaptchapassive node helper stderr: %s", stderr[-2000:])
    if not stdout:
        logger.info("hcaptchapassive node helper produced no stdout (rc=%s)", proc.returncode)
        return "", {}
    try:
        data = json.loads(stdout)
    except Exception as e:
        logger.info("hcaptchapassive node helper JSON parse failed: %s", e)
        return "", {}
    token = str(data.get("token") or "").strip()
    if token:
        logger.info(
            "hcaptchapassive node helper token ready len=%d states=%s",
            len(token),
            ",".join(str(x) for x in (data.get("states") or [])[:5]),
        )
    else:
        logger.info(
            "hcaptchapassive node helper no token rc=%s error=%s states=%s",
            proc.returncode,
            data.get("error"),
            ",".join(str(x) for x in (data.get("states") or [])[:5]),
        )
    try:
        with open("/tmp/pps_hcaptchapassive_node_last.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "returncode": proc.returncode,
                    "ok": bool(token),
                    "error": data.get("error"),
                    "elapsedMs": data.get("elapsedMs"),
                    "states": data.get("states"),
                    "iframeCount": data.get("iframeCount"),
                    "iframeSrcs": data.get("iframeSrcs"),
                    "token_len": len(token),
                    "renderData": data.get("renderData") or {},
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        pass
    return token, (data.get("renderData") or {}) if isinstance(data.get("renderData"), dict) else {}


def _validate_paypal_hcaptcha_passive(
    s: Any,
    *,
    challenge_html: str,
    signup_url: str,
    proxy: Optional[str],
    timeout: int,
) -> bool:
    """Replay PayPal hcaptchapassive `/auth/validatecaptcha` submission."""
    csrf = _html_input_value(challenge_html, "_csrf") or _html_attr_value(challenge_html, "data-csrf")
    request_id = _html_input_value(challenge_html, "_requestId")
    hsh = _html_input_value(challenge_html, "_hash")
    session_id = _html_input_value(challenge_html, "_sessionID") or _html_attr_value(challenge_html, "data-sessionid")
    jse = _html_attr_value(challenge_html, "data-jse")
    iframe_src = _extract_hcaptcha_passive_iframe_src(challenge_html)
    site_key = _extract_hcaptcha_site_key(challenge_html, iframe_src)
    ec_token = ""
    try:
        m_ec = _EC_RE.search(signup_url or "")
        ec_token = m_ec.group(1) if m_ec else (_extract_auth_fpti(challenge_html).get("fltk") or "")
    except Exception:
        ec_token = ""
    if not all([csrf, request_id, hsh, session_id, jse, iframe_src, site_key]):
        logger.warning(
            "hcaptchapassive fields missing csrf=%s request=%s hash=%s session=%s jse=%s iframe=%s sitekey=%s",
            bool(csrf), bool(request_id), bool(hsh), bool(session_id), bool(jse), bool(iframe_src), bool(site_key),
        )
        return False

    for state in (
        "CLIENT_SIDE_HCAPTCHA_PASSIVE_SERVED",
        "CLIENT_SIDE_HCAPTCHA_PASSIVE_SCRIPT_ONLOAD",
        "CLIENT_SIDE_HCAPTCHA_PASSIVE_JS_LOADED",
    ):
        _paypal_auth_logclientdata(
            s,
            challenge_html=challenge_html,
            csrf=csrf,
            session_id=session_id,
            ec_token=ec_token,
            captcha_state=state,
            signup_url=signup_url,
            timeout=timeout,
        )

    token, solution = _solve_hcaptcha_token_protocol(
        site_key=site_key,
        page_url=iframe_src or signup_url,
        timeout=max(60, timeout * 3),
        proxy=proxy,
        alt_page_urls=[signup_url],
    )
    if not token:
        logger.warning("hcaptchapassive: no token available")
        try:
            with open("/tmp/pps_validatecaptcha_meta.json", "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "kind": "hcaptchapassive",
                        "status_code": None,
                        "site_key": site_key,
                        "iframe_src": iframe_src[:500],
                        "error": "no_token",
                        "provider_configured": bool(_captcha_gateway_config()[0] and _captcha_gateway_config()[1]),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass
        return False

    for state in ("CLIENT_SIDE_HCAPTCHA_PASSIVE_SOLVED", "CLIENT_SIDE_PPCAPTCHA_SOLVED"):
        _paypal_auth_logclientdata(
            s,
            challenge_html=challenge_html,
            csrf=csrf,
            session_id=session_id,
            ec_token=ec_token,
            captcha_state=state,
            signup_url=signup_url,
            timeout=timeout,
        )

    now = int(time.time() * 1000)
    render_start = int(
        solution.get("hcaptchaPassiveRenderStartTime")
        or solution.get("hcaptcha_passive_render_start_time_utc")
        or solution.get("renderStartTime")
        or (now - random.randint(3500, 6500))
    )
    render_end = int(
        solution.get("hcaptchaPassiveRenderEndTime")
        or solution.get("hcaptcha_passive_render_end_time_utc")
        or solution.get("renderEndTime")
        or (now - random.randint(500, 1800))
    )
    verify_ts = int(
        solution.get("hcaptchaPassiveVerificationTime")
        or solution.get("hcaptcha_passive_verification_time_utc")
        or solution.get("verificationTime")
        or now
    )
    form = {
        "_csrf": csrf,
        "_requestId": request_id,
        "_hash": hsh,
        "_sessionID": session_id,
        "jse": jse,
        "hcaptchaToken": token,
        "publicKey": site_key,
        "hcaptcha_passive_eval_start_time_utc": str(render_start - random.randint(250, 900)),
        "hcaptcha_passive_render_start_time_utc": str(render_start),
        "hcaptcha_passive_render_end_time_utc": str(render_end),
        "hcaptcha_passive_verification_time_utc": str(verify_ts),
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": PP_ORIGIN,
        "Referer": signup_url,
        "X-Requested-With": "fetch",
        "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "Sec-CH-UA-Full-Version-List": '"Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"',
        "Sec-CH-UA-Platform": '"Windows"',
        "Sec-CH-UA-Model": '""',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Arch": '"x86"',
        "Sec-CH-Device-Memory": "8",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    try:
        r = s.post(f"{PP_ORIGIN}/auth/validatecaptcha", data=form, headers=headers, timeout=timeout)
        text = r.text or ""
        try:
            with open("/tmp/pps_validatecaptcha_last.json", "w", encoding="utf-8") as f:
                f.write(text)
            with open("/tmp/pps_validatecaptcha_meta.json", "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "kind": "hcaptchapassive",
                        "status_code": r.status_code,
                        "content_type": r.headers.get("content-type", ""),
                        "token_len": len(token),
                        "site_key": site_key,
                        "iframe_src": iframe_src[:500],
                        "response_head": text[:500],
                        "response_kind": (
                            "json" if text.lstrip().startswith("{")
                            else "html" if text.lstrip().startswith("<")
                            else "text"
                        ),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            pass
        logger.info("hcaptchapassive validatecaptcha status=%s len=%d token_len=%d",
                    r.status_code, len(text), len(token))
        return r.status_code == 200 and text.lstrip().startswith("{") and "errors" not in text[:500].lower()
    except Exception as e:
        logger.warning("hcaptchapassive validatecaptcha soft-failed: %s", e)
        return False


def _validate_paypal_authchallenge(
    s: Any,
    *,
    challenge_html: str,
    signup_url: str,
    proxy: Optional[str],
    user_data_dir: Optional[str] = None,
    timeout: int,
) -> bool:
    captcha_type = _authchallenge_captcha_type(challenge_html)
    if "hcaptchapassive" in captcha_type or _extract_hcaptcha_passive_iframe_src(challenge_html):
        return _validate_paypal_hcaptcha_passive(
            s,
            challenge_html=challenge_html,
            signup_url=signup_url,
            proxy=proxy,
            timeout=timeout,
        )
    if "recaptcha" in captcha_type or _extract_recaptcha_iframe_src(challenge_html):
        return _validate_paypal_recaptcha(
            s,
            challenge_html=challenge_html,
            signup_url=signup_url,
            proxy=proxy,
            user_data_dir=user_data_dir,
            timeout=timeout,
        )
    logger.warning("authchallenge: unsupported captcha type=%r", captcha_type)
    return False


def _retry_signup_address_from_persona(
    persona: Persona,
    template: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build a userscript-style MANUAL billing address from a fresh persona.

    `scripts/no_card_paypal_plus.py` passes a PayPal-specific getAddr() result
    for the first attempt.  On createMemberAccount/OAS retry we need to rotate
    the whole userscript identity tuple: Gmail, password and address.  This
    helper keeps the address shape accepted by `_signup_variables`.
    """
    template = template or {}
    return {
        "country": (template.get("country") or persona.country or "US").upper(),
        "line1": persona.line1,
        "city": persona.city,
        "state": persona.state,
        "postalCode": persona.postal_code,
        "autoCompleteType": template.get("autoCompleteType") or "MANUAL",
        "isUserModified": bool(template.get("isUserModified", False)),
    }


def _signup_response_parts(signup_resp: dict[str, Any]) -> dict[str, Any]:
    """Normalize PayPal SignUpNewMember response fields used for retry/fallback."""
    signup_errors = signup_resp.get("errors") or []
    first_signup_error = signup_errors[0] if signup_errors else {}
    raw_error_data = first_signup_error.get("errorData") or {}
    first_error_data = raw_error_data if isinstance(raw_error_data, dict) else {}
    first_error_item = (
        raw_error_data[0]
        if isinstance(raw_error_data, list) and raw_error_data and isinstance(raw_error_data[0], dict)
        else {}
    )
    first_error_code = (
        (first_error_data.get("0") or {}).get("code")
        or first_error_item.get("code")
        or (first_signup_error.get("checkpoints") or [""])[0]
        or first_signup_error.get("message")
        or "UNKNOWN"
    )
    onboard = (signup_resp.get("data") or {}).get("onboardAccount") or {}
    buyer = onboard.get("buyer") or {}
    euat = ((buyer.get("auth") or {}).get("accessToken")
            or first_error_data.get("accessToken"))
    return {
        "signup_errors": signup_errors,
        "first_signup_error": first_signup_error,
        "first_error_code": first_error_code,
        "first_error_data": first_error_data,
        "onboard": onboard,
        "buyer": buyer,
        "euat": euat,
        "user_id": buyer.get("userId"),
    }


def _is_retryable_create_member_account_error(parts: dict[str, Any]) -> bool:
    """True when PayPal rejected account creation before issuing EUAT.

    Historical successful captures often return a card/addFI error with
    `errorData.accessToken`; those must *not* be retried because they can
    continue via billingLite.  Retry only the no-EUAT createMemberAccount/OAS
    bucket by rotating the userscript persona tuple.
    """
    if parts.get("euat"):
        return False
    errors = parts.get("signup_errors") or []
    first = parts.get("first_signup_error") or {}
    msg = str(first.get("message") or first.get("_name") or "").lower()
    checkpoints = [str(x).lower() for x in (first.get("checkpoints") or [])]
    code = str(parts.get("first_error_code") or "").lower()
    if "creatememberaccount" in checkpoints:
        return True
    if "oas_error" in msg and ("creatememberaccount" in code or "creatememberaccount" in checkpoints):
        return True
    # PayPal sometimes omits checkpoints but still reports a plain
    # VALIDATION/OAS message before any buyer/auth object exists. Keep the
    # retry rule narrow to avoid retrying card/addFI paths that are useful.
    if errors and "oas_error" in msg and not any("addcard" in c or "addfi" in c for c in checkpoints):
        return True
    return False


def _signup_variables(
    *,
    persona: Persona,
    ec_token: str,
    phone_e164: str,
    locale_country: str,
    locale_lang: str,
    content_identifier: Optional[str] = None,
    signup_card: Optional[dict[str, Any]] = None,
    signup_billing_address: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    cc, num = _phone_split(phone_e164)
    # The random identity provider sometimes emits synthetic-looking family
    # names (for example consonant-heavy strings) that PayPal's OAS rejects as
    # INVALID_LAST_NAME even though they are alphabetic.  The browser userscript
    # we are replicating used stable common US names; do the same for the PayPal
    # member profile while keeping the random email/password.
    if signup_card:
        # Keep the userscript-compatible stable name by default, but allow
        # targeted replay experiments against captured traces without editing
        # the module again.
        given_name = (
            os.environ.get("PPS_PAYPAL_SIGNUP_FIRST_NAME")
            or os.environ.get("PPS_PAYPAL_FIRST_NAME")
            or "James"
        )
        family_name = (
            os.environ.get("PPS_PAYPAL_SIGNUP_LAST_NAME")
            or os.environ.get("PPS_PAYPAL_LAST_NAME")
            or "Smith"
        )
    else:
        given_name = persona.first_name
        family_name = persona.last_name

    src_addr = signup_billing_address or {}
    addr_country = (src_addr.get("country") or persona.country or locale_country).upper()
    addr = {
        "line1": src_addr.get("line1") or persona.line1,
        "city": src_addr.get("city") or persona.city,
        "postalCode": src_addr.get("postalCode") or src_addr.get("postal_code") or persona.postal_code,
        "accountQuality": {
            # v32 userscript fills billingLine1/city/postal/state manually and
            # hides/escapes address autocomplete, so signup should present as
            # MANUAL unless a caller explicitly overrides it.
            "autoCompleteType": src_addr.get("autoCompleteType") or "MANUAL",
            "isUserModified": bool(src_addr.get("isUserModified", False)),
        },
        "country": addr_country,
        "familyName": family_name,
        "givenName": given_name,
    }
    # PayPal's state field is country-specific: US uses 2-letter codes from
    # the locale metadata (e.g. "CA"); FR/GB/most-of-EU don't take it at all.
    state = _us_state_code(src_addr.get("state") or persona.state)
    if addr_country == "US" and state:
        addr["state"] = state

    variables = {
        "country": locale_country,
        "email": persona.email,
        "firstName": given_name,
        "lastName": family_name,
        "phone": {"countryCode": cc, "number": num, "type": "MOBILE"},
        "supportedThreeDsExperiences": ["IFRAME"],
        "token": ec_token,
        "billingAddress": addr,
        "shippingAddress": {
            "line1": "",
            "city": "",
            "state": "",
            "postalCode": "",
            "accountQuality": {"autoCompleteType": "MANUAL", "isUserModified": False},
            "country": addr_country,
            "familyName": family_name,
            "givenName": given_name,
        },
        "contentIdentifier": content_identifier or f"{locale_country}:{locale_lang}:compliance.signupTerms",
        "marketingOptOut": False,
        "password": persona.password,
        "crsData": None,
        "legalAgreements": {},
    }
    if signup_card:
        variables["card"] = {
            "cardNumber": str(signup_card.get("cardNumber") or signup_card.get("number") or "").replace(" ", ""),
            "expirationDate": str(signup_card.get("expirationDate") or ""),
            "securityCode": str(signup_card.get("securityCode") or signup_card.get("cvc") or ""),
            "type": str(signup_card.get("type") or "VISA").upper(),
        }
    return variables


# ── Camoufox bootstrap (datadome JS challenge) ────────────────────────────────
def seed_via_camoufox(
    redirect_url: str,
    *,
    proxy: Optional[str] = None,
    timeout_ms: int = 60000,
    user_data_dir: Optional[str] = None,
    headless: bool = True,
    locale_country: str = "US",
    locale_lang: str = "en",
) -> dict[str, Any]:
    """Use Camoufox to pass datadome on /agreements/approve and harvest the
    cookies + EC token, then quit immediately.

    Pure-protocol cannot run the datadome JS challenge from a flagged IP, so
    Camoufox does the bootstrap and hands a "warm" session over to
    `signup_no_card(seed=...)`. Returns:

        {
          "cookies": {name: value, ...},     # only paypal.com cookies
          "ec_token": "EC-...",
          "ba_token": "BA-...",
          "signup_url": "https://www.paypal.com/checkoutweb/signup?...",
          "user_agent": "Mozilla/...",        # the UA Camoufox sent
        }
    """
    # Lazy imports so callers without Camoufox can still use --ec-token paths
    from camoufox.sync_api import Camoufox  # type: ignore
    from browserforge.fingerprints import Screen  # type: ignore
    from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

    locale_country = (locale_country or "US").upper()
    locale_lang = (locale_lang or "en").lower()

    def _force_paypal_locale(url: str) -> str:
        """Keep PayPal checkout/onboarding in the intended country/locale."""
        try:
            p = urlparse(url)
            q = parse_qs(p.query, keep_blank_values=True)
            q["ul"] = ["1"]
            q["country.x"] = [locale_country]
            q["locale.x"] = [f"{locale_lang}_{locale_country}"]
            return urlunparse(p._replace(query=urlencode(q, doseq=True)))
        except Exception:
            return url

    cf_proxy: Optional[dict[str, str]] = None
    if proxy:
        u = urlparse(proxy)
        if u.scheme in ("socks5", "socks5h") and u.username:
            # Camoufox needs a SOCKS5 server without auth in URL; user must
            # have a local gost relay. We just pass the no-auth form; caller
            # is responsible for spinning up the relay (card.py already does).
            cf_proxy = {"server": f"socks5://{u.hostname}:{u.port}"}
        else:
            cf_proxy = {
                "server": f"{u.scheme}://{u.hostname}:{u.port}",
                "username": u.username or "",
                "password": u.password or "",
            }

    cookies_out: dict[str, str] = {}
    ec_token = ""
    redirect_url = _force_paypal_locale(redirect_url)
    ba_token = parse_qs(urlparse(redirect_url).query).get("ba_token", [""])[0]
    signup_url = ""
    signup_html = ""
    user_agent = USER_AGENT

    kwargs: dict[str, Any] = dict(
        headless=headless,
        humanize=False,
        os="windows",
        screen=Screen(max_width=1920, max_height=1080),
        # Keep consistent with _mint_recaptcha_v3_token_via_camoufox (line 2839-2845):
        # Local gost relay is short-lived; geoip=True causes Camoufox to fetch
        # outbound public IP before startup, racing with relay startup/shutdown → throws InvalidIP. Browser
        # traffic still goes through caller proxy; Camoufox-side timezone/geo inference (en-US locale)
        # is already hardcoded without relying on IP lookup.
        geoip=False,
        locale="en-US",
    )
    if cf_proxy is not None:
        kwargs["proxy"] = cf_proxy
    if user_data_dir:
        kwargs["persistent_context"] = True
        kwargs["user_data_dir"] = user_data_dir

    deadline_ms = int(time.time() * 1000) + timeout_ms
    with Camoufox(**kwargs) as launched:
        # Camoufox returns either a Browser (default) or a BrowserContext
        # (when persistent_context=True). Normalise so .new_page/.cookies work.
        if hasattr(launched, "new_context"):
            ctx = launched.new_context()
        else:
            ctx = launched
        page = (ctx.pages[0] if getattr(ctx, "pages", None) else ctx.new_page())
        try:
            user_agent = page.evaluate("() => navigator.userAgent") or USER_AGENT
        except Exception:
            pass

        # Track whether the real browser actually ran the PayPal side-effect
        # scripts before we hand over to pure HTTP.  Previously the seed broke
        # as soon as the URL contained /checkoutweb/signup, often before
        # ddbm2/FraudNet/Weasley GraphQL had a chance to touch the EC token.
        warm_seen: dict[str, int] = {}

        def _note_warm_request(req: Any) -> None:
            try:
                u = req.url or ""
                host = urlparse(u).hostname or ""
                path = urlparse(u).path or ""
                key = ""
                if host == "ddbm2.paypal.com" and path.startswith("/js"):
                    key = "ddbm2"
                elif host in {"c.paypal.com", "c6.paypal.com", "b.stats.paypal.com", "hnd.stats.paypal.com"}:
                    key = "fraudnet"
                elif host == "www.paypal.com" and path == "/graphql":
                    key = "weasley_gql"
                elif host == "www.paypal.com" and path.startswith("/platform/tealeaftarget"):
                    key = "tealeaf"
                elif host == "www.paypal.com" and path.startswith("/xoplatform/logger"):
                    key = "xologger"
                if key:
                    warm_seen[key] = warm_seen.get(key, 0) + 1
            except Exception:
                pass

        try:
            page.on("request", _note_warm_request)
        except Exception:
            pass

        logger.info("camoufox: navigating %s", redirect_url[:120])
        page.goto(redirect_url, wait_until="domcontentloaded", timeout=timeout_ms)

        # ── DataDome slider solver (ported from card.py _try_solve_ddc_slider) ──
        # No vision libs — just an eased mouse drag from handle origin to the
        # iframe's right edge. Sufficient for DataDome's geo.ddc.paypal.com
        # passive challenge when the browser fingerprint is otherwise clean.
        _SLIDER_KWS = (
            "将滑块", "确认您是人类", "Slide the puzzle",
            "move the slider", "Move the slider", "滑动到最右",
        )

        _SLIDER_SELECTORS = (
            '.slider', '[role="slider"]', '.slider-handle', '.sliderIcon',
            'div[class*="slider"]', 'button[class*="slider"]',
            'div[class*="Slider"]', 'button[class*="Slider"]',
            'div[class*="handle"]', 'button[class*="handle"]',
            'div[class*="Handle"]', 'button[class*="Handle"]',
            'input[type="range"]', '#ddv1-captcha-container .slider',
        )

        def _ctx_text(ctx: Any, limit: int = 1800) -> str:
            try:
                return (ctx.inner_text("body") or "")[:limit]
            except Exception:
                return ""

        def _ctx_has_slider_dom(ctx: Any) -> bool:
            for sel in _SLIDER_SELECTORS:
                try:
                    el = ctx.query_selector(sel)
                    if el:
                        try:
                            if el.is_visible():
                                return True
                        except Exception:
                            return True
                except Exception:
                    continue
            return False

        def _is_ddcish_url(url: str) -> bool:
            u = (url or "").lower()
            return (
                "geo.ddc.paypal.com" in u
                or "ct.ddc.paypal.com" in u
                or "datadome" in u
                or "ads-dd-captcha" in u
            )

        def _slider_visible() -> bool:
            # Original Team/PayPal logic only checks frame for ddc/captcha/datadome URL;
            # no-card seed testing found DataDome iframe with empty URL, so here we scan all
            # frames and allow slider to render directly in main document.
            if any(kw in _ctx_text(page) for kw in _SLIDER_KWS) or _ctx_has_slider_dom(page):
                return True
            for fr in page.frames:
                if (fr.url or "") == (page.url or ""):
                    continue
                u = fr.url or ""
                txt = _ctx_text(fr)
                if (
                    _is_ddcish_url(u)
                    or any(kw in txt for kw in _SLIDER_KWS)
                    or _ctx_has_slider_dom(fr)
                ):
                    if any(kw in txt for kw in _SLIDER_KWS) or _ctx_has_slider_dom(fr):
                        return True
            return False

        def _ddc_present() -> bool:
            try:
                html = (page.content() or "")[:20000].lower()
            except Exception:
                html = ""
            if (
                "geo.ddc.paypal.com" in html
                or "ct.ddc.paypal.com" in html
                or "ads-dd-captcha" in html
            ):
                return True
            for fr in page.frames:
                u = fr.url or ""
                if _is_ddcish_url(u):
                    return True
            try:
                for el in page.query_selector_all("iframe"):
                    src = el.get_attribute("src") or ""
                    if _is_ddcish_url(src):
                        return True
            except Exception:
                pass
            return False

        def _visible_iframe_boxes() -> list[tuple[Any, dict[str, float] | None, str]]:
            out: list[tuple[Any, dict[str, float] | None, str]] = []
            try:
                iframe_els = page.query_selector_all("iframe")
            except Exception:
                iframe_els = []
            for idx, el in enumerate(iframe_els):
                box = None
                try:
                    if not el.is_visible():
                        continue
                except Exception:
                    pass
                try:
                    box = el.bounding_box()
                except Exception:
                    box = None
                src = ""
                try:
                    src = el.get_attribute("src") or ""
                except Exception:
                    pass
                out.append((el, box, f"iframe[{idx}] src={src[:80]!r}"))
            return out

        def _save_ddc_debug(label: str) -> None:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "ddc"
            try:
                page.screenshot(path=f"/tmp/pps_{safe}.png", full_page=True)
            except Exception:
                pass
            try:
                Path(f"/tmp/pps_{safe}.html").write_text(page.content() or "", encoding="utf-8")
            except Exception:
                pass
            for idx, fr in enumerate(page.frames):
                u = (fr.url or "").lower()
                if idx and not ("ddc" in u or "captcha" in u or "datadome" in u or not u):
                    continue
                try:
                    Path(f"/tmp/pps_{safe}_frame_{idx}.html").write_text(
                        fr.content() or "",
                        encoding="utf-8",
                    )
                except Exception:
                    pass

        def _try_click_ddc_action() -> bool:
            """Handle non-slider DataDome pages that expose a visible button.

            If the iframe is a pure blocked/captcha interstitial with no slider
            DOM, dragging random coordinates just wastes the full seed timeout.
            Try one benign visible action first; otherwise fail fast and let the
            caller re-seed or rotate the BA/proxy instead of falling back to HTTP.
            """
            contexts: list[tuple[Any, str]] = [(page, "main")]
            for idx, fr in enumerate(page.frames):
                if (fr.url or "") == (page.url or ""):
                    continue
                u = (fr.url or "").lower()
                txt = _ctx_text(fr, limit=600).lower()
                if _is_ddcish_url(u) or "datadome" in txt:
                    contexts.append((fr, f"frame[{idx}] url={fr.url[:80]!r}"))
            for ctx, label in contexts:
                try:
                    els = ctx.query_selector_all(
                        "button, [role='button'], input[type='button'], input[type='submit'], a"
                    )
                except Exception:
                    els = []
                for el in els:
                    try:
                        if not el.is_visible():
                            continue
                    except Exception:
                        pass
                    try:
                        txt = " ".join(filter(None, [
                            el.inner_text(timeout=500) if hasattr(el, "inner_text") else "",
                            el.get_attribute("value") or "",
                            el.get_attribute("aria-label") or "",
                            el.get_attribute("title") or "",
                        ])).strip()
                    except Exception:
                        txt = ""
                    if not re.search(r"(verify|continue|submit|start|agree|human|robot|验证|继续|确认)", txt, re.I):
                        continue
                    logger.info("ddc: clicking visible action in %s text=%r", label, txt[:80])
                    try:
                        el.click(timeout=3000)
                        page.wait_for_timeout(3000)
                    except Exception as e:
                        logger.info("ddc: visible action click failed: %s", e)
                        continue
                    cur = page.url or ""
                    if any(kw in cur for kw in ("/webapps/hermes", "checkoutweb", "/signin", "chatgpt.com")):
                        logger.info("ddc: action passed → %s", cur[:120])
                        return True
                    if not _ddc_present():
                        logger.info("ddc: action dismissed challenge")
                        return True
            return False

        def _candidate_contexts() -> list[tuple[Any, dict[str, float] | None, str]]:
            candidates: list[tuple[Any, dict[str, float] | None, str]] = []
            if any(kw in _ctx_text(page) for kw in _SLIDER_KWS) or _ctx_has_slider_dom(page):
                candidates.append((page, None, "main"))

            boxes = _visible_iframe_boxes()
            # Coarsely match iframe elements by frame order; if PayPal gives frame.url="",
            # we can still determine if it's a DataDome slider via text/DOM.
            child_frames = [fr for fr in page.frames if (fr.url or "") != (page.url or "")]
            for idx, fr in enumerate(child_frames):
                u = fr.url or ""
                txt = _ctx_text(fr)
                is_ddcish = (
                    _is_ddcish_url(u)
                    or any(kw in txt for kw in _SLIDER_KWS)
                    or _ctx_has_slider_dom(fr)
                )
                if not is_ddcish:
                    continue
                box = boxes[idx][1] if idx < len(boxes) else None
                candidates.append((fr, box, f"frame[{idx}] url={u[:80]!r}"))
            if not candidates and boxes:
                # Final fallback: when main document has slider text but Playwright frame.url is empty/unmapped,
                # attempt generic drag on all visible iframes.
                for idx, (_, box, label) in enumerate(boxes):
                    candidates.append((page, box, f"fallback-{label}"))
            return candidates

        def _find_handle(ctx: Any) -> tuple[Any | None, str]:
            tried = []
            for sel in _SLIDER_SELECTORS:
                try:
                    el = ctx.query_selector(sel)
                except Exception:
                    el = None
                tried.append(f"{sel}={'Y' if el else 'N'}")
                if not el:
                    continue
                try:
                    if el.is_visible():
                        return el, ",".join(tried)
                except Exception:
                    return el, ",".join(tried)
            return None, ",".join(tried)

        def _drag_once(start_x: float, start_y: float, end_x: float, end_y: float) -> None:
            page.mouse.move(start_x - random.uniform(20, 40),
                            start_y + random.uniform(-5, 5))
            time.sleep(random.uniform(0.15, 0.35))
            page.mouse.move(start_x, start_y)
            time.sleep(random.uniform(0.08, 0.18))
            page.mouse.down()
            time.sleep(random.uniform(0.1, 0.22))
            steps = random.randint(32, 48)
            for i in range(1, steps + 1):
                t = i / steps
                eased = t * t * (3 - 2 * t)
                x = start_x + (end_x - start_x) * eased
                y = start_y + (end_y - start_y) * eased + random.uniform(-1.8, 1.8)
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.012, 0.028))
            time.sleep(random.uniform(0.08, 0.18))
            page.mouse.up()

        def _passed_after_drag(attempt: int) -> bool:
            for _ in range(10):
                time.sleep(0.8)
                cur = page.url or ""
                if any(kw in cur for kw in ("/webapps/hermes", "checkoutweb",
                                            "/signin", "chatgpt.com")):
                    logger.info("ddc: passed → %s", cur[:120])
                    return True
                if not _slider_visible():
                    logger.info("ddc: slider passed (attempt %d)", attempt + 1)
                    return True
            return False

        def _try_solve_ddc_slider(attempts: int = 3) -> bool:
            for attempt in range(attempts):
                if attempt == 0:
                    page.wait_for_timeout(2500)
                contexts = _candidate_contexts()
                if not contexts:
                    logger.warning(
                        "ddc: no candidate context; frames=%r iframes=%r",
                        [f.url[:80] for f in page.frames],
                        [x[2] for x in _visible_iframe_boxes()],
                    )
                    page.wait_for_timeout(2000)
                    continue

                for ctx, iframe_box, label in contexts:
                    handle, tried = _find_handle(ctx)
                    coord_variants: list[tuple[float, float, float, float, str]] = []
                    if handle:
                        try:
                            hb = handle.bounding_box()
                        except Exception:
                            hb = None
                        if hb:
                            # Playwright normally returns main frame absolute coordinates; old Team logic handled
                            # iframe relative coordinates. Both are retained, sorted by reasonableness for attempts.
                            sx = hb["x"] + hb["width"] / 2
                            sy = hb["y"] + hb["height"] / 2
                            if iframe_box:
                                coord_variants.append((
                                    sx, sy,
                                    iframe_box["x"] + iframe_box["width"] - 10,
                                    sy,
                                    f"{label}/handle-abs",
                                ))
                                if hb["x"] < iframe_box["width"] and hb["y"] < iframe_box["height"]:
                                    coord_variants.append((
                                        iframe_box["x"] + hb["x"] + hb["width"] / 2,
                                        iframe_box["y"] + hb["y"] + hb["height"] / 2,
                                        iframe_box["x"] + iframe_box["width"] - 10,
                                        iframe_box["y"] + hb["y"] + hb["height"] / 2,
                                        f"{label}/handle-rel",
                                    ))
                            else:
                                vp = page.viewport_size or {"width": 1365, "height": 768}
                                coord_variants.append((sx, sy, vp["width"] - 30, sy, f"{label}/handle-main"))
                    else:
                        logger.warning("ddc: no slider handle in %s; tried=%s", label, tried)

                    if iframe_box:
                        coord_variants.append((
                            iframe_box["x"] + max(55, iframe_box["width"] * 0.10),
                            iframe_box["y"] + iframe_box["height"] * 0.55,
                            iframe_box["x"] + iframe_box["width"] - 15,
                            iframe_box["y"] + iframe_box["height"] * 0.55,
                            f"{label}/generic-iframe",
                        ))

                    if not coord_variants and label == "main":
                        vp = page.viewport_size or {"width": 1365, "height": 768}
                        coord_variants.append((110, vp["height"] * 0.55, vp["width"] - 35,
                                               vp["height"] * 0.55, "main/generic"))

                    for sx, sy, ex, ey, why in coord_variants:
                        logger.info(
                            "ddc drag attempt=%d %s start=(%.0f,%.0f) -> end=(%.0f,%.0f)",
                            attempt + 1, why, sx, sy, ex, ey,
                        )
                        try:
                            try:
                                page.screenshot(path=f"/tmp/pps_ddc_before_{attempt+1}.png", full_page=True)
                            except Exception:
                                pass
                            _drag_once(sx, sy, ex, ey)
                        except Exception as e:
                            logger.warning("ddc drag exception: %s", e)
                            continue
                        if _passed_after_drag(attempt):
                            return True
                    logger.info("ddc: %s did not pass on attempt %d", label, attempt + 1)
                time.sleep(random.uniform(1.0, 2.0))
            _save_ddc_debug("ddc_slider_failed")
            return False

        # Give DataDome 4s to settle / show the slider if it's going to.
        page.wait_for_timeout(4000)
        if _ddc_present() and not _slider_visible():
            logger.info("ddc: non-slider captcha/interstitial detected; trying visible action once")
            if not _try_click_ddc_action() and _ddc_present():
                _save_ddc_debug("ddc_non_slider_initial")
                raise CaptchaRequired("DataDome non-slider captcha (no slider DOM)")
        if _slider_visible() or _ddc_present():
            logger.info("ddc: visible slider/captcha iframe detected, trying solver")
            if not _try_solve_ddc_slider(attempts=3):
                raise CaptchaRequired("DataDome slider solver failed")

        # Wait until we land on /checkoutweb/signup with an EC token, or timeout.
        last_state = ""
        while int(time.time() * 1000) < deadline_ms:
            cur = page.url or ""
            if "/checkoutweb/signup" in cur and "token=EC-" in cur:
                signup_url = cur
                m = _EC_RE.search(cur)
                if m:
                    ec_token = m.group(1)
                try:
                    signup_html = page.content() or signup_html
                except Exception:
                    pass
                # Let the page behave like the reference browser for a short
                # window: it loads Weasley chunks, fires Deferred/Griffin/
                # CheckoutSession GraphQL, posts ddbm2 /js/ and FraudNet
                # beacons.  These server-side side effects are tied to EC and
                # are not visible if we immediately close the browser.
                warm_sec = float(os.environ.get("PPS_SEED_WARMUP_SEC", "10") or "10")
                warm_min_sec = min(4.0, warm_sec)
                warm_start = time.time()
                last_warm_log = ""
                logger.info("camoufox: signup reached; warming browser telemetry %.1fs", warm_sec)
                while time.time() - warm_start < warm_sec:
                    try:
                        page.wait_for_timeout(1000)
                    except Exception:
                        time.sleep(1)
                    try:
                        signup_html = page.content() or signup_html
                    except Exception:
                        pass
                    summary = ",".join(f"{k}={v}" for k, v in sorted(warm_seen.items())) or "none"
                    if summary != last_warm_log:
                        logger.info("camoufox warmup seen: %s", summary)
                        last_warm_log = summary
                    # Good-enough parity with the capture: at least one ddbm2,
                    # multiple FraudNet and page GraphQL requests, and a few
                    # seconds of dwell time for cookies to settle.
                    if (
                        time.time() - warm_start >= warm_min_sec
                        and warm_seen.get("ddbm2", 0) >= 1
                        and warm_seen.get("fraudnet", 0) >= 3
                        and warm_seen.get("weasley_gql", 0) >= 2
                    ):
                        break
                break
            try:
                html = page.content()
            except Exception:
                html = ""
            signup_html = html or signup_html

            # Detect the DataDome interstitial — surface it so callers stop
            # rather than wait the full timeout
            is_ddc = (
                "ads-dd-captcha" in html
                or "geo.ddc.paypal.com" in html
                or "ct.ddc.paypal.com" in html
                or _ddc_present()
            )
            state = ("ddc" if is_ddc else "page") + f"|len={len(html)}"
            if state != last_state:
                logger.info("camoufox state: %s url=%s", state, cur[:120])
                last_state = state
            if is_ddc:
                if not _slider_visible():
                    logger.info("ddc: non-slider challenge during wait loop; trying visible action once")
                    if _try_click_ddc_action():
                        continue
                    _save_ddc_debug("ddc_non_slider_wait")
                    raise CaptchaRequired("DataDome non-slider captcha (no slider DOM)")
                logger.info("ddc: still on challenge during wait loop, retrying solver")
                if not _try_solve_ddc_slider(attempts=2):
                    raise CaptchaRequired("DataDome slider solver failed")
                continue

            m_link = _ONBOARD_RE.search(html or "") or _UL_ONBOARD_RE.search(html or "")
            m_ec = _EC_RE.search(html or "")
            if m_ec and not ec_token:
                ec_token = m_ec.group(1)
            if m_link and not is_ddc:
                onboard = _unescape_url(m_link.group(1))
                if onboard.startswith("/"):
                    onboard = PP_ORIGIN + onboard
                onboard = _force_paypal_locale(onboard)
                logger.info("camoufox: following onboarding/ulOnboardRedirect link")
                page.goto(onboard, wait_until="domcontentloaded", timeout=timeout_ms)
                continue
            page.wait_for_timeout(1500)

        if not signup_url:
            signup_url = page.url or ""
            # Dump page state for diagnosis
            try:
                title = page.title()
                full = page.content() or ""
                signup_html = full or signup_html
                with open("/tmp/_pps_seed_timeout.html", "w") as f:
                    f.write(full)
                # Look for known PayPal markers
                for kw in ("expired", "Session expired", "session has expired",
                           "onboardingLink", "checkoutweb/signup", "EC-",
                           "addressTitle", "Invalid", "could not be processed",
                           "Sorry"):
                    if kw.lower() in full.lower():
                        idx = full.lower().index(kw.lower())
                        logger.warning(
                            "camoufox marker %r @ %d: %r",
                            kw, idx, full[max(0, idx-40):idx+150]
                        )
                logger.warning(
                    "camoufox: timed out url=%s title=%r body_len=%d (saved /tmp/_pps_seed_timeout.html)",
                    signup_url[:140], title, len(full),
                )
            except Exception:
                pass

        # Harvest paypal.com cookies
        try:
            cookie_list = ctx.cookies()
        except Exception:
            cookie_list = []
        for c in cookie_list:
            if "paypal.com" in c.get("domain", ""):
                cookies_out[c["name"]] = c["value"]

    if not ec_token:
        raise RuntimeError(f"camoufox: never reached /checkoutweb/signup (final={signup_url[:140]!r})")
    logger.info("camoufox seed: ec=%s ba=%s cookies=%d", ec_token, ba_token, len(cookies_out))
    return {
        "cookies": cookies_out,
        "ec_token": ec_token,
        "ba_token": ba_token,
        "signup_url": signup_url,
        "signup_html": signup_html,
        "user_agent": user_agent,
        "user_data_dir": user_data_dir or "",
    }


# ── Top-level entry point ─────────────────────────────────────────────────────
def signup_no_card(
    ba_token: str,
    *,
    seed: Optional[dict[str, Any]] = None,
    ec_token: Optional[str] = None,
    proxy: Optional[str] = None,
    persona: Optional[Persona] = None,
    signup_card: Optional[dict[str, Any]] = None,
    signup_billing_address: Optional[dict[str, Any]] = None,
    phone_e164: str = SMS_PHONE_E164,
    locale_country: str = "US",
    locale_lang: str = "en",
    otp_timeout: int = 180,
    request_timeout: int = 30,
    max_persona_retries: int = 0,
) -> SignupResult:
    """Run the captured no-card signup flow end-to-end.

    ``ba_token`` is the BA-... handle minted by the upstream merchant
    (e.g. Stripe SetupIntent → PayPal).

    Three bootstrap modes (cheapest first):
      - ``seed`` from :func:`seed_via_camoufox`: cookies + EC pre-warmed,
        we go straight to GraphQL. Required when datadome flags the IP.
      - ``ec_token`` only: skip /agreements/approve scrape, hit
        /checkoutweb/signup once for cookies. Works on clean IPs.
      - neither: pure HTTP /agreements/approve -> /checkoutweb/signup.
    """
    s = _make_session(proxy)
    supplied_persona = persona is not None
    persona = persona or fetch_persona(proxy=proxy)
    logger.info(
        "signup persona=%s %s <%s>", persona.first_name, persona.last_name, persona.email
    )

    # 1) Bootstrap → EC token + datadome cookie + signup URL
    signup_html = ""
    seed_url_for_warmup = ""
    if seed is not None:
        for name, val in (seed.get("cookies") or {}).items():
            try:
                s.cookies.set(name, val, domain=".paypal.com", path="/")
            except Exception:
                pass
        ec_token = ec_token or seed.get("ec_token")
        seed_url = seed.get("signup_url") or ""
        seed_url_for_warmup = seed_url
        signup_html = seed.get("signup_html") or ""
        # Camoufox/DataDome seed often lands on a real `/checkoutweb/signup`
        # URL with extra transient query flags (for example a bare
        # `Z3JncnB0=` marker) but *without* the browser-success
        # `modxo_redirect_reason=guest_user` parameter.  Weasley GraphQL is
        # sensitive to the signup page URL used as Referer; successful traces
        # keep the canonical form:
        #   ssrt, ul, country.x, locale.x, modxo_redirect_reason, ba_token,
        #   token, rcache, cookieBannerVariant
        # So always rebuild the signup referer from the seed URL instead of
        # trusting the transient URL verbatim.  Cookies/EC still come from the
        # real browser seed; only the pure-protocol page/referrer is normalized.
        signup_url = _build_signup_url(
            ba_token=ba_token,
            ec_token=str(ec_token or ""),
            locale_country=locale_country,
            locale_lang=locale_lang,
            source_url=seed_url,
        )
        if not ec_token:
            raise RuntimeError("seed missing ec_token")
        if seed_url and seed_url != signup_url:
            logger.info(
                "seed signup_url canonicalized: %s -> %s",
                seed_url[:140],
                signup_url[:140],
            )
        if "/checkoutweb/signup" not in seed_url or not signup_html or seed_url != signup_url:
            logger.info(
                "priming canonical checkoutweb/signup after seed (%s)",
                seed_url[:120],
            )
            try:
                signup_url, signup_html = _prime_checkout_signup(
                    s,
                    signup_url=signup_url,
                    referer=seed_url or f"{PP_ORIGIN}/agreements/approve?ba_token={ba_token}",
                    locale_country=locale_country,
                    locale_lang=locale_lang,
                    timeout=request_timeout,
                )
            except Exception as e:
                logger.warning("signup prime soft-failed: %s", e)
    elif ec_token:
        signup_url = _build_signup_url(
            ba_token=ba_token,
            ec_token=ec_token,
            locale_country=locale_country,
            locale_lang=locale_lang,
        )
        signup_url, signup_html = _prime_checkout_signup(
            s,
            signup_url=signup_url,
            referer="https://chatgpt.com/",
            locale_country=locale_country,
            locale_lang=locale_lang,
            timeout=request_timeout,
        )
    else:
        ec_token, signup_url, signup_html = _bootstrap(
            s,
            ba_token,
            locale_country=locale_country,
            locale_lang=locale_lang,
            timeout=request_timeout,
        )
    if "/checkoutweb/signup" not in (signup_url or ""):
        old_signup_url = signup_url
        signup_url = _build_signup_url(
            ba_token=ba_token,
            ec_token=str(ec_token or ""),
            locale_country=locale_country,
            locale_lang=locale_lang,
            source_url=old_signup_url,
        )
        logger.warning("signup_url was not checkoutweb/signup (%s); canonicalized to %s",
                       (old_signup_url or "")[:120], signup_url[:140])
    logger.info("ec_token=%s signup_url=%s", ec_token, signup_url[:140])

    # Userscript source of truth:
    #   paypal.com/pay -> fill random email -> click "Create an account"
    #   -> checkoutweb/signup
    # The pure-HTTP bootstrap path already emits the `/pay` side effects inside
    # `_bootstrap()`.  The Camoufox seed path used to skip those protocol
    # beacons because the real browser was closed as soon as it produced EC.
    # Emit the same best-effort BA-token observability/DFP/FraudNet pre-onboard
    # warmup here so seeded runs keep the same logical step order.
    if seed is not None:
        try:
            onboard_url = _build_onboard_url(
                ba_token=ba_token,
                locale_country=locale_country,
                locale_lang=locale_lang,
                source_url=seed_url_for_warmup or signup_url,
            )
            _paypal_pay_pre_onboard_warmup(
                s,
                ba_token=ba_token,
                ec_token=str(ec_token or ""),
                approve_html=(seed.get("approve_html") if isinstance(seed, dict) else "") or signup_html,
                onboard_url=onboard_url,
                locale_country=locale_country,
                locale_lang=locale_lang,
                timeout=request_timeout,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("seed pay pre-onboard warmup soft-failed: %s", e)

    # 2) Bootstrap GraphQL (DeferredFeature gives an OTP login context we
    #    don't strictly need, but its absence sometimes flags risk).
    deferred_resp: dict[str, Any] = {}
    try:
        deferred_resp = _gql(
            s,
            "DeferredFeature",
            {
                "channel": "WEB",
                "countryCodeAsString": locale_country,
                "integrationType": "XoSignupAuth",
                "isBaslAsString": "false",
                "isForcedGuest": "false",
                "token": ec_token,
            },
            Q_DEFERRED,
            signup_url=signup_url,
            timeout=request_timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("DeferredFeature soft-failed: %s", e)
    otp_login_context = _decode_otp_login_context(deferred_resp)

    # 2b) Metadata/session warm-up. The browser trace calls both before any
    # phone/card submission; skipping them correlates with opaque OAS errors on
    # createMemberAccount for some PayPal risk buckets.
    try:
        _gql(
            s,
            "GriffinMetadataQuery",
            {
                "countryCode": locale_country,
                "languageCode": locale_lang,
                "shippingCountryCode": locale_country,
            },
            Q_GRIFFIN_METADATA,
            signup_url=signup_url,
            timeout=request_timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("GriffinMetadata soft-failed: %s", e)
    try:
        _gql(
            s,
            "CheckoutSessionDataQuery",
            {"token": ec_token},
            Q_CHECKOUT_SESSION,
            signup_url=signup_url,
            timeout=request_timeout,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("CheckoutSessionData soft-failed: %s", e)

    _paypal_weasley_log(
        s,
        ec_token=ec_token,
        signup_url=signup_url,
        locale_country=locale_country,
        locale_lang=locale_lang,
        event_names=[
            "weasley_client_eligibility_check_success",
            "weasley_api_request_deferred_feature",
            "weasley_experiment_shouldShowOTP",
            "WEASLEY_PAGE_INTERACTIVE_FPTI",
            "WEASLEY_PREPARE_BILLING_PAGE_FPTI",
            "WEASLEY_IS_ADDRESSLESS_FPTI",
            "weasley_payment_request_api_available",
        ],
        timeout=request_timeout,
    )

    _paypal_ddbm2_node_warmup(
        s,
        signup_url=signup_url,
        ba_token=ba_token,
        timeout=request_timeout,
    )

    if str(os.environ.get("PPS_SKIP_FRAUDNET", "")).lower() not in {"1", "true", "yes", "on"}:
        _paypal_fraudnet_warmup(
            s,
            ec_token=ec_token,
            signup_url=signup_url,
            ba_token=ba_token,
            timeout=request_timeout,
        )

    idapps_enabled = (
        str(os.environ.get("PPS_DISABLE_IDAPPS", "")).lower() not in {"1", "true", "yes", "on"}
        and str(os.environ.get("PPS_ENABLE_IDAPPS", "1")).lower() in {"1", "true", "yes", "on"}
    )
    if otp_login_context and idapps_enabled:
        challenge_html = _idapps_get_otp_challenge(
            s,
            signup_url=signup_url,
            ec_token=ec_token,
            email=persona.email,
            otp_context=otp_login_context,
            timeout=request_timeout,
        )
        if challenge_html:
            _validate_paypal_authchallenge(
                s,
                challenge_html=challenge_html,
                signup_url=signup_url,
                proxy=proxy,
                user_data_dir=(seed or {}).get("user_data_dir") if isinstance(seed, dict) else None,
                timeout=request_timeout,
            )

    # userscript v32 does not select a Google autocomplete suggestion on the
    # PayPal signup page; it hides the autocomplete dropdown / clicks manual
    # entry and fills billingLine1/city/postal/state directly.  Therefore the
    # default pure-protocol path keeps the caller/persona US address as MANUAL.
    resolved_billing_address = signup_billing_address
    if (
        signup_card
        and str(os.environ.get("PPS_ENABLE_GOOGLE_ADDRESS", "")).lower() in {"1", "true", "yes", "on"}
    ):
        resolved_billing_address = (
            _resolve_google_like_address(
                s,
                signup_url=signup_url,
                ec_token=ec_token,
                locale_country=locale_country,
                locale_lang=locale_lang,
                timeout=request_timeout,
            )
            or signup_billing_address
        )

    if (
        signup_card
        and str(os.environ.get("PPS_ENABLE_BROWSER_FORM_WARMUP", "")).lower() in {"1", "true", "yes", "on"}
    ):
        warm_vars = _signup_variables(
            persona=persona,
            ec_token=ec_token,
            phone_e164=phone_e164,
            locale_country=locale_country,
            locale_lang=locale_lang,
            content_identifier=None,
            signup_card=signup_card,
            signup_billing_address=resolved_billing_address,
        )
        browser_cookies = _browser_warm_signup_form(
            s,
            signup_url=signup_url,
            variables=warm_vars,
            proxy=proxy,
            user_data_dir=(seed or {}).get("user_data_dir") if isinstance(seed, dict) else None,
            timeout_ms=max(45000, request_timeout * 1000),
        )
        for name, val in browser_cookies.items():
            try:
                s.cookies.set(name, val, domain=".paypal.com", path="/")
            except Exception:
                pass
        if browser_cookies:
            logger.info("browser form warmup merged cookies=%s", _session_cookie_names(s))

    if signup_card and str(os.environ.get("PPS_SKIP_FRAUDNET", "")).lower() not in {"1", "true", "yes", "on"}:
        _paypal_fraudnet_field_events(
            s,
            ec_token=ec_token,
            # Match the v32 userscript fill order on `/checkoutweb/*`.
            # These are not form submissions; they are FraudNet typing/field
            # beacons that make the pure-protocol flow look like the same
            # browser-side sequence:
            # email -> phone -> card -> expiry -> cvv -> password -> names
            # -> billing address fields -> state select.
            field_ids=[
                "email",
                "phone",
                "cardNumber",
                "cardExpiry",
                "cardCvv",
                "password",
                "firstName",
                "lastName",
                "billingLine1",
                "billingCity",
                "billingPostalCode",
                "billingState",
            ],
            timeout=request_timeout,
        )

    # 3) Send SMS OTP
    _paypal_weasley_log(
        s,
        ec_token=ec_token,
        signup_url=signup_url,
        locale_country=locale_country,
        locale_lang=locale_lang,
        event_names=[
            "weasley_risk_based_phone_confirmation_modal_component_mounted",
            "weasley_initiate_phone_confirmation_start",
            "weasley_api_request_initiate_risk_based_two_factor_phone_confirmation_mutation",
        ],
        timeout=request_timeout,
    )
    sms_baseline = _sms_gateway_text(proxy=proxy)
    if sms_baseline:
        logger.info("sms baseline before init: %s", sms_baseline)
    sms_t0 = time.time()
    cc, num = _phone_split(phone_e164)
    phone_country = {"1": "US", "33": "FR", "44": "GB"}.get(cc, locale_country)
    init_resp = _gql(
        s,
        "InitiateRiskBasedTwoFactorPhoneConfirmationMutation",
        {
            "locale": {"country": locale_country, "lang": locale_lang},
            "phoneCountry": phone_country,
            "phoneNumber": num,
            "token": ec_token,
        },
        Q_INIT_OTP,
        signup_url=signup_url,
        timeout=request_timeout,
    )
    init_data = (init_resp.get("data") or {}).get("initiateRiskBasedTwoFactorPhoneConfirmation") or {}
    auth_id = init_data.get("authId")
    challenge_id = init_data.get("challengeId")
    if not auth_id or not challenge_id:
        return SignupResult(
            success=False,
            error="OTP init failed",
            error_code="OTP_INIT",
            ec_token=ec_token,
            ba_token=ba_token,
            persona=persona,
        )
    _paypal_weasley_log(
        s,
        ec_token=ec_token,
        signup_url=signup_url,
        locale_country=locale_country,
        locale_lang=locale_lang,
        event_names=[
            "weasley_api_response_status_200_initiate_risk_based_two_factor_phone_confirmation_mutation",
            "weasley_initiate_phone_confirmation_success",
            "weasley_phone_confirmation_interstitial_component_mounted",
        ],
        timeout=request_timeout,
    )
    logger.info("otp init authId=%s challengeId=%s state=%s",
                auth_id, challenge_id, init_data.get("state"))

    # 4) Poll SMS gateway
    pin = wait_for_sms_otp(
        after_ts=sms_t0,
        timeout=otp_timeout,
        proxy=proxy,
        baseline_text=sms_baseline,
    )
    logger.info("otp received: %s", pin)

    # 5) Confirm OTP
    _paypal_weasley_log(
        s,
        ec_token=ec_token,
        signup_url=signup_url,
        locale_country=locale_country,
        locale_lang=locale_lang,
        event_names=[
            "weasley_confirm_phone_confirmation_start",
            "weasley_api_request_confirm_risk_based_two_factor_phone_confirmation_mutation",
        ],
        timeout=request_timeout,
    )
    conf_resp = _gql(
        s,
        "ConfirmRiskBasedTwoFactorPhoneConfirmationMutation",
        {
            "authId": auth_id,
            "challengeId": challenge_id,
            "pin": pin,
            "token": ec_token,
        },
        Q_CONFIRM_OTP,
        signup_url=signup_url,
        timeout=request_timeout,
    )
    conf_state = ((conf_resp.get("data") or {})
                  .get("confirmRiskBasedTwoFactorPhoneConfirmation") or {}).get("state")
    if conf_state != "CONFIRMED":
        return SignupResult(
            success=False,
            error=f"OTP confirm rejected: state={conf_state}",
            error_code="OTP_CONFIRM",
            ec_token=ec_token,
            ba_token=ba_token,
            persona=persona,
        )
    _paypal_weasley_log(
        s,
        ec_token=ec_token,
        signup_url=signup_url,
        locale_country=locale_country,
        locale_lang=locale_lang,
        event_names=[
            "weasley_api_response_status_200_confirm_risk_based_two_factor_phone_confirmation_mutation",
            "weasley_confirm_phone_confirmation_success",
        ],
        timeout=request_timeout,
    )

    # 6) Sign up — no card
    content_identifier = _extract_content_identifier(signup_html, locale_country, locale_lang)
    logger.info("signup contentIdentifier=%s", content_identifier)
    try:
        env_retries = int(os.environ.get("PPS_PAYPAL_SIGNUP_PERSONA_RETRIES", "0") or "0")
    except Exception:
        env_retries = 0
    max_persona_retries = max(0, int(max_persona_retries or env_retries or 0))
    if supplied_persona and str(os.environ.get("PPS_PAYPAL_RETRY_SUPPLIED_PERSONA", "")).lower() not in {"1", "true", "yes", "on"}:
        max_persona_retries = 0

    attempt_persona = persona
    attempt_billing_address = resolved_billing_address
    signup_attempts_debug: list[dict[str, Any]] = []
    signup_resp: dict[str, Any] = {}
    parts: dict[str, Any] = {}

    for signup_attempt in range(max_persona_retries + 1):
        if signup_attempt > 0:
            logger.info(
                "signup persona retry %d/%d persona=%s %s <%s>",
                signup_attempt,
                max_persona_retries,
                attempt_persona.first_name,
                attempt_persona.last_name,
                attempt_persona.email,
            )
            # Replaying the userscript after a failed submit means the new
            # field values are typed before clicking submit again.  Emit a
            # second field burst for retry attempts.
            if signup_card and str(os.environ.get("PPS_SKIP_FRAUDNET", "")).lower() not in {"1", "true", "yes", "on"}:
                _paypal_fraudnet_field_events(
                    s,
                    ec_token=ec_token,
                    field_ids=[
                        "email", "phone", "cardNumber", "cardExpiry", "cardCvv",
                        "password", "firstName", "lastName",
                        "billingLine1", "billingCity", "billingPostalCode", "billingState",
                    ],
                    timeout=request_timeout,
                )

        variables = _signup_variables(
            persona=attempt_persona,
            ec_token=ec_token,
            phone_e164=phone_e164,
            locale_country=locale_country,
            locale_lang=locale_lang,
            content_identifier=content_identifier,
            signup_card=signup_card,
            signup_billing_address=attempt_billing_address,
        )
        if signup_card:
            logger.info(
                "signup attempt %d/%d with card type=%s last4=%s billing=%s %s %s",
                signup_attempt + 1,
                max_persona_retries + 1,
                (variables.get("card") or {}).get("type"),
                (variables.get("card") or {}).get("cardNumber", "")[-4:],
                (variables.get("billingAddress") or {}).get("country"),
                (variables.get("billingAddress") or {}).get("state", ""),
                (variables.get("billingAddress") or {}).get("postalCode", ""),
            )
        _paypal_weasley_log(
            s,
            ec_token=ec_token,
            signup_url=signup_url,
            locale_country=locale_country,
            locale_lang=locale_lang,
            event_names=[
                "weasley_create_account_and_pay_submit",
                "weasley_api_request_sign_up_new_member_mutation",
            ],
            timeout=request_timeout,
        )
        try:
            signup_resp = _gql(
                s,
                "SignUpNewMemberMutation",
                variables,
                Q_SIGNUP,
                signup_url=signup_url,
                timeout=request_timeout,
                extra_body={"fn_sync_data": _paypal_fn_sync_data(ec_token)},
            )
        except CaptchaRequired as e:
            challenge_html = getattr(e, "html", "") or ""
            if challenge_html and _validate_paypal_authchallenge(
                s,
                challenge_html=challenge_html,
                signup_url=signup_url,
                proxy=proxy,
                user_data_dir=(seed or {}).get("user_data_dir") if isinstance(seed, dict) else None,
                timeout=request_timeout,
            ):
                logger.info("SignUpNewMember authchallenge validated; retrying mutation once")
                signup_resp = _gql(
                    s,
                    "SignUpNewMemberMutation",
                    variables,
                    Q_SIGNUP,
                    signup_url=signup_url,
                    timeout=request_timeout,
                    extra_body={"fn_sync_data": _paypal_fn_sync_data(ec_token)},
                )
            elif signup_attempt < max_persona_retries:
                is_visible_recaptcha_v2 = "recaptcha_v2.html" in (challenge_html or "").lower()
                api_url, api_key = _captcha_gateway_config()
                has_recaptcha_token_path = bool(
                    (os.environ.get("PPS_PAYPAL_RECAPTCHA_TOKEN") or os.environ.get("PPS_PAYPAL_GRC_TOKEN") or "").strip()
                    or (api_url and api_key)
                )
                if is_visible_recaptcha_v2 and not has_recaptcha_token_path:
                    logger.warning(
                        "signup authchallenge is visible reCAPTCHA v2 and no token/provider is configured; "
                        "persona retry will not clear this session"
                    )
                    raise
                signup_attempts_debug.append({
                    "attempt": signup_attempt + 1,
                    "error": "captcha/html",
                    "code": getattr(e, "args", ["captcha/html"])[0] if getattr(e, "args", None) else "captcha/html",
                    "persona": _redact_for_log(asdict(attempt_persona)),
                })
                logger.warning(
                    "signup attempt %d/%d got captcha/html; rotating userscript persona/address",
                    signup_attempt + 1,
                    max_persona_retries + 1,
                )
                attempt_persona = fetch_persona(proxy=proxy)
                persona = attempt_persona
                if str(os.environ.get("PPS_PAYPAL_RETRY_KEEP_ADDRESS", "")).lower() not in {"1", "true", "yes", "on"}:
                    attempt_billing_address = _retry_signup_address_from_persona(
                        attempt_persona,
                        attempt_billing_address if isinstance(attempt_billing_address, dict) else resolved_billing_address,
                    )
                continue
            else:
                raise

        parts = _signup_response_parts(signup_resp)
        signup_errors = parts["signup_errors"]
        first_signup_error = parts["first_signup_error"]
        first_error_code = parts["first_error_code"]
        euat = parts["euat"]
        retryable = bool(signup_errors and _is_retryable_create_member_account_error(parts))
        if retryable and signup_attempt < max_persona_retries:
            signup_attempts_debug.append({
                "attempt": signup_attempt + 1,
                "error": first_signup_error.get("message"),
                "code": first_error_code,
                "checkpoints": first_signup_error.get("checkpoints") or [],
                "persona": _redact_for_log(asdict(attempt_persona)),
            })
            logger.warning(
                "signup attempt %d/%d hit %s/%s without EUAT; rotating userscript persona/address",
                signup_attempt + 1,
                max_persona_retries + 1,
                first_signup_error.get("message"),
                first_error_code,
            )
            attempt_persona = fetch_persona(proxy=proxy)
            persona = attempt_persona
            if str(os.environ.get("PPS_PAYPAL_RETRY_KEEP_ADDRESS", "")).lower() not in {"1", "true", "yes", "on"}:
                attempt_billing_address = _retry_signup_address_from_persona(
                    attempt_persona,
                    attempt_billing_address if isinstance(attempt_billing_address, dict) else resolved_billing_address,
                )
            continue
        break

    persona = attempt_persona
    resolved_billing_address = attempt_billing_address
    signup_errors = parts.get("signup_errors") or []
    first_signup_error = parts.get("first_signup_error") or {}
    first_error_code = parts.get("first_error_code") or "UNKNOWN"
    euat = parts.get("euat")
    user_id = parts.get("user_id")
    if signup_errors and euat:
        logger.warning(
            "signup partial error message=%s code=%s but accessToken present; "
            "continue via billingLite fallback",
            first_signup_error.get("message"),
            first_error_code,
        )
    elif signup_errors:
        return SignupResult(
            success=False,
            error=first_signup_error.get("message"),
            error_code=first_error_code,
            ec_token=ec_token,
            ba_token=ba_token,
            persona=persona,
            debug={
                "signup_errors": _redact_for_log(signup_errors),
                "signup_attempts": signup_attempts_debug,
            },
        )
    if not euat:
        return SignupResult(
            success=False,
            error="signup returned no accessToken",
            error_code="NO_EUAT",
            ec_token=ec_token,
            ba_token=ba_token,
            persona=persona,
            debug={
                "signup_errors": _redact_for_log(signup_errors),
                "signup_attempts": signup_attempts_debug,
            },
        )

    # 7) drop -> hermes -> authorize
    headers_html = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,*/*;q=0.8",
        "Referer": signup_url,
        "X-PayPal-Internal-EUAT": euat,
    }
    s.get(f"{PP_ORIGIN}/checkoutweb/drop", headers=headers_html, timeout=request_timeout)
    try:
        signup_qs = urllib.parse.parse_qs(urllib.parse.urlparse(signup_url).query)
    except Exception:
        signup_qs = {}
    hermes_params: list[tuple[str, str]] = []
    ssrt = (signup_qs.get("ssrt") or [""])[0]
    if ssrt:
        hermes_params.append(("ssrt", ssrt))
    hermes_params.extend([
        ("ul", "1"),
        ("country.x", locale_country),
        ("locale.x", f"{locale_lang}_{locale_country}"),
        ("modxo_redirect_reason", "guest_user"),
        ("ba_token", ba_token),
        ("token", ec_token),
        ("rcache", "1"),
        ("cookieBannerVariant", "hidden"),
        ("fromSignupLite", "true"),
    ])
    if signup_errors:
        reason = str(first_error_code or first_signup_error.get("message") or "SIGNUP_PARTIAL")
        reason_b64 = base64.b64encode(reason.encode("utf-8")).decode("ascii").rstrip("=")
        hermes_params.extend([
            ("fallback", "1"),
            ("reason", reason_b64),
            ("billingLite", "1"),
        ])
    hermes_url = f"{PP_ORIGIN}/webapps/hermes?{urllib.parse.urlencode(hermes_params)}"
    s.get(hermes_url, headers=headers_html, timeout=request_timeout)

    auth_resp = s.post(
        f"{PP_ORIGIN}/graphql/",
        json=[{
            "operationName": "authorize",
            "variables": {
                "billingAgreementId": ec_token,
                "fundingPreference": {"balancePreference": "OPT_OUT"},
                "legalAgreements": {},
            },
            "query": Q_AUTHORIZE,
        }],
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Origin": PP_ORIGIN,
            "Referer": hermes_url,
            "X-Requested-With": "fetch",
            "X-App-Name": "checkoutuinodeweb",
            "X-PayPal-Internal-EUAT": euat,
        },
        timeout=request_timeout,
    )
    try:
        auth_data = auth_resp.json()[0]
    except Exception as e:  # noqa: BLE001
        return SignupResult(
            success=False,
            error=f"authorize parse: {e}: {auth_resp.text[:200]}",
            error_code="AUTHORIZE_PARSE",
            ec_token=ec_token, ba_token=ba_token, user_id=user_id, euat=euat,
            persona=persona,
        )
    authorize = ((auth_data.get("data") or {}).get("billing") or {}).get("authorize") or {}
    return_url = (authorize.get("returnURL") or {}).get("href")

    cookies_out: dict[str, str] = {}
    try:
        # curl_cffi.Cookies has .get_dict(); requests.cookies has .get_dict() too.
        cookies_out = dict(s.cookies.get_dict())  # type: ignore[attr-defined]
    except Exception:
        try:
            cookies_out = {c.name: c.value for c in s.cookies}  # type: ignore[attr-defined]
        except Exception:
            pass

    return SignupResult(
        success=bool(return_url),
        error=None if return_url else "authorize returned no URL",
        error_code=None if return_url else "AUTHORIZE_EMPTY",
        ec_token=ec_token,
        ba_token=authorize.get("billingAgreementToken") or ba_token,
        user_id=(authorize.get("buyer") or {}).get("userId") or user_id,
        return_url=return_url,
        euat=euat,
        persona=persona,
        cookies=cookies_out,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────
def _cli() -> int:
    import argparse

    p = argparse.ArgumentParser(description="PayPal no-card signup (pure protocol)")
    p.add_argument("--ba-token", required=True, help="BA-... token from upstream merchant")
    p.add_argument("--ec-token", help="EC-... token (else scraped from /agreements/approve)")
    p.add_argument("--proxy", help="proxy URL (e.g. socks5://127.0.0.1:18898)")
    p.add_argument("--phone", default=SMS_PHONE_E164)
    p.add_argument("--country", default="US")
    p.add_argument("--lang", default="en")
    p.add_argument("--otp-timeout", type=int, default=180)
    p.add_argument("--dry-persona", action="store_true",
                   help="only fetch and print a persona, then exit")
    p.add_argument("--seed", action="store_true",
                   help="use Camoufox to bootstrap datadome + EC before signup")
    p.add_argument("--dry-seed", action="store_true",
                   help="only run the Camoufox seed and print it, then exit")
    p.add_argument("--no-headless", action="store_true",
                   help="run Camoufox headed (needs DISPLAY)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.dry_persona:
        per = fetch_persona(proxy=args.proxy)
        print(json.dumps(asdict(per), ensure_ascii=False, indent=2))
        return 0

    seed = None
    if args.seed or args.dry_seed:
        redirect = f"{PP_ORIGIN}/agreements/approve?ba_token={args.ba_token}"
        seed = seed_via_camoufox(
            redirect, proxy=args.proxy, headless=not args.no_headless
        )
        if args.dry_seed:
            print(json.dumps(seed, ensure_ascii=False, indent=2))
            return 0

    result = signup_no_card(
        args.ba_token,
        seed=seed,
        ec_token=args.ec_token,
        proxy=args.proxy,
        phone_e164=args.phone,
        locale_country=args.country,
        locale_lang=args.lang,
        otp_timeout=args.otp_timeout,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.success else 2


if __name__ == "__main__":
    raise SystemExit(_cli())
