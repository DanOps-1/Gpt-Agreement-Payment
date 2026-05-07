"""Refresh stored Codex OAuth credentials and inspect quota windows."""
from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable

import httpx

from .db import get_db


_USER_AGENT = "codex_cli_rs/0.118.0 (Windows 10.0; x86_64) CodexApp"
_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False)


def _jwt_payload(token: str) -> dict:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return {}


def _plan_from_id_token(id_token: str) -> tuple[str, str, str]:
    payload = _jwt_payload(id_token)
    auth = payload.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        auth = {}
    plan = str(auth.get("chatgpt_plan_type") or "").strip().lower()
    account_id = str(auth.get("chatgpt_account_id") or "").strip()
    email = str(payload.get("email") or "").strip().lower()
    return plan, account_id, email


def _refresh_with_rt(refresh_token: str, *, timeout_s: float) -> tuple[dict, int, str]:
    body = {
        "client_id": _CODEX_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "openid profile email",
    }
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    with _client(timeout_s) as c:
        r = c.post(_OAUTH_TOKEN_URL, data=body, headers=headers)
    text = r.text[:500]
    try:
        data = r.json()
    except Exception:
        data = {}
    return data, r.status_code, text


def _usage_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Originator": "codex_cli_rs",
        "User-Agent": _USER_AGENT,
    }


def _extract_quota_windows(data: object) -> tuple[dict, dict, list[dict]]:
    all_windows: list[dict] = []

    def add_window(name: str, node: dict) -> None:
        all_windows.append({
            "name": name,
            "percent_remaining": node.get("percent_remaining") or node.get("remaining_percent"),
            "remaining": node.get("remaining"),
            "limit": node.get("limit"),
            "used": node.get("used") or node.get("usage"),
            "resets_at": node.get("resets_at") or node.get("reset_at"),
            "resets_in_seconds": node.get("resets_in_seconds"),
        })

    def walk(node: object, path: str = "") -> None:
        if isinstance(node, dict):
            keys = {str(k).lower() for k in node.keys()}
            if (
                {"percent_remaining", "remaining_percent", "resets_at"} & keys
                or {"used", "limit"} <= keys
                or {"remaining", "limit"} <= keys
            ):
                add_window(path.rsplit(".", 1)[-1] or "window", node)
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(data)
    dedup: list[dict] = []
    seen: set[str] = set()
    for item in all_windows:
        key = json.dumps(item, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(item)

    def match_window(*needles: str) -> dict:
        for item in dedup:
            name = str(item.get("name") or "").lower()
            if any(n in name for n in needles):
                return item
        return {}

    five_hour = match_window("5h", "5_hour", "five", "hour")
    weekly = match_window("7d", "week", "weekly")
    return five_hour, weekly, dedup[:12]


def _format_quota_message(five_hour: dict, weekly: dict) -> str:
    parts: list[str] = []
    for label, item in (("5h", five_hour), ("weekly", weekly)):
        if not item:
            continue
        percent = item.get("percent_remaining")
        remaining = item.get("remaining")
        limit = item.get("limit")
        if percent is not None:
            parts.append(f"{label}: {percent}% remaining")
        elif remaining is not None and limit is not None:
            parts.append(f"{label}: {remaining}/{limit} remaining")
    return "; ".join(parts) if parts else "credential refreshed; usage returned"


def _usage_from_access_token(access_token: str, *, timeout_s: float) -> tuple[dict, int, str]:
    with _client(timeout_s) as c:
        r = c.get(_CODEX_USAGE_URL, headers=_usage_headers(access_token))
    text = r.text[:500]
    try:
        data = r.json()
    except Exception:
        data = {}
    return data, r.status_code, text


def inspect_account_oauth_usage(account: dict, *, timeout_s: float = 10.0) -> dict:
    aid = int(account.get("id") or 0)
    email = str(account.get("email") or "").strip().lower()
    old_rt = str(account.get("refresh_token") or "").strip()
    if not old_rt:
        get_db().set_oauth_status(email, "missing", "refresh_token missing")
        return {
            "id": aid,
            "email": email,
            "status": "no_refresh_token",
            "oauth_valid": False,
            "message": "refresh_token missing",
        }

    try:
        token_data, token_status, token_text = _refresh_with_rt(old_rt, timeout_s=timeout_s)
    except httpx.TimeoutException:
        return {"id": aid, "email": email, "status": "unknown", "oauth_valid": False, "message": "token refresh timeout"}
    except httpx.HTTPError as e:
        return {"id": aid, "email": email, "status": "unknown", "oauth_valid": False, "message": f"token refresh {type(e).__name__}"}
    except Exception as e:
        return {"id": aid, "email": email, "status": "unknown", "oauth_valid": False, "message": f"token refresh {type(e).__name__}: {str(e)[:80]}"}

    if token_status in (400, 401, 403):
        return {
            "id": aid,
            "email": email,
            "status": "invalid",
            "oauth_valid": False,
            "http_status": token_status,
            "message": str(token_data.get("error") or token_data.get("message") or token_text or f"token refresh http {token_status}")[:300],
        }
    if token_status != 200:
        return {
            "id": aid,
            "email": email,
            "status": "unknown",
            "oauth_valid": False,
            "http_status": token_status,
            "message": f"token refresh http {token_status}",
        }

    access_token = str(token_data.get("access_token") or "").strip()
    id_token = str(token_data.get("id_token") or "").strip()
    new_rt = str(token_data.get("refresh_token") or "").strip() or old_rt
    if not access_token:
        return {"id": aid, "email": email, "status": "unknown", "oauth_valid": False, "message": "token refresh returned no access_token"}

    plan, account_id, token_email = _plan_from_id_token(id_token)
    if token_email:
        email = token_email
    get_db().update_registered_account_oauth_tokens(
        aid,
        access_token=access_token,
        id_token=id_token,
        refresh_token=new_rt,
    )

    try:
        usage_data, usage_status, usage_text = _usage_from_access_token(access_token, timeout_s=timeout_s)
    except httpx.TimeoutException:
        return {"id": aid, "email": email, "status": "refreshed", "oauth_valid": True, "plan": plan, "account_id": account_id, "message": "credential refreshed; usage timeout"}
    except httpx.HTTPError as e:
        return {"id": aid, "email": email, "status": "refreshed", "oauth_valid": True, "plan": plan, "account_id": account_id, "message": f"credential refreshed; usage {type(e).__name__}"}
    except Exception as e:
        return {"id": aid, "email": email, "status": "refreshed", "oauth_valid": True, "plan": plan, "account_id": account_id, "message": f"credential refreshed; usage {type(e).__name__}: {str(e)[:80]}"}

    if usage_status in (401, 403):
        return {
            "id": aid,
            "email": email,
            "status": "invalid",
            "oauth_valid": False,
            "plan": plan,
            "account_id": account_id,
            "http_status": usage_status,
            "message": f"usage http {usage_status}",
        }
    if usage_status == 429:
        error = usage_data.get("error") if isinstance(usage_data, dict) else {}
        return {
            "id": aid,
            "email": email,
            "status": "quota_limited",
            "oauth_valid": True,
            "plan": plan,
            "account_id": account_id,
            "message": str((error or {}).get("message") or "usage limit reached")[:300],
            "resets_at": (error or {}).get("resets_at"),
            "resets_in_seconds": (error or {}).get("resets_in_seconds"),
        }
    if usage_status < 200 or usage_status >= 300:
        return {
            "id": aid,
            "email": email,
            "status": "refreshed",
            "oauth_valid": True,
            "plan": plan,
            "account_id": account_id,
            "http_status": usage_status,
            "message": f"credential refreshed; usage http {usage_status}: {usage_text[:120]}",
        }

    five_hour, weekly, windows = _extract_quota_windows(usage_data)
    return {
        "id": aid,
        "email": email,
        "status": "ok",
        "oauth_valid": True,
        "plan": plan,
        "account_id": account_id,
        "message": _format_quota_message(five_hour, weekly),
        "five_hour_quota": five_hour,
        "weekly_quota": weekly,
        "usage_windows": windows,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def inspect_accounts_oauth_usage(account_ids: Iterable[int], *, max_workers: int = 3,
                                 timeout_s: float = 10.0) -> list[dict]:
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
        result = inspect_account_oauth_usage(account, timeout_s=timeout_s)
        status = "valid" if result.get("oauth_valid") else "invalid"
        if result.get("status") in ("unknown", "refreshed"):
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
