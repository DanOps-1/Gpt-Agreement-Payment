"""Push accounts to cpa_autofill retail panel (https://github internal project /root/cpa_autofill).

cpa_autofill server is Node.js + SQLite, interface `POST /api/supplier/upload`
authenticated with Bearer token (personal API token rotated from retail UI).
Single batch ≤1000, ≤5 times per 60s, ≤5000 accounts per 24h.

This module only formats accounts that webui library already holds (email, refresh_token, access_token,
id_token) into codex JSON expected by cpa_autofill, then batch POST.

Server will RT-refresh once itself (auth.openai.com /oauth/token), so we don't
do token exchange on our end — but retail panel will reject rows without id_token / fake refresh_token,
so we only upload accounts with complete fields.

Server response:
    {ok: true, accepted: N, rejected: N, results: [{index, email, accepted, reason?}, ...], price: X}"""

from __future__ import annotations

import json
from typing import Iterable


def _decode_jwt_chatgpt_account_id(access_token: str) -> str:
    """Decode chatgpt_account_id from access_token JWT (cpa_autofill doesn't mandate
    upload with it, but server will compare with JWT — filling it directly is more stable)."""
    import base64
    if not access_token or "." not in access_token:
        return ""
    parts = access_token.split(".")
    if len(parts) < 2:
        return ""
    p = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(p).decode())
    except Exception:
        return ""
    auth = payload.get("https://api.openai.com/auth") or {}
    return auth.get("chatgpt_account_id", "") or ""


def _required_fields_ok(acc: dict) -> str:
    """Replicate cpa_autofill validateShape main rules, pre-filter obviously unqualified rows,
    save one round-trip. Return "" means OK, otherwise is reject reason."""
    for k in ("email", "refresh_token", "access_token", "id_token"):
        v = acc.get(k)
        if not isinstance(v, str) or not v:
            return f"missing_field:{k}"
    if len(acc["refresh_token"]) < 30:
        return f"rt_too_short(len={len(acc['refresh_token'])})"
    it = acc["id_token"]
    if len(it) < 100 or it.count(".") < 2:
        return f"id_token_not_jwt(len={len(it)})"
    if "@" not in acc["email"]:
        return "email_invalid"
    return ""


def build_payload_row(account: dict) -> dict | None:
    """Convert one row from webui registered_accounts to codex JSON expected by cpa_autofill.
    Return None if field is missing — caller handles missing_field marking."""
    email = (account.get("email") or "").strip()
    rt = (account.get("refresh_token") or "").strip()
    at = (account.get("access_token") or "").strip()
    it = (account.get("id_token") or "").strip() or at
    if not (email and rt and at and it):
        return None
    account_id = _decode_jwt_chatgpt_account_id(at)
    return {
        "type": "codex",
        "email": email,
        "refresh_token": rt,
        "access_token": at,
        "id_token": it,
        "account_id": account_id,
    }


def upload_accounts(
    accounts: list[dict],
    cfg: dict,
    *,
    price_override: float | None = None,
) -> dict:
    """Batch upload to cpa_autofill retail panel.

    Args:
        accounts: list of dict, each must have at least (email, refresh_token, access_token, id_token).
            account_id can be missing (we decode JWT to fill it).
        cfg: cpa_autofill config, from PAY_CONFIG.cpa_autofill. Requires enabled / base_url / api_token.
            Optional price / timeout_s / batch_size.
        price_override: caller temporarily override cfg.price listing price for this batch (yuan/account).

    Returns:
        {
            "ok": bool,
            "results": [{email, status, reason?}, ...],  # one per account
            "summary": {total, accepted, rejected, missing_field, api_error},
            "batches": int,            # actual batches sent (single batch ≤ batch_size)
            "api_errors": [str, ...],  # failure reasons for entire batches
            "price": float | None,     # actual listing price reported this time
        }"""
    out_results: list[dict] = []
    summary = {"total": len(accounts), "accepted": 0, "rejected": 0,
               "missing_field": 0, "api_error": 0}
    api_errors: list[str] = []

    if not cfg or not cfg.get("enabled"):
        return {"ok": False, "results": [], "summary": summary,
                "batches": 0, "api_errors": ["cpa_autofill 未启用"], "price": None}

    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_token = (cfg.get("api_token") or "").strip()
    if not base_url or not api_token:
        return {"ok": False, "results": [], "summary": summary,
                "batches": 0, "api_errors": ["cpa_autofill base_url / api_token 缺一不可"],
                "price": None}

    price = price_override if (isinstance(price_override, (int, float)) and price_override >= 0) \
        else cfg.get("price")
    try:
        price_val = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_val = None
    if price_val is None or price_val < 0:
        return {"ok": False, "results": [], "summary": summary,
                "batches": 0, "api_errors": ["未设置 price (元/号),散户面板必填"],
                "price": None}

    timeout = int(cfg.get("timeout_s") or 30)
    batch_size = int(cfg.get("batch_size") or 100)
    if batch_size <= 0 or batch_size > 1000:
        batch_size = 100

    # ── Pre-filter: select rows with complete fields, missing_field marked for incomplete ──
    valid_rows: list[tuple[int, dict, dict]] = []  # (orig_idx, source_acc, payload_row)
    for i, acc in enumerate(accounts):
        row = build_payload_row(acc)
        if not row:
            out_results.append({
                "email": acc.get("email", ""),
                "status": "missing_field",
                "reason": "缺 email / refresh_token / access_token / id_token 之一",
            })
            summary["missing_field"] += 1
            continue
        why = _required_fields_ok(row)
        if why:
            out_results.append({
                "email": row["email"],
                "status": "missing_field",
                "reason": why,
            })
            summary["missing_field"] += 1
            continue
        valid_rows.append((i, acc, row))

    if not valid_rows:
        return {"ok": True, "results": out_results, "summary": summary,
                "batches": 0, "api_errors": [], "price": price_val}

    # ── Batch POST ──
    url = f"{base_url}/api/supplier/upload"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # autofill.lukyface.com goes through CF, default urllib UA triggers 1010 block —
        # use chrome desktop UA consistent with _cpa_import_after_team.
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/136.0 Safari/537.36",
    }
    batches = 0
    for chunk_start in range(0, len(valid_rows), batch_size):
        chunk = valid_rows[chunk_start: chunk_start + batch_size]
        batch_payload = [r for _, _, r in chunk]
        body = json.dumps({"accounts": batch_payload, "price": price_val}).encode()
        batches += 1
        try:
            resp_json = _post_json(url, headers, body, timeout)
        except _UploadError as e:
            api_errors.append(str(e))
            for _, src, row in chunk:
                out_results.append({
                    "email": row["email"],
                    "status": "api_error",
                    "reason": str(e),
                })
                summary["api_error"] += 1
            print(f"[CPA-AF] ✗ batch {batches} 失败: {e}")
            continue

        # Server returns {ok, accepted, rejected, results:[{index, email, accepted, reason?}], price}
        results = resp_json.get("results")
        if not isinstance(results, list):
            err = f"服务端响应缺 results 字段: {json.dumps(resp_json)[:200]}"
            api_errors.append(err)
            for _, src, row in chunk:
                out_results.append({"email": row["email"], "status": "api_error", "reason": err})
                summary["api_error"] += 1
            continue
        for r in results:
            email = r.get("email") or ""
            accepted = bool(r.get("accepted"))
            if accepted:
                out_results.append({"email": email, "status": "ok"})
                summary["accepted"] += 1
            else:
                out_results.append({
                    "email": email,
                    "status": "rejected",
                    "reason": (r.get("reason") or "")[:300],
                })
                summary["rejected"] += 1
        print(f"[CPA-AF] batch {batches} accepted={resp_json.get('accepted')} "
              f"rejected={resp_json.get('rejected')} (price={price_val})")

    return {
        "ok": summary["api_error"] == 0,
        "results": out_results,
        "summary": summary,
        "batches": batches,
        "api_errors": api_errors,
        "price": price_val,
    }


class _UploadError(RuntimeError):
    pass


def _post_json(url: str, headers: dict, body: bytes, timeout: int) -> dict:
    """POST body, return parsed JSON. Non-2xx raises _UploadError with status + body summary.

    Use urllib direct connection — retail panel is internal service, Bearer auth suffices, no need for curl_cffi
    chrome fingerprint evasion of CF. curl_cffi's impersonate + IPv6 dual-stack previously had
    "Could not connect" false timeout (host curl can reach same address), using urllib directly is more stable.
    Explicit ProxyHandler({}) blocks HTTPS_PROXY environment variable, consistent with existing _cpa_import_after_team."""
    import urllib.request as _urlreq
    import urllib.error as _urlerr
    opener = _urlreq.build_opener(_urlreq.ProxyHandler({}))
    req = _urlreq.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode()
    except _urlerr.HTTPError as e:
        try:
            eb = e.read().decode()[:300]
        except Exception:
            eb = ""
        raise _UploadError(f"http={e.code} body={eb}") from e
    except Exception as e:
        raise _UploadError(f"transport={type(e).__name__}: {e}") from e
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise _UploadError(f"非 JSON 响应: {raw[:200]}") from e
