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
    """Validate one stored account, persist outcome, return summary."""
    db = get_db()
    account = db.get_registered_account(int(account_id))
    if not account:
        return {"id": int(account_id), "status": "missing",
                "message": "account not found", "email": ""}
    status, message = validate_account(account, timeout_s=timeout_s,
                                          use_proxy=use_proxy)
    db.update_account_check(int(account_id), status, message)
    return {
        "id": int(account_id),
        "email": account.get("email", ""),
        "status": status,
        "message": message,
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
