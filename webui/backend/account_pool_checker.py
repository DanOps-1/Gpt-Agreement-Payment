"""Status checks for the new account planning pool."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable

import httpx

from .account_oauth_usage import (
    _extract_quota_windows,
    _format_quota_message,
    _plan_from_id_token,
    _refresh_with_rt,
    _usage_from_access_token,
)
from .account_pool import get_pool_items_by_ids
from .db import get_db


_PAID_PLANS = {"plus", "pro", "team", "business", "enterprise", "chatgptplus", "chatgptpro"}


def _pool_status_for_check(*, oauth_valid: bool, plan: str, usage_status: int, current_status: str) -> str:
    plan_key = str(plan or "").strip().lower().replace("_", "").replace("-", "")
    if not oauth_valid:
        return "plus_missing_rt" if current_status == "plus_with_rt" else "email_unused"
    if plan_key in _PAID_PLANS:
        return "plus_with_rt"
    if not plan_key and usage_status == 200:
        return "plus_with_rt"
    if plan_key == "free":
        return "email_unused"
    return current_status or "email_unused"


def _summary_status(result: dict) -> str:
    if result.get("status") in {"ok", "quota_limited", "refreshed"}:
        return "ok"
    if result.get("status") in {"invalid", "no_refresh_token", "error"}:
        return "fail"
    return "unknown"


def _percent(value: object) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return round(float(value), 2)
    except Exception:
        return None


def _usage_fields(status: str, message: str, five_hour: dict, weekly: dict) -> dict:
    return {
        "codex_usage_status": status,
        "codex_usage_error": "" if status in {"ok", "quota_limited", "refreshed"} else message[:500],
        "codex_usage_checked_at": time.time(),
        "codex_5h_used_percent": _percent(five_hour.get("used_percent")),
        "codex_5h_reset_at": str(five_hour.get("reset_at") or ""),
        "codex_7d_used_percent": _percent(weekly.get("used_percent")),
        "codex_7d_reset_at": str(weekly.get("reset_at") or ""),
    }


def _update_pool_item(item: dict, result: dict) -> None:
    item_id = int(item.get("id") or 0)
    if not item_id:
        return
    now = time.time()
    old_status = str(item.get("pool_status") or "")
    to_status = str(result.get("to_status") or old_status)
    message = str(result.get("message") or result.get("status") or "")[:500]
    access_token = str(result.get("access_token") or "")
    id_token = str(result.get("id_token") or "")
    refresh_token = str(result.get("refresh_token") or item.get("refresh_token") or "")
    plan = str(result.get("plan") or item.get("plan_type") or "")
    account_id = str(result.get("account_id") or item.get("account_id") or "")
    token_email = str(result.get("email") or item.get("chatgpt_email") or item.get("email") or "").strip().lower()
    usage = result.get("usage_update") if isinstance(result.get("usage_update"), dict) else {}

    with get_db()._conn() as c:
        c.execute(
            """
            UPDATE account_pool_items
            SET chatgpt_email=COALESCE(NULLIF(?, ''), chatgpt_email),
                access_token=COALESCE(NULLIF(?, ''), access_token),
                id_token=COALESCE(NULLIF(?, ''), id_token),
                refresh_token=COALESCE(NULLIF(?, ''), refresh_token),
                account_id=COALESCE(NULLIF(?, ''), account_id),
                plan_type=COALESCE(NULLIF(?, ''), plan_type),
                payment_status=?,
                pool_status=?,
                last_stage='status_check',
                last_error=?,
                codex_usage_status=COALESCE(NULLIF(?, ''), codex_usage_status),
                codex_usage_error=?,
                codex_usage_checked_at=COALESCE(?, codex_usage_checked_at),
                codex_5h_used_percent=?,
                codex_5h_reset_at=COALESCE(NULLIF(?, ''), codex_5h_reset_at),
                codex_7d_used_percent=?,
                codex_7d_reset_at=COALESCE(NULLIF(?, ''), codex_7d_reset_at),
                updated_at=?,
                activated_at=CASE WHEN ?='plus_with_rt' THEN ? ELSE activated_at END,
                rt_obtained_at=CASE WHEN ?='plus_with_rt' THEN ? ELSE rt_obtained_at END,
                failed_at=CASE WHEN ?='registration_failed' THEN ? ELSE failed_at END
            WHERE id=?
            """,
            (
                token_email,
                access_token,
                id_token,
                refresh_token,
                account_id,
                plan,
                str(result.get("status") or ""),
                to_status,
                message,
                str(usage.get("codex_usage_status") or ""),
                str(usage.get("codex_usage_error") or ""),
                usage.get("codex_usage_checked_at"),
                usage.get("codex_5h_used_percent"),
                str(usage.get("codex_5h_reset_at") or ""),
                usage.get("codex_7d_used_percent"),
                str(usage.get("codex_7d_reset_at") or ""),
                now,
                to_status,
                now,
                to_status,
                now,
                to_status,
                now,
                item_id,
            ),
        )
        c.execute(
            """
            INSERT INTO account_pool_events(item_id, ts, from_status, to_status, stage, reason, payload)
            VALUES (?, ?, ?, ?, 'status_check', ?, ?)
            """,
            (
                item_id,
                now,
                old_status,
                to_status,
                message,
                str(result.get("payload") or "")[:4000],
            ),
        )


def inspect_pool_item(item: dict, *, timeout_s: float = 10.0) -> dict:
    item_id = int(item.get("id") or 0)
    email = str(item.get("chatgpt_email") or item.get("email") or "").strip().lower()
    old_rt = str(item.get("refresh_token") or "").strip()
    current_status = str(item.get("pool_status") or "")
    if not old_rt:
        result = {
            "id": item_id,
            "email": email,
            "status": "no_refresh_token",
            "oauth_valid": False,
            "to_status": "plus_missing_rt" if current_status == "plus_with_rt" else current_status,
            "message": "refresh_token missing",
        }
        _update_pool_item(item, result)
        return result

    try:
        token_data, token_status, token_text = _refresh_with_rt(old_rt, timeout_s=timeout_s)
    except httpx.TimeoutException:
        return {"id": item_id, "email": email, "status": "unknown", "oauth_valid": False, "message": "token refresh timeout"}
    except httpx.HTTPError as e:
        return {"id": item_id, "email": email, "status": "unknown", "oauth_valid": False, "message": f"token refresh {type(e).__name__}"}
    except Exception as e:
        return {"id": item_id, "email": email, "status": "unknown", "oauth_valid": False, "message": f"token refresh {type(e).__name__}: {str(e)[:80]}"}

    if token_status in (400, 401, 403):
        result = {
            "id": item_id,
            "email": email,
            "status": "invalid",
            "oauth_valid": False,
            "http_status": token_status,
            "to_status": "plus_missing_rt" if current_status == "plus_with_rt" else "email_unused",
            "message": str(token_data.get("error") or token_data.get("message") or token_text or f"token refresh http {token_status}")[:300],
        }
        _update_pool_item(item, result)
        return result
    if token_status != 200:
        return {
            "id": item_id,
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
        return {"id": item_id, "email": email, "status": "unknown", "oauth_valid": False, "message": "token refresh returned no access_token"}

    plan, account_id, token_email = _plan_from_id_token(id_token)
    if token_email:
        email = token_email

    usage_data: dict = {}
    usage_status = 0
    usage_message = ""
    try:
        usage_data, usage_status, usage_text = _usage_from_access_token(access_token, timeout_s=timeout_s)
        usage_message = usage_text
    except httpx.TimeoutException:
        usage_message = "usage timeout"
    except httpx.HTTPError as e:
        usage_message = f"usage {type(e).__name__}"
    except Exception as e:
        usage_message = f"usage {type(e).__name__}: {str(e)[:80]}"

    if usage_status in (401, 403):
        usage_update = _usage_fields("error", f"usage http {usage_status}", {}, {})
        result = {
            "id": item_id,
            "email": email,
            "status": "error",
            "oauth_valid": True,
            "plan": plan,
            "account_id": account_id,
            "http_status": usage_status,
            "to_status": current_status,
            "message": f"usage http {usage_status}",
            "access_token": access_token,
            "id_token": id_token,
            "refresh_token": new_rt,
            "usage_update": usage_update,
        }
        _update_pool_item(item, result)
        public = dict(result)
        public.pop("access_token", None)
        public.pop("id_token", None)
        public.pop("refresh_token", None)
        public.pop("usage_update", None)
        public.update(usage_update)
        return public

    status = "refreshed"
    message = "credential refreshed"
    five_hour: dict = {}
    weekly: dict = {}
    windows: list[dict] = []
    if usage_status == 429:
        status = "quota_limited"
        error = usage_data.get("error") if isinstance(usage_data, dict) else {}
        message = str((error or {}).get("message") or "usage limit reached")[:300]
    elif usage_status and 200 <= usage_status < 300:
        status = "ok"
        five_hour, weekly, windows = _extract_quota_windows(usage_data)
        message = _format_quota_message(five_hour, weekly)
    elif usage_status:
        message = f"credential refreshed; usage http {usage_status}: {usage_message[:120]}"
    else:
        message = f"credential refreshed; {usage_message}"

    to_status = _pool_status_for_check(
        oauth_valid=True,
        plan=plan,
        usage_status=usage_status,
        current_status=current_status,
    )
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "usage_status": usage_status,
        "five_hour_quota": five_hour,
        "weekly_quota": weekly,
        "usage_windows": windows,
    }
    usage_update = _usage_fields(status, message, five_hour, weekly)
    result = {
        "id": item_id,
        "email": email,
        "status": status,
        "oauth_valid": True,
        "plan": plan,
        "account_id": account_id,
        "to_status": to_status,
        "message": message,
        "five_hour_quota": five_hour,
        "weekly_quota": weekly,
        "usage_windows": windows,
        "access_token": access_token,
        "id_token": id_token,
        "refresh_token": new_rt,
        "usage_update": usage_update,
        "payload": __import__("json").dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }
    _update_pool_item(item, result)
    public = dict(result)
    public.pop("access_token", None)
    public.pop("id_token", None)
    public.pop("refresh_token", None)
    public.pop("usage_update", None)
    public.pop("payload", None)
    public.update(usage_update)
    return public


def inspect_pool_items(ids: Iterable[int], *, max_workers: int = 3, timeout_s: float = 10.0) -> dict:
    items = get_pool_items_by_ids([int(i) for i in ids if str(i).strip().lstrip("-").isdigit()], reveal=True)
    if not items:
        return {"requested": 0, "results": [], "summary": {"ok": 0, "fail": 0, "unknown": 0, "total": 0}}
    workers = max(1, min(int(max_workers or 3), len(items), 8))
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(inspect_pool_item, item, timeout_s=timeout_s): item for item in items}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                item = futures[fut]
                results.append({
                    "id": int(item.get("id") or 0),
                    "email": item.get("primary_email") or item.get("email") or "",
                    "status": "unknown",
                    "oauth_valid": False,
                    "message": f"worker error: {type(e).__name__}: {e}",
                })
    summary = {"ok": 0, "fail": 0, "unknown": 0, "total": len(results)}
    for result in results:
        summary[_summary_status(result)] += 1
    return {"requested": len(items), "results": results, "summary": summary}
