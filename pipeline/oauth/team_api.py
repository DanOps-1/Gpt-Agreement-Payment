"""OpenAI Codex refresh-token exchange + ChatGPT Team invite/accept API.

从 pipeline._monolith 抽出, 五个 fn 自包含, 仅依赖 stdlib + curl_cffi + webui.db.

- `_oai_exchange_refresh_to_access_token`: refresh_token grant → 完整 token dict
- `_oai_team_id_from_access_token`: 解 access_token JWT, 拿 chatgpt_account_id
- `_oai_send_team_invite`: Owner 推团队邀请
- `_oai_accept_team_invite`: Member 接受邀请
- `_find_team_id_from_results`: 倒查 webui.db 的支付记录找 team_id
"""

from __future__ import annotations

import json
import os

from webui.backend.db import get_db

_OAI_CODEX_CLIENT_ID = "YOUR_OPENAI_CODEX_CLIENT_ID"


def _oai_exchange_refresh_to_access_token(refresh_token: str,
                                          client_id: str = _OAI_CODEX_CLIENT_ID) -> dict:
    """refresh_token grant → 完整 token dict (access_token/id_token/refresh_token)。
    走环境 HTTPS_PROXY；若没有，走 urllib 默认（可能直连 auth.openai.com 失败）。"""
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
    """解 access_token JWT → chatgpt_account_id（= workspace id）。"""
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
    """Owner 向 member_email 发送团队邀请。
    调 POST https://chatgpt.com/backend-api/accounts/{team_id}/invites，body 沿用 gpt-team 的写法。
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
    """Member 接受邀请：POST https://chatgpt.com/backend-api/accounts/{team_id}/invites/accept。
    该 endpoint 是从 chatgpt 前端 JS (auth.login/_accept_account_id cookie handler) 逆出来的。"""
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
    """倒序扫支付记录，找 chatgpt_email=email 的 team_account_id。"""
    try:
        return get_db().find_team_id_from_results(email)
    except Exception as e:
        print(f"[self-dealer] 读 team_id 失败: {e}")
    return ""
