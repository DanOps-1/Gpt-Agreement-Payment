"""Inspect stored ChatGPT OAuth access tokens and Codex usage state."""
from __future__ import annotations

import base64
import json
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable, Optional

import httpx

from .db import get_db


_USER_AGENT = "codex_cli_rs/0.118.0 (Windows 10.0; x86_64) CodexApp"
_ME_URL = "https://chatgpt.com/backend-api/me"
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"


def _gost_alive(port: int = 18898) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _client(timeout: float, proxy: Optional[str]) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False, proxy=proxy)


def _headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Originator": "codex_cli_rs",
        "User-Agent": _USER_AGENT,
    }


def _jwt_payload(token: str) -> dict:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return {}


def _token_expired(access_token: str) -> bool:
    payload = _jwt_payload(access_token)
    try:
        exp = int(payload.get("exp") or 0)
    except Exception:
        exp = 0
    return bool(exp and datetime.now(timezone.utc).timestamp() >= exp)


def _plan_from_tokens(account: dict) -> str:
    for key in ("id_token", "access_token"):
        payload = _jwt_payload(str(account.get(key) or ""))
        auth = payload.get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            plan = str(auth.get("chatgpt_plan_type") or "").strip().lower()
            if plan:
                return plan
        plan = str(payload.get("chatgpt_plan_type") or "").strip().lower()
        if plan:
            return plan
    return ""


def _extract_usage_windows(data: object) -> list[dict]:
    windows: list[dict] = []

    def walk(node: object, path: str = "") -> None:
        if isinstance(node, dict):
            keys = {str(k).lower() for k in node.keys()}
            if (
                {"percent_remaining", "resets_at"} & keys
                or {"used", "limit"} <= keys
                or {"remaining", "limit"} <= keys
                or {"usage", "limit"} <= keys
            ):
                windows.append({
                    "name": path.rsplit(".", 1)[-1] or "window",
                    "percent_remaining": node.get("percent_remaining"),
                    "remaining": node.get("remaining"),
                    "limit": node.get("limit"),
                    "used": node.get("used") or node.get("usage"),
                    "resets_at": node.get("resets_at") or node.get("reset_at"),
                    "resets_in_seconds": node.get("resets_in_seconds"),
                })
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(data)
    dedup: list[dict] = []
    seen: set[str] = set()
    for item in windows:
        key = json.dumps(item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(item)
    return dedup[:12]


def _usage_message(data: object, windows: list[dict]) -> str:
    if windows:
        parts = []
        for win in windows[:3]:
            name = str(win.get("name") or "window")
            percent = win.get("percent_remaining")
            remaining = win.get("remaining")
            limit = win.get("limit")
            if percent is not None:
                parts.append(f"{name}: {percent}% remaining")
            elif remaining is not None and limit is not None:
                parts.append(f"{name}: {remaining}/{limit} remaining")
        if parts:
            return "; ".join(parts)
    if isinstance(data, dict):
        plan = data.get("plan") or data.get("plan_type")
        if plan:
            return f"usage ok, plan={plan}"
    return "usage ok"


def inspect_account_oauth_usage(account: dict, *, timeout_s: float = 10.0,
                                use_proxy: bool = True) -> dict:
    aid = int(account.get("id") or 0)
    email = str(account.get("email") or "").strip().lower()
    access_token = str(account.get("access_token") or "").strip()
    plan = _plan_from_tokens(account)
    if not access_token:
        return {
            "id": aid,
            "email": email,
            "status": "no_access_token",
            "oauth_valid": False,
            "plan": plan,
            "message": "access_token missing",
        }

    if _token_expired(access_token):
        return {
            "id": aid,
            "email": email,
            "status": "expired",
            "oauth_valid": False,
            "plan": plan,
            "message": "access_token jwt expired",
        }

    proxy = "socks5://127.0.0.1:18898" if use_proxy and _gost_alive() else None
    headers = _headers(access_token)
    try:
        with _client(timeout_s, proxy) as c:
            me = c.get(_ME_URL, headers=headers)
    except httpx.TimeoutException:
        return {"id": aid, "email": email, "status": "unknown", "oauth_valid": False, "plan": plan, "message": "me timeout"}
    except (httpx.NetworkError, httpx.ProxyError) as e:
        return {"id": aid, "email": email, "status": "unknown", "oauth_valid": False, "plan": plan, "message": f"me {type(e).__name__}"}
    except Exception as e:
        return {"id": aid, "email": email, "status": "unknown", "oauth_valid": False, "plan": plan, "message": f"me {type(e).__name__}: {str(e)[:80]}"}

    if me.status_code == 401:
        return {"id": aid, "email": email, "status": "invalid", "oauth_valid": False, "plan": plan, "message": "me http 401"}
    if me.status_code == 403:
        return {"id": aid, "email": email, "status": "forbidden", "oauth_valid": False, "plan": plan, "message": "me http 403"}
    if me.status_code != 200:
        return {"id": aid, "email": email, "status": "unknown", "oauth_valid": False, "plan": plan, "message": f"me http {me.status_code}"}

    try:
        data_me = me.json()
        email = str(data_me.get("email") or email).strip().lower()
    except Exception:
        data_me = {}

    try:
        with _client(timeout_s, proxy) as c:
            usage = c.get(_CODEX_USAGE_URL, headers=headers)
    except httpx.TimeoutException:
        return {"id": aid, "email": email, "status": "valid", "oauth_valid": True, "plan": plan, "message": "oauth valid; usage timeout"}
    except (httpx.NetworkError, httpx.ProxyError) as e:
        return {"id": aid, "email": email, "status": "valid", "oauth_valid": True, "plan": plan, "message": f"oauth valid; usage {type(e).__name__}"}
    except Exception as e:
        return {"id": aid, "email": email, "status": "valid", "oauth_valid": True, "plan": plan, "message": f"oauth valid; usage {type(e).__name__}: {str(e)[:80]}"}

    if usage.status_code == 401:
        return {"id": aid, "email": email, "status": "invalid", "oauth_valid": False, "plan": plan, "message": "usage http 401"}
    if usage.status_code == 429:
        try:
            body = usage.json()
        except Exception:
            body = {}
        error = body.get("error") if isinstance(body, dict) else {}
        return {
            "id": aid,
            "email": email,
            "status": "quota_limited",
            "oauth_valid": True,
            "plan": plan,
            "message": str((error or {}).get("message") or "usage limit reached")[:300],
            "resets_at": (error or {}).get("resets_at"),
            "resets_in_seconds": (error or {}).get("resets_in_seconds"),
        }
    if usage.status_code < 200 or usage.status_code >= 300:
        return {
            "id": aid,
            "email": email,
            "status": "valid",
            "oauth_valid": True,
            "plan": plan,
            "message": f"oauth valid; usage http {usage.status_code}",
        }

    try:
        data = usage.json()
    except Exception:
        data = {}
    if isinstance(data, dict):
        plan = str(data.get("plan_type") or data.get("plan") or plan or "").strip().lower()
    windows = _extract_usage_windows(data)
    return {
        "id": aid,
        "email": email,
        "status": "ok",
        "oauth_valid": True,
        "plan": plan,
        "message": _usage_message(data, windows),
        "usage_windows": windows,
    }


def inspect_accounts_oauth_usage(account_ids: Iterable[int], *, max_workers: int = 3,
                                 timeout_s: float = 10.0, use_proxy: bool = True) -> list[dict]:
    ids = [int(i) for i in account_ids if str(i).strip().lstrip("-").isdigit()]
    if not ids:
        return []
    db = get_db()
    workers = max(1, min(int(max_workers), len(ids), 8))
    results: list[dict] = []

    def run_one(aid: int) -> dict:
        account = db.get_registered_account(aid)
        if not account:
            return {"id": aid, "email": "", "status": "missing", "oauth_valid": False, "message": "account not found"}
        result = inspect_account_oauth_usage(account, timeout_s=timeout_s, use_proxy=use_proxy)
        status = "valid" if result.get("oauth_valid") else "invalid"
        if result.get("status") in ("unknown", "valid"):
            status = "unknown" if not result.get("oauth_valid") else "valid"
        db.update_account_check(aid, status, str(result.get("message") or "")[:500])
        return result

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(run_one, aid): aid for aid in ids}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({
                    "id": futures[fut],
                    "email": "",
                    "status": "unknown",
                    "oauth_valid": False,
                    "message": f"worker error: {type(e).__name__}: {e}",
                })
    return results
