"""Probe registered ChatGPT accounts to tell whether they're still usable.

Status taxonomy (persisted to ``registered_accounts.last_check_status``):

  - ``valid``    Some credential successfully exchanges with OpenAI right now.
                 Either rt → fresh at, or current at → /me 200, or cookie → /me 200.
  - ``invalid``  All available credentials definitively rejected by OpenAI
                 (invalid_grant / 401 from /me with Bearer) AND there's no
                 remaining path to recover. Safe to delete.
  - ``unknown``  Network error, timeout, 5xx, or Cloudflare bot-challenge —
                 caller couldn't determine validity. NEVER auto-delete on this.

Probe order (strongest signal first):
  1. refresh_token → POST auth.openai.com/oauth/token  (most reliable: rt is
     long-lived; success means account fundamentally alive, can re-mint at)
  2. access_token  → GET chatgpt.com/backend-api/me Bearer  (at expires in
     ~1h; without an rt to re-mint, an expired at means no recovery → invalid)
  3. cookie/session_token → GET /backend-api/me with Cookie  (web session;
     CF often challenges non-browser TLS so 403 is treated as unknown, not
     invalid, to avoid false positives)

All probes go through the local gost relay (127.0.0.1:18898) when it's listening
so source IP stays close to the original registration IP.
"""
from __future__ import annotations

import base64
import json
import socket
from typing import Iterable, Optional

import httpx

from .db import get_db


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
_ME_URL = "https://chatgpt.com/backend-api/me"
_SESSION_URL = "https://chatgpt.com/api/auth/session"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _decode_jwt_payload(token: str) -> dict:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload_b64 = token.split(".", 2)[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload_b64.encode()).decode())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _access_token_email(token: str) -> str:
    payload = _decode_jwt_payload(token)
    profile = payload.get("https://api.openai.com/profile") or {}
    if isinstance(profile, dict):
        return str(profile.get("email") or "").strip().lower()
    return ""


def _normal_plan_type(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "team" in raw:
        return "team"
    if "plus" in raw:
        return "plus"
    if "pro" in raw:
        return "pro"
    if "free" in raw:
        return "free"
    return raw[:40]


def _access_token_plan_type(token: str) -> str:
    payload = _decode_jwt_payload(token)
    auth_claim = payload.get("https://api.openai.com/auth") or {}
    if isinstance(auth_claim, dict):
        return _normal_plan_type(str(auth_claim.get("chatgpt_plan_type") or ""))
    return ""


_CHECK_V4_URL = (
    "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
    "?timezone_offset_min=-540"
)


def _subscription_plan_to_normal(sp: str) -> str:
    """Normalize OpenAI 实时 subscription_plan slug.

    实测样本:
      chatgptplusplan -> plus
      chatgptteamplan -> team
      chatgptproplan  -> pro
      chatgptfreeplan -> free   (active subscription 但 free tier, 出现在 promo 之外)
      None / "" + has_active_subscription=False -> free
    """
    raw = (sp or "").strip().lower()
    if not raw:
        return ""
    if "team" in raw:
        return "team"
    if "pro" in raw and "plus" not in raw:
        return "pro"
    if "plus" in raw:
        return "plus"
    if "free" in raw:
        return "free"
    return raw[:40]


def _probe_check_v4_plan(access_token: str, timeout: float,
                          proxy: Optional[str]) -> tuple[str, str, str]:
    """实时 plan 探测: GET /backend-api/accounts/check/v4-2023-04-27.

    返回 (status, plan_type, message). plan_type 优先级:
      1. accounts.default.entitlement.subscription_plan (实时 server 状态)
      2. has_active_subscription=False → 'free'
    fallback 失败时 plan_type 返空串, 让 caller 决定是否回退 JWT claim。

    走 curl_cffi (impersonate chrome) 让 OpenAI 不识别为脚本: httpx+socks 在
    host 缺 socksio, curl_cffi 跨环境稳。
    """
    try:
        from curl_cffi import requests as cr
    except Exception as e:
        return "unknown", "", f"check/v4: curl_cffi missing: {e}"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
        "Referer": "https://chatgpt.com/",
    }

    def _try(proxies_arg):
        with cr.Session(impersonate="chrome136", proxies=proxies_arg) as s:
            return s.get(_CHECK_V4_URL, headers=headers, timeout=timeout)

    r = None
    last_err = ""
    # 先试代理(若提供); 代理失败(ProxyError/网络异常)时直连兜底, 别让代理瞬断
    # 把账号误标 invalid (live API 是判断"实时 plan"的唯一可靠途径)
    tried = []
    if proxy:
        p = proxy.replace("socks5://", "socks5h://")
        tried.append(("proxy", {"http": p, "https": p}))
    tried.append(("direct", None))
    for label, proxies in tried:
        try:
            r = _try(proxies)
            break
        except Exception as e:
            last_err = f"{label}: {type(e).__name__}: {str(e)[:80]}"
            r = None
            continue
    if r is None:
        return "unknown", "", f"check/v4: {last_err}"

    status_code = getattr(r, "status_code", 0)
    if status_code == 401:
        return "invalid", "", "check/v4: 401 (token revoked)"
    if status_code == 403:
        return "invalid", "", "check/v4: 403 (banned/disabled)"
    if status_code != 200:
        return "unknown", "", f"check/v4: http {status_code}"

    try:
        data = r.json()
    except Exception:
        return "unknown", "", "check/v4: 200 non-json"
    if not isinstance(data, dict):
        return "unknown", "", "check/v4: 200 non-dict"
    acc = (data.get("accounts") or {}).get("default") or {}
    if not acc:
        return "unknown", "", "check/v4: no default account"
    ent = acc.get("entitlement") or {}
    has_active = bool(ent.get("has_active_subscription"))
    sub_plan = str(ent.get("subscription_plan") or "")
    plan = _subscription_plan_to_normal(sub_plan)
    if not plan:
        plan = "free" if not has_active else "unknown"
    msg = (
        f"check/v4 ok; sub_plan={sub_plan!r} plan={plan} active={has_active}"
        f" expires={ent.get('expires_at')}"
    )
    return "valid", plan, msg


def _gost_alive(port: int = 18898) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _client(timeout: float, proxy: Optional[str]) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False, proxy=proxy)


def _probe_refresh(refresh_token: str, timeout: float,
                    proxy: Optional[str]) -> tuple[str, str]:
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CODEX_CLIENT_ID,
        "scope": "openid profile email offline_access",
    }
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    try:
        with _client(timeout, proxy) as c:
            r = c.post(_OAUTH_TOKEN_URL, data=body, headers=headers)
    except httpx.TimeoutException:
        return "unknown", "rt: timeout"
    except (httpx.NetworkError, httpx.ProxyError) as e:
        return "unknown", f"rt: {type(e).__name__}"
    except Exception as e:
        return "unknown", f"rt: {type(e).__name__}: {str(e)[:80]}"
    if r.status_code == 200:
        try:
            if r.json().get("access_token"):
                return "valid", "rt → at swap ok"
        except Exception:
            pass
        return "unknown", "rt: 200 no access_token"
    if r.status_code in (400, 401):
        try:
            err = (r.json().get("error") or "")[:60]
        except Exception:
            err = ""
        if err in ("invalid_grant", "invalid_client", "unauthorized_client",
                   "invalid_request"):
            return "invalid", f"rt: {err}"
        return "invalid", f"rt: http {r.status_code} {err}".strip()
    return "unknown", f"rt: http {r.status_code}"


def _refresh_status_with_rt(account: dict, timeout: float,
                            proxy: Optional[str]) -> dict:
    """Use the stored Codex refresh_token to mint a fresh access_token and
    parse ChatGPT plan claims from it.

    This is stronger than the generic /me probe for plan state: the token grant
    endpoint is less likely to be blocked by ChatGPT web CF pages, and the new
    access token carries ``chatgpt_plan_type`` (free/plus/team/pro).
    """
    account_id = int(account.get("id") or 0)
    email = str(account.get("email") or "").strip().lower()
    refresh_token = (account.get("refresh_token") or "").strip()
    if not refresh_token:
        return {
            "id": account_id,
            "email": email,
            "status": "no_rt",
            "plan_type": "",
            "message": "no refresh_token stored",
        }

    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _CODEX_CLIENT_ID,
        "scope": "openid profile email offline_access",
    }
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    db = get_db()
    try:
        with _client(timeout, proxy) as c:
            r = c.post(_OAUTH_TOKEN_URL, data=body, headers=headers)
    except httpx.TimeoutException:
        msg = "rt refresh: timeout"
        db.update_account_rt_status(account_id, status="unknown", message=msg)
        return {"id": account_id, "email": email, "status": "unknown",
                "plan_type": "", "message": msg}
    except (httpx.NetworkError, httpx.ProxyError) as e:
        msg = f"rt refresh: {type(e).__name__}"
        db.update_account_rt_status(account_id, status="unknown", message=msg)
        return {"id": account_id, "email": email, "status": "unknown",
                "plan_type": "", "message": msg}
    except Exception as e:
        msg = f"rt refresh: {type(e).__name__}: {str(e)[:120]}"
        db.update_account_rt_status(account_id, status="unknown", message=msg)
        return {"id": account_id, "email": email, "status": "unknown",
                "plan_type": "", "message": msg}

    if r.status_code != 200:
        try:
            data = r.json()
        except Exception:
            data = {}
        err = str(data.get("error") or "")[:80] if isinstance(data, dict) else ""
        msg = f"rt refresh: http {r.status_code} {err}".strip()
        status = "invalid" if r.status_code in (400, 401) and err in (
            "invalid_grant", "invalid_client", "unauthorized_client", "invalid_request"
        ) else "unknown"
        db.update_account_rt_status(account_id, status=status, message=msg)
        return {"id": account_id, "email": email, "status": status,
                "plan_type": "", "message": msg}

    try:
        data = r.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        msg = "rt refresh: 200 no access_token"
        db.update_account_rt_status(account_id, status="unknown", message=msg)
        return {"id": account_id, "email": email, "status": "unknown",
                "plan_type": "", "message": msg}

    token_email = _access_token_email(access_token)
    plan_type = _access_token_plan_type(access_token) or "unknown"
    if token_email and email and token_email != email:
        msg = f"rt refresh: token email mismatch {token_email} != {email}"
        db.update_account_rt_status(account_id, status="invalid", message=msg)
        return {"id": account_id, "email": email, "status": "invalid",
                "plan_type": plan_type, "message": msg, "token_email": token_email}

    new_refresh_token = str(data.get("refresh_token") or "").strip()
    id_token = str(data.get("id_token") or "").strip()
    msg = f"rt refresh ok; plan={plan_type}; email={token_email or email}"
    db.update_account_rt_status(
        account_id,
        status="valid",
        message=msg,
        plan_type=plan_type if plan_type != "unknown" else "",
        access_token=access_token,
        refresh_token=new_refresh_token,
        id_token=id_token,
    )
    return {
        "id": account_id,
        "email": email,
        "status": "valid",
        "plan_type": plan_type,
        "message": msg,
        "token_email": token_email or email,
        "access_token_updated": True,
        "refresh_token_rotated": bool(new_refresh_token),
    }


def _probe_me_with_bearer(access_token: str, timeout: float,
                            proxy: Optional[str]) -> tuple[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    try:
        with _client(timeout, proxy) as c:
            r = c.get(_ME_URL, headers=headers)
    except httpx.TimeoutException:
        return "unknown", "me: timeout"
    except (httpx.NetworkError, httpx.ProxyError) as e:
        return "unknown", f"me: {type(e).__name__}"
    except Exception as e:
        return "unknown", f"me: {type(e).__name__}: {str(e)[:80]}"
    if r.status_code == 200:
        try:
            data = r.json()
            uid = (data.get("id") or "")[:18]
            return "valid", f"me ok ({uid})"
        except Exception:
            return "unknown", "me: 200 non-json"
    if r.status_code == 401:
        return "invalid", "me: http 401 (token expired/revoked)"
    if r.status_code == 403:
        # /backend-api/me 走 Bearer 一般不会被 CF 误拦，403 多是 banned/disabled
        return "invalid", "me: http 403"
    return "unknown", f"me: http {r.status_code}"


def _build_cookie(account: dict) -> str:
    cookie_header = (account.get("cookie_header") or "").strip()
    if cookie_header:
        return cookie_header
    session_token = (account.get("session_token") or "").strip()
    if session_token:
        return f"__Secure-next-auth.session-token={session_token}"
    return ""


def _probe_me_with_cookie(account: dict, timeout: float,
                            proxy: Optional[str]) -> tuple[str, str]:
    cookie = _build_cookie(account)
    if not cookie:
        return "unknown", "no cookie"
    headers = {
        "Cookie": cookie,
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
        "Referer": "https://chatgpt.com/",
    }
    try:
        with _client(timeout, proxy) as c:
            r = c.get(_ME_URL, headers=headers)
    except httpx.TimeoutException:
        return "unknown", "cookie/me: timeout"
    except (httpx.NetworkError, httpx.ProxyError) as e:
        return "unknown", f"cookie/me: {type(e).__name__}"
    except Exception as e:
        return "unknown", f"cookie/me: {type(e).__name__}: {str(e)[:80]}"
    if r.status_code == 200:
        try:
            uid = (r.json().get("id") or "")[:18]
            return "valid", f"cookie/me ok ({uid})"
        except Exception:
            return "unknown", "cookie/me: 200 non-json"
    if r.status_code == 401:
        # 401 with cookie auth is OpenAI saying "session-token rejected" — usually
        # real, but with no Bearer to cross-check we treat as invalid only when
        # there's literally no other credential. Caller decides.
        return "invalid", "cookie/me: http 401"
    if r.status_code == 403:
        # 403 with no Bearer is almost always Cloudflare bot challenge / datadome,
        # not a real auth rejection. Mark unknown so we never delete on this.
        body_snip = r.text[:80].replace("\n", " ")
        return "unknown", f"cookie/me: http 403 (likely CF challenge) {body_snip}"
    return "unknown", f"cookie/me: http {r.status_code}"


def validate_account(account: dict, *, timeout_s: float = 10.0,
                       use_proxy: bool = True) -> tuple[str, str]:
    """Pure HTTP probe — caller persists result.

    Returns (status, message) where status ∈ {'valid','invalid','unknown'}.
    """
    refresh_token = (account.get("refresh_token") or "").strip()
    access_token = (account.get("access_token") or "").strip()
    cookie = _build_cookie(account)
    if not (refresh_token or access_token or cookie):
        return "unknown", "no credentials stored"

    proxy = "socks5://127.0.0.1:18898" if use_proxy and _gost_alive() else None

    # ── probe 1: refresh_token (most reliable, long-lived)
    if refresh_token:
        s, m = _probe_refresh(refresh_token, timeout_s, proxy)
        if s != "unknown":
            return s, m
        # rt path uncertain: fall through to at/cookie

    # ── probe 2: access_token Bearer → /me
    if access_token:
        s, m = _probe_me_with_bearer(access_token, timeout_s, proxy)
        if s == "valid":
            return s, m
        if s == "invalid":
            # at expired/revoked. Without rt there's no path to mint a new one
            # → genuinely unusable. With rt we'd already have returned above.
            if not refresh_token:
                return "invalid", m
            # If we had an rt but it returned 'unknown' earlier, falling
            # through to cookie probe is still informative.

    # ── probe 3: cookie / session_token → /me (CF-pruned, conservative)
    if cookie:
        s, m = _probe_me_with_cookie(account, timeout_s, proxy)
        if s == "valid":
            return s, m
        # cookie 401 alone isn't strong enough to delete; degrade to unknown
        if s == "invalid" and not (access_token or refresh_token):
            return "invalid", m
        if s == "invalid":
            return "unknown", f"cookie says invalid but other creds inconclusive: {m}"
        return s, m

    return "unknown", "no probe path succeeded"


def validate_account_by_id(account_id: int, *, timeout_s: float = 10.0,
                              use_proxy: bool = True) -> dict:
    """Validate one stored account, persist outcome, return summary.

    除常规探活外, 当有 access_token 时额外调一次 /backend-api/accounts/check
    拿**实时** plan_type (subscription_plan) 写回 DB. 否则只靠 JWT claim 会
    miss 注册后买的 plus/team — JWT claim 永远 stale at 签发时刻。
    """
    db = get_db()
    account = db.get_registered_account(int(account_id))
    if not account:
        return {"id": int(account_id), "status": "missing",
                "message": "account not found", "email": ""}
    status, message = validate_account(account, timeout_s=timeout_s,
                                          use_proxy=use_proxy)

    # 实时 plan 探测: 优先于 JWT claim 写回 last_plan_type
    plan_type = ""
    at = (account.get("access_token") or "").strip()
    if at:
        proxy = "socks5://127.0.0.1:18898" if use_proxy and _gost_alive() else None
        live_status, live_plan, live_msg = _probe_check_v4_plan(at, timeout_s, proxy)
        if live_status == "valid":
            # curl_cffi+chrome impersonate 200 OK from /backend-api/accounts/check 是
            # 比 httpx /me 403 更可靠的"token 有效"信号 (httpx 缺浏览器指纹常被 CF 误拦)。
            # 提升状态到 valid, 避免误删能用的账号。
            if status != "valid":
                status = "valid"
                message = f"{message} | {live_msg}" if message else live_msg
            if live_plan and live_plan != "unknown":
                plan_type = live_plan
        elif live_status == "invalid":
            # 实时 API 也明确说 401/403 → 提升对 status 的信心
            status = "invalid"
            message = f"{message} | {live_msg}" if message else live_msg
        else:
            # live API unknown (代理/网络瞬断): 不要 trust httpx /me 的 invalid,
            # 因为 httpx 缺浏览器指纹常被 CF 误拦 403 (实测多个有效 plus 账号被这么误判)。
            # 降级 invalid → unknown 保护好账号, 等下次代理恢复再判断。
            if status == "invalid" and "403" in (message or ""):
                status = "unknown"
                message = f"httpx 说 invalid 但 live API 不可达, 不下结论: {message} | {live_msg}"
        if not plan_type and live_status != "valid":
            # 只有 live API 没明确给 plan 时才 fallback JWT claim (stale, 但聊胜于无).
            # 注意 live_status==valid 但 plan 空时不 fallback, 避免把 JWT stale 的 free
            # 写到已经被 live API 确认订阅 active 的账号上。
            plan_type = _access_token_plan_type(at)

    db.update_account_check(int(account_id), status, message, plan_type=plan_type)
    return {
        "id": int(account_id),
        "email": account.get("email", ""),
        "status": status,
        "message": message,
        "plan_type": plan_type,
    }


def validate_accounts(account_ids: Iterable[int], *, max_workers: int = 3,
                        timeout_s: float = 10.0, use_proxy: bool = True) -> list[dict]:
    """Validate many accounts with bounded concurrency."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ids = [int(i) for i in account_ids if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        return []
    results: list[dict] = []
    workers = max(1, min(int(max_workers), len(ids)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(validate_account_by_id, i,
                              timeout_s=timeout_s, use_proxy=use_proxy): i
                   for i in ids}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"id": futures[fut], "status": "unknown",
                                "message": f"worker error: {type(e).__name__}: {e}",
                                "email": ""})
    return results


def refresh_rt_status_by_id(account_id: int, *, timeout_s: float = 15.0,
                            use_proxy: bool = True) -> dict:
    """Refresh one account's current ChatGPT plan/status via refresh_token."""
    db = get_db()
    account = db.get_registered_account(int(account_id))
    if not account:
        return {"id": int(account_id), "status": "missing",
                "message": "account not found", "email": "", "plan_type": ""}
    proxy = "socks5://127.0.0.1:18898" if use_proxy and _gost_alive() else None
    return _refresh_status_with_rt(account, float(timeout_s), proxy)


def refresh_rt_status_accounts(account_ids: Iterable[int], *, max_workers: int = 3,
                               timeout_s: float = 15.0,
                               use_proxy: bool = True) -> list[dict]:
    """Refresh many accounts with bounded concurrency."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ids = [int(i) for i in account_ids if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        return []
    results: list[dict] = []
    workers = max(1, min(int(max_workers), len(ids)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(refresh_rt_status_by_id, i,
                              timeout_s=timeout_s, use_proxy=use_proxy): i
                   for i in ids}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"id": futures[fut], "status": "unknown",
                                "plan_type": "",
                                "message": f"worker error: {type(e).__name__}: {e}",
                                "email": ""})
    return results
