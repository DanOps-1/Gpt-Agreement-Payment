"""Local account inventory: list, validate, delete, push to downstream channels."""
from __future__ import annotations

import json
import os
import sys
import time
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..account_inventory import build_accounts_inventory
from ..account_oauth_usage import inspect_accounts_oauth_usage
from ..account_validator import validate_accounts
from ..db import get_db
from .. import settings as s
from .. import runner


router = APIRouter(prefix="/api/inventory", tags=["inventory"])


class IdsRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class CheckRequest(IdsRequest):
    timeout_s: float = 10.0
    max_workers: int = 3


class ServerPushRequest(IdsRequest):
    target: str = "account_import_server"
    import_url: str = "http://127.0.0.1:8787/api/import"
    import_token: str = "dev-import-token"


def _safe_filename(value: str) -> str:
    out = []
    for ch in str(value or ""):
        if ch.isalnum() or ch in ("@", ".", "_", "-"):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out).strip("._") or "account"


def _jwt_exp_iso(access_token: str) -> str:
    import base64
    if not access_token:
        return ""
    try:
        p = access_token.split(".")[1]
        p += "=" * (-len(p) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p).decode())
        if payload.get("exp"):
            return datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        pass
    return ""


def _cpa_auth_file_body(account: dict) -> dict:
    email = str(account.get("email") or "").strip().lower()
    at = str(account.get("access_token") or "").strip()
    rt = str(account.get("refresh_token") or "").strip()
    id_tok = str(account.get("id_token") or "").strip() or at
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id_token": id_tok,
        "access_token": at,
        "refresh_token": rt,
        "account_id": "",
        "email": email,
        "last_refresh": now_iso,
        "expired": _jwt_exp_iso(at),
        "type": "codex",
    }


def _load_cpa_cfg() -> dict:
    try:
        cfg = json.loads(s.PAY_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读 PAY_CONFIG_PATH 失败: {e}")
    cpa = (cfg.get("cpa") or {})
    if not cpa.get("enabled"):
        raise HTTPException(status_code=400,
                            detail="CPA 未启用：请先在 wizard Step11 填 base_url + admin_key 并启用")
    if not (cpa.get("base_url") and cpa.get("admin_key")):
        raise HTTPException(status_code=400, detail="CPA 配置缺 base_url 或 admin_key")
    return cpa


def _load_sub2api_cfg() -> dict:
    try:
        cfg = json.loads(s.PAY_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读 PAY_CONFIG_PATH 失败: {e}")
    sub2api = (cfg.get("sub2api") or {})
    if not sub2api.get("enabled"):
        raise HTTPException(status_code=400,
                            detail="sub2api 未启用：请先在 wizard Step11 填 sub2api 配置并启用")
    if not sub2api.get("base_url"):
        raise HTTPException(status_code=400, detail="sub2api 配置缺 base_url")
    if not (
        sub2api.get("admin_token")
        or sub2api.get("admin_jwt")
        or (sub2api.get("admin_email") and sub2api.get("admin_password"))
    ):
        raise HTTPException(status_code=400, detail="sub2api 配置缺 Admin JWT 或 admin_email/admin_password")
    return sub2api


def _load_pay_cfg() -> dict:
    try:
        cfg = json.loads(s.PAY_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取 PAY_CONFIG_PATH 失败: {e}")
    if not isinstance(cfg, dict):
        raise HTTPException(status_code=500, detail="PAY_CONFIG_PATH JSON 顶层不是对象")
    return cfg


def _server_push_payload(account: dict) -> dict:
    extra = _cpa_auth_file_body(account)
    return {
        "email": str(account.get("email") or "").strip().lower(),
        "password": str(account.get("password") or ""),
        "id_token": extra.get("id_token") or "",
        "access_token": extra.get("access_token") or "",
        "refresh_token": extra.get("refresh_token") or "",
        "extra": json.dumps(extra, ensure_ascii=False, separators=(",", ":")),
    }


def _account_import_item(account: dict) -> dict:
    return {
        **_server_push_payload(account),
        "enabled": True,
    }


def _push_account_import_server(client: httpx.Client, req: ServerPushRequest, accounts: list[dict]) -> list[dict]:
    url = str(req.import_url or "").strip()
    token = str(req.import_token or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="账号导入服务器 URL 不能为空")
    if not token:
        raise HTTPException(status_code=400, detail="账号导入服务器 Bearer token 不能为空")

    ready_accounts = [acc for acc in accounts if str(acc.get("refresh_token") or "").strip()]
    skipped = [
        {
            "id": acc.get("id"),
            "email": str(acc.get("email") or "").strip().lower(),
            "status": "no_rt",
            "error": "refresh_token missing",
        }
        for acc in accounts
        if not str(acc.get("refresh_token") or "").strip()
    ]
    if not ready_accounts:
        return skipped

    items = [_account_import_item(acc) for acc in ready_accounts]
    try:
        resp = client.post(
            url,
            json={"type": "accounts", "items": items},
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as e:
        return [
            {"id": acc.get("id"), "email": str(acc.get("email") or "").strip().lower(), "status": "error", "error": str(e)}
            for acc in ready_accounts
        ]

    text = resp.text[:500]
    try:
        body = resp.json()
    except Exception:
        body = None
    ok = 200 <= resp.status_code < 300
    if isinstance(body, dict) and body.get("success") is False:
        ok = False
    error = "" if ok else ((body or {}).get("message") if isinstance(body, dict) else text)
    pushed = [
        {
            "id": acc.get("id"),
            "email": str(acc.get("email") or "").strip().lower(),
            "status": "ok" if ok else "fail",
            "http_status": resp.status_code,
            "error": error,
        }
        for acc in ready_accounts
    ]
    return [*skipped, *pushed]


def _log_server_push_failures(results: list[dict]) -> None:
    failures = [r for r in results if r.get("status") != "ok"]
    if not failures:
        return
    runner._append_log(f"[inventory:server-push] failed={len(failures)}")
    for item in failures:
        aid = item.get("id") or ""
        email = str(item.get("email") or "").strip().lower() or "-"
        status = str(item.get("status") or "unknown")
        http_status = item.get("http_status")
        error = str(item.get("error") or item.get("reason") or "").strip()
        parts = [
            f"id={aid}",
            f"email={email}",
            f"status={status}",
        ]
        if http_status:
            parts.append(f"http={http_status}")
        if error:
            parts.append(f"error={error[:300]}")
        runner._append_log("[inventory:server-push] " + " ".join(parts))


def _pipeline_module():
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import pipeline  # type: ignore
    return pipeline


def _do_cpa_push(account: dict, cpa_cfg: dict) -> dict:
    """Run the CPA push for one account using pipeline._cpa_import_after_team.
    Records outcome to pipeline_results so inventory reflects new state."""
    pipeline = _pipeline_module()

    email = account.get("email", "")
    rt = (account.get("refresh_token") or "").strip()
    is_free = False  # caller will set via plan_tag if needed; default False == use plan_tag
    try:
        status = pipeline._cpa_import_after_team(
            email, "", cpa_cfg, refresh_token=rt, is_free=is_free,
        )
    except Exception as e:
        status = f"error: {type(e).__name__}: {str(e)[:120]}"

    # 记一条 pipeline_results 让 inventory 的 cpa_status 能反映本次推送
    try:
        get_db().add_pipeline_result({
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": "cpa_push_manual",
            "status": "ok" if status == "ok" else "fail",
            "registration": {"status": "reused", "email": email},
            "payment": {"status": "skipped", "email": email},
            "cpa_import": status,
        })
    except Exception:
        pass
    return {"id": account.get("id"), "email": email, "status": status}


def _do_sub2api_push(account: dict, sub2api_cfg: dict) -> dict:
    """Run the sub2api push for one account and persist its outcome."""
    pipeline = _pipeline_module()

    email = account.get("email", "")
    rt = (account.get("refresh_token") or "").strip()
    try:
        status = pipeline._sub2api_import_after_team(
            email, "", sub2api_cfg, refresh_token=rt, is_free=False,
        )
    except Exception as e:
        status = f"error: {type(e).__name__}: {str(e)[:120]}"

    try:
        get_db().add_pipeline_result({
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": "sub2api_push_manual",
            "status": "ok" if status == "ok" else "fail",
            "registration": {"status": "reused", "email": email},
            "payment": {"status": "skipped", "email": email},
            "sub2api_import": status,
        })
    except Exception:
        pass
    return {"id": account.get("id"), "email": email, "status": status}


def _do_backfill_rt(account: dict, pay_cfg: dict, pipeline) -> dict:
    db = get_db()
    aid = int(account.get("id") or 0)
    email = str(account.get("email") or "").strip().lower()
    if not aid or not email:
        return {"id": aid, "email": email, "status": "missing"}
    if str(account.get("refresh_token") or "").strip():
        return {"id": aid, "email": email, "status": "skipped", "reason": "has_rt"}
    if pipeline._should_skip_oauth_account(email):
        oauth = pipeline._get_account_oauth_status(email) or {}
        return {
            "id": aid,
            "email": email,
            "status": "skipped",
            "reason": str(oauth.get("status") or "oauth_skip"),
        }

    cpa_cfg = (pay_cfg.get("cpa") or {}) if isinstance(pay_cfg.get("cpa"), dict) else {}
    sub2api_cfg = (pay_cfg.get("sub2api") or {}) if isinstance(pay_cfg.get("sub2api"), dict) else {}
    mail_cfg = pay_cfg.get("mail") or {}
    proxy_url = str(pay_cfg.get("proxy") or "")
    client_id = str(cpa_cfg.get("oauth_client_id") or sub2api_cfg.get("oauth_client_id") or "").strip()
    if client_id and not os.environ.get("OAUTH_CODEX_CLIENT_ID"):
        os.environ["OAUTH_CODEX_CLIENT_ID"] = client_id

    password = account.get("password") or pipeline._password_from_email(email)
    sid = str(account.get("device_id") or "") or email.replace("@", "")[:16]
    rt, fail = pipeline._exchange_rt_with_classification(email, password, mail_cfg, proxy_url)
    if not rt:
        status = "dead" if fail == "account_dead" else "transient_failed"
        pipeline._set_account_oauth_status(email, status, fail)
        return {"id": aid, "email": email, "status": status, "reason": fail}

    db.update_registered_account_refresh_token(aid, rt)
    db.add_card_result({
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": "succeeded",
        "chatgpt_email": email,
        "email": email,
        "session_id": sid,
        "channel": "manual_backfill_rt",
        "refresh_token": rt,
    })
    pipeline._set_account_oauth_status(email, "succeeded")

    cpa_status = ""
    sub2api_status = ""
    if cpa_cfg.get("enabled"):
        try:
            cpa_status = pipeline._cpa_import_after_team(email, sid, cpa_cfg, refresh_token=rt, is_free=True)
        except Exception as e:
            cpa_status = f"error: {type(e).__name__}: {str(e)[:120]}"
    if sub2api_cfg.get("enabled"):
        try:
            sub2api_status = pipeline._sub2api_import_after_team(email, sid, sub2api_cfg, refresh_token=rt, is_free=True)
        except Exception as e:
            sub2api_status = f"error: {type(e).__name__}: {str(e)[:120]}"
    try:
        db.add_pipeline_result({
            "ts": datetime.now(timezone.utc).isoformat(),
            "mode": "manual_backfill_rt",
            "status": "ok",
            "registration": {"status": "reused", "email": email},
            "payment": {"status": "skipped", "email": email},
            "cpa_import": cpa_status,
            "sub2api_import": sub2api_status,
        })
    except Exception:
        pass
    return {
        "id": aid,
        "email": email,
        "status": "succeeded",
        "rt_len": len(rt),
        "cpa_status": cpa_status,
        "sub2api_status": sub2api_status,
    }


@router.get("/accounts")
def get_accounts(user: str = CurrentUser):
    return build_accounts_inventory()


@router.post("/accounts/rt-check")
def check_account_rt(req: IdsRequest, user: str = CurrentUser):
    """Inspect the stored RT/backfill state for selected accounts."""
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if len(req.ids) > 500:
        raise HTTPException(status_code=400, detail="单次最多 500 个")

    inventory = build_accounts_inventory()
    accounts_by_id = {
        int(a["id"]): a
        for a in inventory.get("accounts", [])
        if a.get("id") is not None
    }
    results: list[dict] = []
    for raw_id in req.ids:
        aid = int(raw_id)
        acc = accounts_by_id.get(aid)
        if not acc:
            results.append({
                "id": aid,
                "email": "",
                "status": "missing",
                "rt_state": "missing",
                "has_refresh_token": False,
                "can_backfill_rt": False,
                "message": "account not found",
            })
            continue
        rt_state = str(acc.get("rt_state") or "missing")
        has_rt = bool(acc.get("has_refresh_token"))
        cooldown = int(acc.get("oauth_cooldown_remaining_s") or 0)
        reason = str(acc.get("oauth_fail_reason") or "")
        if not has_rt and rt_state in ("missing", "oauth_succeeded"):
            email = str(acc.get("email") or "").strip().lower()
            if email:
                get_db().set_oauth_status(email, "missing", "refresh_token missing")
            rt_state = "missing"
            cooldown = 0
            reason = "refresh_token missing"
        if has_rt:
            message = "refresh_token exists"
        elif rt_state == "cooldown":
            message = f"RT backfill cooldown: {cooldown}s"
        elif reason:
            message = reason
        elif rt_state == "retryable":
            message = "retryable"
        elif rt_state == "dead":
            message = "marked dead"
        else:
            message = "refresh_token missing"
        results.append({
            "id": aid,
            "email": acc.get("email") or "",
            "status": rt_state,
            "rt_state": rt_state,
            "has_refresh_token": has_rt,
            "can_backfill_rt": bool(acc.get("can_backfill_rt")),
            "oauth_status": acc.get("oauth_status") or "",
            "oauth_fail_reason": reason,
            "oauth_updated_at": acc.get("oauth_updated_at") or "",
            "oauth_cooldown_remaining_s": cooldown,
            "message": message,
        })

    summary = {
        "total": len(results),
        "has_rt": sum(1 for r in results if r.get("has_refresh_token")),
        "missing": sum(1 for r in results if r.get("rt_state") == "missing"),
        "retryable": sum(1 for r in results if r.get("rt_state") == "retryable"),
        "cooldown": sum(1 for r in results if r.get("rt_state") == "cooldown"),
        "dead": sum(1 for r in results if r.get("rt_state") == "dead"),
        "oauth_succeeded": sum(1 for r in results if r.get("rt_state") == "oauth_succeeded"),
        "not_found": sum(1 for r in results if not r.get("email")),
    }
    runner._append_log(
        "[inventory:rt-check] "
        f"total={summary['total']} has_rt={summary['has_rt']} "
        f"missing={summary['missing']} retryable={summary['retryable']} "
        f"cooldown={summary['cooldown']} dead={summary['dead']} "
        f"not_found={summary['not_found']}"
    )
    return {"results": results, "summary": summary}


@router.post("/accounts/check")
def check_accounts(req: CheckRequest, user: str = CurrentUser):
    """Probe each account's session via OpenAI's /api/auth/session.
    Body: {ids: [account_id, ...], timeout_s?, max_workers?}.
    Returns per-account {id, email, status, message} (status: valid|invalid|unknown)."""
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if len(req.ids) > 500:
        raise HTTPException(status_code=400, detail="单次最多 500 个")
    workers = max(1, min(int(req.max_workers), 8))
    timeout = max(2.0, min(float(req.timeout_s), 30.0))
    results = validate_accounts(req.ids, max_workers=workers, timeout_s=timeout)
    summary = {
        "total": len(results),
        "valid": sum(1 for r in results if r.get("status") == "valid"),
        "invalid": sum(1 for r in results if r.get("status") == "invalid"),
        "unknown": sum(1 for r in results if r.get("status") == "unknown"),
    }
    return {"results": results, "summary": summary}


@router.post("/accounts/oauth-usage-check")
def check_oauth_usage(req: CheckRequest, user: str = CurrentUser):
    """Validate access_token OAuth state and inspect ChatGPT/Codex usage."""
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if len(req.ids) > 100:
        raise HTTPException(status_code=400, detail="单次最多 100 个")
    workers = max(1, min(int(req.max_workers), 8))
    timeout = max(2.0, min(float(req.timeout_s), 30.0))
    results = inspect_accounts_oauth_usage(req.ids, max_workers=workers, timeout_s=timeout)
    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "valid": sum(1 for r in results if r.get("oauth_valid")),
        "invalid": sum(1 for r in results if r.get("status") in ("invalid", "expired", "forbidden")),
        "refreshed": sum(1 for r in results if r.get("oauth_valid")),
        "quota_limited": sum(1 for r in results if r.get("status") == "quota_limited"),
        "no_refresh_token": sum(1 for r in results if r.get("status") == "no_refresh_token"),
        "unknown": sum(1 for r in results if r.get("status") == "unknown"),
        "missing": sum(1 for r in results if r.get("status") == "missing"),
    }
    plans: dict[str, int] = {}
    for item in results:
        plan = str(item.get("plan") or "unknown").strip().lower() or "unknown"
        plans[plan] = plans.get(plan, 0) + 1
    summary["plans"] = plans
    runner._append_log(
        "[inventory:oauth-usage-check] "
        f"total={summary['total']} refreshed={summary['refreshed']} ok={summary['ok']} "
        f"quota_limited={summary['quota_limited']} invalid={summary['invalid']} "
        f"no_refresh_token={summary['no_refresh_token']} unknown={summary['unknown']} "
        f"plans={json.dumps(plans, ensure_ascii=False, separators=(',', ':'))}"
    )
    return {"results": results, "summary": summary}


@router.post("/accounts/delete")
def delete_accounts(req: IdsRequest, user: str = CurrentUser):
    """Hard-delete accounts by id. Associated pipeline_results / card_results /
    oauth_status rows are kept (audit trail; lookup by email still works)."""
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    n = get_db().delete_registered_accounts(req.ids)
    return {"deleted": n, "requested": len(req.ids)}


@router.post("/accounts/delete-downloaded")
def delete_downloaded_accounts(user: str = CurrentUser):
    db = get_db()
    ids = db.downloaded_account_ids()
    if not ids:
        return {"deleted": 0, "requested": 0}
    n = db.delete_registered_accounts(ids)
    return {"deleted": n, "requested": len(ids)}


@router.post("/accounts/export-cpa-zip")
def export_cpa_zip(req: IdsRequest, user: str = CurrentUser):
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if len(req.ids) > 500:
        raise HTTPException(status_code=400, detail="单次最多 500 个")
    db = get_db()
    buf = io.BytesIO()
    exported_ids: list[int] = []
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for aid in req.ids:
            acc = db.get_registered_account(int(aid))
            if not acc:
                continue
            email = str(acc.get("email") or "").strip().lower()
            if not email:
                continue
            body = _cpa_auth_file_body(acc)
            name = f"{_safe_filename(email)}.json"
            zf.writestr(name, json.dumps(body, ensure_ascii=False, indent=2))
            exported_ids.append(int(aid))
    if not exported_ids:
        raise HTTPException(status_code=404, detail="没有可导出的账号")
    db.mark_accounts_downloaded(exported_ids)
    buf.seek(0)
    filename = f"cpa-accounts-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/accounts/backfill-rt")
def backfill_rt(req: IdsRequest, user: str = CurrentUser):
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if len(req.ids) > 100:
        raise HTTPException(status_code=400, detail="单次最多 100 个")
    try:
        status = runner.start_backfill_rt(req.ids)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"started": True, "requested": len(req.ids), "status": status}


@router.post("/accounts/server-push")
def server_push(req: ServerPushRequest, user: str = CurrentUser):
    """Push selected accounts to an account import server.

    Request sent to import_url:
      {type: "accounts", items: [{email, password, enabled, extra}, ...]}

    extra is a JSON string containing the CPA auth-file payload.
    """
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if len(req.ids) > 100:
        raise HTTPException(status_code=400, detail="单次最大 100 个")
    db = get_db()
    accounts: list[dict] = []
    results: list[dict] = [
        {"id": aid, "email": "", "status": "missing", "error": "account not found"}
        for aid in req.ids
        if not db.get_registered_account(int(aid))
    ]
    for aid in req.ids:
        acc = db.get_registered_account(int(aid))
        if acc:
            accounts.append(acc)
    if accounts:
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            results.extend(_push_account_import_server(client, req, accounts))
    pushed_ids = [int(r["id"]) for r in results if r.get("status") == "ok" and r.get("id")]
    if pushed_ids:
        db.mark_accounts_server_pushed(pushed_ids)
    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "missing": sum(1 for r in results if r.get("status") == "missing"),
        "fail": sum(1 for r in results if r.get("status") not in ("ok", "missing")),
    }
    _log_server_push_failures(results)
    return {"results": results, "summary": summary}


@router.post("/accounts/cpa-push")
def cpa_push(req: IdsRequest, user: str = CurrentUser):
    """Push selected accounts to CPA (CLIProxyAPI). Reuses
    pipeline._cpa_import_after_team. Each row's stored refresh_token (or
    fallback access_token) is used; records outcome to pipeline_results."""
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if len(req.ids) > 100:
        raise HTTPException(status_code=400, detail="单次最多 100 个")
    cpa_cfg = _load_cpa_cfg()
    db = get_db()
    results: list[dict] = []
    for aid in req.ids:
        acc = db.get_registered_account(int(aid))
        if not acc:
            results.append({"id": aid, "email": "", "status": "missing"})
            continue
        results.append(_do_cpa_push(acc, cpa_cfg))
    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "no_rt": sum(1 for r in results if r.get("status") == "no_rt"),
        "fail": sum(1 for r in results if r.get("status") not in ("ok", "no_rt", "skipped", "missing")),
    }
    return {"results": results, "summary": summary}


@router.post("/accounts/sub2api-push")
def sub2api_push(req: IdsRequest, user: str = CurrentUser):
    """Push selected accounts to sub2api. Uses each row's stored refresh_token
    or falls back to the registered access_token if no RT exists."""
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if len(req.ids) > 100:
        raise HTTPException(status_code=400, detail="单次最多 100 个")
    sub2api_cfg = _load_sub2api_cfg()
    db = get_db()
    results: list[dict] = []
    for aid in req.ids:
        acc = db.get_registered_account(int(aid))
        if not acc:
            results.append({"id": aid, "email": "", "status": "missing"})
            continue
        results.append(_do_sub2api_push(acc, sub2api_cfg))
    summary = {
        "total": len(results),
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "no_token": sum(1 for r in results if r.get("status") in ("no_token", "no_rt")),
        "fail": sum(1 for r in results if r.get("status") not in ("ok", "no_token", "no_rt", "skipped", "missing")),
    }
    return {"results": results, "summary": summary}
