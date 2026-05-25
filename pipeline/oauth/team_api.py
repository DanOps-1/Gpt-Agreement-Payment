"""OpenAI Codex refresh-token exchange + ChatGPT Team invite/accept API.

Extracted from pipeline._monolith, five self-contained functions, dependencies on stdlib + curl_cffi + webui.db only.

- `_oai_exchange_refresh_to_access_token`: refresh_token grant → complete token dict
- `_oai_team_id_from_access_token`: parse access_token JWT, extract chatgpt_account_id
- `_oai_send_team_invite`: Owner sends team invitation
- `_oai_accept_team_invite`: Member accepts invitation
- `_find_team_id_from_results`: reverse lookup webui.db payment records to find team_id"""

from __future__ import annotations

import json
import os

from webui.backend.db import get_db

_OAI_CODEX_CLIENT_ID = "YOUR_OPENAI_CODEX_CLIENT_ID"


def _oai_exchange_refresh_to_access_token(refresh_token: str,
                                          client_id: str = _OAI_CODEX_CLIENT_ID) -> dict:
    """refresh_token grant → complete token dict (access_token/id_token/refresh_token).
Uses environment HTTPS_PROXY; if not set, falls back to urllib default (may fail with direct connection to auth.openai.com)."""
    import urllib.request as _urlreq, urllib.parse as _urlparse, urllib.error as _urlerr
    data = _urlparse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "scope": "openid email profile offline_access",
    }).encode()
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
    opener = _urlreq.build_opener(_urlreq.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {}
    ))
    req = _urlreq.Request(
        "https://auth.openai.com/oauth/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except _urlerr.HTTPError as e:
        body = ""
        try: body = e.read().decode()[:300]
        except Exception: pass
        raise RuntimeError(f"refresh_token 交换失败 http={e.code} body={body}") from e


def _oai_team_id_from_access_token(access_token: str) -> str:
    """Parse access_token JWT → chatgpt_account_id (= workspace id)."""
    import base64 as _b64
    parts = access_token.split(".")
    if len(parts) < 2:
        return ""
    p = parts[1]
    p += "=" * (-len(p) % 4)
    payload = json.loads(_b64.urlsafe_b64decode(p).decode())
    return (payload.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id", "") or ""


def _oai_send_team_invite(owner_at: str, team_id: str, member_email: str,
                          owner_device_id: str = "", proxy_url: str = "") -> dict:
    """Owner sends team invitation to member_email.
Calls POST https://chatgpt.com/backend-api/accounts/{team_id}/invites, request body follows gpt-team convention.
Returns: {"status": int, "body": str, "invite_id": str or ""}"""
    import curl_cffi.requests as cr
    s = cr.Session(impersonate="chrome136")
    if proxy_url:
        pu = proxy_url.replace("socks5://", "socks5h://")
        s.proxies = {"http": pu, "https": pu}
    r = s.post(
        f"https://chatgpt.com/backend-api/accounts/{team_id}/invites",
        headers={
            "authorization": f"Bearer {owner_at}",
            "chatgpt-account-id": team_id,
            "content-type": "application/json",
            "accept": "*/*",
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/admin",
            "oai-device-id": owner_device_id or "",
        },
        json={
            "email_addresses": [member_email],
            "role": "standard-user",
            "seat_type": "default",
            "resend_emails": True,
        }, timeout=30,
    )
    invite_id = ""
    try:
        j = r.json()
        invs = j.get("account_invites") or []
        if invs and isinstance(invs[0], dict):
            invite_id = invs[0].get("id", "") or ""
    except Exception:
        pass
    return {"status": r.status_code, "body": r.text, "invite_id": invite_id}


def _oai_accept_team_invite(member_at: str, team_id: str,
                            member_device_id: str = "", proxy_url: str = "") -> dict:
    """Member accepts invitation: POST https://chatgpt.com/backend-api/accounts/{team_id}/invites/accept.
This endpoint was reverse-engineered from chatgpt frontend JS (auth.login/_accept_account_id cookie handler)."""
    import curl_cffi.requests as cr
    s = cr.Session(impersonate="chrome136")
    if proxy_url:
        pu = proxy_url.replace("socks5://", "socks5h://")
        s.proxies = {"http": pu, "https": pu}
    r = s.post(
        f"https://chatgpt.com/backend-api/accounts/{team_id}/invites/accept",
        headers={
            "authorization": f"Bearer {member_at}",
            "content-type": "application/json",
            "accept": "*/*",
            "origin": "https://chatgpt.com",
            "referer": "https://chatgpt.com/",
            "oai-device-id": member_device_id or "",
        }, data="", timeout=30,
    )
    return {"status": r.status_code, "body": r.text}


def _find_team_id_from_results(email: str) -> str:
    """Scan payment records in reverse order, find team_account_id where chatgpt_email=email."""
    try:
        return get_db().find_team_id_from_results(email)
    except Exception as e:
        print(f"[self-dealer] 读 team_id 失败: {e}")
    return ""
