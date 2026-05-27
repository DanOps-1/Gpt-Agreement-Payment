"""把账号推到 cpa_autofill 散户面板 (https://github 内部项目 /root/cpa_autofill).

cpa_autofill 服务端是 Node.js + SQLite, 接口 `POST /api/supplier/upload`
用 Bearer token (散户 UI 里轮换出来的 personal API token) 鉴权。
单批 ≤1000, 每 60s ≤5 次, 24h ≤5000 账号。

本模块只负责把 webui 库里已经握有 (email, refresh_token, access_token,
id_token) 的账号格式化成 cpa_autofill 期望的 codex JSON, 然后批量 POST。

服务端会自己 RT-refresh 一次 (auth.openai.com /oauth/token), 所以本端不再
做 token 交换 — 但散户面板会拒收没有 id_token / 假 refresh_token 的行,
所以这里只挑齐字段的账号上传。

服务端响应:
    {ok: true, accepted: N, rejected: N, results: [{index, email, accepted, reason?}, ...], price: X}
"""

from __future__ import annotations

import json
from typing import Iterable


def _decode_jwt_chatgpt_account_id(access_token: str) -> str:
    """从 access_token JWT 里解 chatgpt_account_id (cpa_autofill 不强制
    上传带,但服务端会拿 JWT 里的对比 — 我们直接填进去更稳)。"""
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
    """复刻 cpa_autofill validateShape 主要规则,提前过滤明显不合格的行,
    省一次 round-trip。返回 "" 表示 OK,否则是 reject reason。"""
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
    """把 webui registered_accounts 一行转成 cpa_autofill 期望的 codex JSON。
    缺字段返回 None — 调用方负责标 missing_field。"""
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
    """批量上传到 cpa_autofill 散户面板。

    Args:
        accounts: list of dict, 每个至少有 (email, refresh_token, access_token, id_token)。
            account_id 可缺(本端自解 JWT 补)。
        cfg: cpa_autofill 配置, 来自 PAY_CONFIG.cpa_autofill。需要 enabled / base_url / api_token。
            可选 price / timeout_s / batch_size。
        price_override: 调用方临时覆盖 cfg.price 的本批挂单价 (元/号)。

    Returns:
        {
            "ok": bool,
            "results": [{email, status, reason?}, ...],  # 一行一个账号
            "summary": {total, accepted, rejected, missing_field, api_error},
            "batches": int,            # 实际发了多少批 (单批 ≤ batch_size)
            "api_errors": [str, ...],  # 整批失败的 batch 原因
            "price": float | None,     # 本次实际上报的挂单价
        }
    """
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

    # ── 前置过滤:挑出字段齐的行,缺字段的直接 missing_field ──
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

    # ── 分批 POST ──
    url = f"{base_url}/api/supplier/upload"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        # autofill.lukyface.com 走 CF, 默认 urllib UA 触发 1010 拦截 —
        # 用 chrome desktop UA 跟 _cpa_import_after_team 一致。
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

        # 服务端返回 {ok, accepted, rejected, results:[{index, email, accepted, reason?}], price}
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
    """POST body, return parsed JSON. 非 2xx 抛 _UploadError 带 status + body 摘要。

    用 urllib 直连 — 散户面板是自家服务, Bearer auth 即可, 不需要 curl_cffi
    chrome 指纹规避 CF。curl_cffi 的 impersonate + IPv6 dual-stack 之前出过
    "Could not connect" 假超时 (host curl 同地址能通), 直接走 urllib 更稳。
    显式 ProxyHandler({}) 屏蔽 HTTPS_PROXY 环境变量, 跟现有 _cpa_import_after_team 一致。"""
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
