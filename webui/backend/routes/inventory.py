"""Local account inventory: list, validate, delete, push to CPA."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..account_inventory import build_accounts_inventory
from ..account_validator import validate_accounts, refresh_rt_status_accounts
from ..db import get_db
from .. import settings as s


router = APIRouter(prefix="/api/inventory", tags=["inventory"])


class IdsRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class CheckRequest(IdsRequest):
    timeout_s: float = 10.0
    max_workers: int = 3


class RefreshRtStatusRequest(IdsRequest):
    timeout_s: float = 15.0
    max_workers: int = 3


class CpaAutofillPushRequest(IdsRequest):
    """ids 来自前端选中;price 可临时覆盖配置里默认值,不传则用 cpa_autofill.price。"""
    price: float | None = None


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


def _load_cpa_autofill_cfg() -> dict:
    """读 cpa_autofill 配置, 优先 PAY_CONFIG.cpa_autofill;PAY_CONFIG 里
    enabled 没开或字段空时 fallback 到 wizard state (wizard Step11 改字段
    只会写 wizard state 不会自动写盘, 这里兜底让用户改完不用重导出)。"""
    try:
        cfg = json.loads(s.PAY_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读 PAY_CONFIG_PATH 失败: {e}")
    af = dict(cfg.get("cpa_autofill") or {})
    # wizard state fallback — Step11 的实时输入存这里
    if not (af.get("enabled") and af.get("base_url") and af.get("api_token")):
        try:
            wiz = get_db().get_runtime_json("wizard_state") or {}
            wiz_af = (wiz.get("answers") or {}).get("cpa_autofill") or {}
            for k in ("enabled", "base_url", "api_token"):
                if not af.get(k) and wiz_af.get(k):
                    af[k] = wiz_af[k]
        except Exception:
            pass
    if not af.get("enabled"):
        raise HTTPException(
            status_code=400,
            detail=(
                "散户面板推送未启用。去 wizard Step11 启用并填 base_url + api_token "
                "(自动写入 wizard state,推送时实时读),或直接在 PAY_CONFIG.cpa_autofill 加 "
                '{"enabled": true, "base_url": "...", "api_token": "..."}'
            ),
        )
    if not (af.get("base_url") and af.get("api_token")):
        raise HTTPException(status_code=400, detail="cpa_autofill 配置缺 base_url 或 api_token")
    return af


def _do_cpa_push(account: dict, cpa_cfg: dict) -> dict:
    """Run the CPA push for one account using pipeline._cpa_import_after_team.
    Records outcome to pipeline_results so inventory reflects new state."""
    import sys
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import pipeline  # type: ignore

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


@router.get("/accounts")
def get_accounts(user: str = CurrentUser):
    return build_accounts_inventory()


@router.post("/accounts/check")
def check_accounts(req: CheckRequest, user: str = CurrentUser):
    """Probe each account's session + 实时 plan via OpenAI APIs.

    Body: {ids: [account_id, ...], timeout_s?, max_workers?}.

    每个账号会同时:
      - validate_account: 探活 (rt/at/cookie 三档)
      - /backend-api/accounts/check/v4-2023-04-27: 拿实时 subscription_plan
        (覆盖 JWT claim 的 stale 状态; 写回 DB.last_plan_type)
    """
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
        # plan 分布: 实时 /backend-api/accounts/check 拿到的, 写回 last_plan_type
        "free": sum(1 for r in results if r.get("plan_type") == "free"),
        "plus": sum(1 for r in results if r.get("plan_type") == "plus"),
        "team": sum(1 for r in results if r.get("plan_type") == "team"),
        "pro": sum(1 for r in results if r.get("plan_type") == "pro"),
    }
    return {"results": results, "summary": summary}


@router.post("/accounts/refresh-rt-status")
def refresh_rt_status(req: RefreshRtStatusRequest, user: str = CurrentUser):
    """Use stored Codex refresh_token to mint a fresh access_token, parse
    chatgpt_plan_type (free/plus/team/pro), update inventory status, and store
    the fresh access_token back to registered_accounts.
    """
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if len(req.ids) > 500:
        raise HTTPException(status_code=400, detail="单次最多 500 个")
    workers = max(1, min(int(req.max_workers), 8))
    timeout = max(3.0, min(float(req.timeout_s), 45.0))
    results = refresh_rt_status_accounts(req.ids, max_workers=workers, timeout_s=timeout)
    summary = {
        "total": len(results),
        "valid": sum(1 for r in results if r.get("status") == "valid"),
        "invalid": sum(1 for r in results if r.get("status") == "invalid"),
        "unknown": sum(1 for r in results if r.get("status") == "unknown"),
        "missing": sum(1 for r in results if r.get("status") == "missing"),
        "no_rt": sum(1 for r in results if r.get("status") == "no_rt"),
        "free": sum(1 for r in results if r.get("plan_type") == "free"),
        "plus": sum(1 for r in results if r.get("plan_type") == "plus"),
        "team": sum(1 for r in results if r.get("plan_type") == "team"),
        "pro": sum(1 for r in results if r.get("plan_type") == "pro"),
    }
    return {"results": results, "summary": summary}


@router.post("/accounts/delete")
def delete_accounts(req: IdsRequest, user: str = CurrentUser):
    """Hard-delete accounts by id. Associated pipeline_results / card_results /
    oauth_status rows are kept (audit trail; lookup by email still works)."""
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    n = get_db().delete_registered_accounts(req.ids)
    return {"deleted": n, "requested": len(req.ids)}


@router.post("/accounts/cpa-autofill-push")
def cpa_autofill_push(req: CpaAutofillPushRequest, user: str = CurrentUser):
    """把选中账号推到 cpa_autofill 散户面板 (POST /api/supplier/upload)。

    每行需要 access_token / refresh_token / id_token 都齐全,缺一会标
    missing_field。服务端会自己再 RT-refresh 一次做 anti-double-spend,
    所以本地账号上传后,本地的 refresh_token 就废了 — 调用方应自己评估。
    """
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if len(req.ids) > 1000:
        raise HTTPException(status_code=400, detail="单次最多 1000 个 (散户面板单批上限)")
    # 挂单价不预设, 调用方必须本批显式传 — 防止误打默认价
    if req.price is None:
        raise HTTPException(status_code=400, detail="必须传 price (元/号);前端推送按钮会弹窗输入")
    if req.price < 0:
        raise HTTPException(status_code=400, detail="price 须为非负数字")
    cfg = _load_cpa_autofill_cfg()

    import sys
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import pipeline  # type: ignore

    db = get_db()
    # 拉本地账号,缺失的标 missing 但不调上传
    accounts: list[dict] = []
    missing_ids: list[int] = []
    for aid in req.ids:
        acc = db.get_registered_account(int(aid))
        if not acc:
            missing_ids.append(int(aid))
            continue
        accounts.append({
            "id": int(aid),
            "email": acc.get("email", ""),
            "refresh_token": acc.get("refresh_token", ""),
            "access_token": acc.get("access_token", ""),
            "id_token": acc.get("id_token", ""),
        })

    upload_result = pipeline._cpa_autofill_upload(
        accounts, cfg, price_override=req.price,
    )

    # 写 pipeline_results,让 inventory 的 cpa_status 反映本次推送
    # (跟 cpa-push 共用 mode 字段,只是用前缀区分)
    try:
        for r in upload_result.get("results", []):
            email = r.get("email", "")
            status_str = r.get("status", "")
            db.add_pipeline_result({
                "ts": datetime.now(timezone.utc).isoformat(),
                "mode": "cpa_autofill_push_manual",
                "status": "ok" if status_str == "ok" else "fail",
                "registration": {"status": "reused", "email": email},
                "payment": {"status": "skipped", "email": email},
                "cpa_import": status_str if status_str == "ok" else f"af_{status_str}",
            })
    except Exception:
        pass

    summary = dict(upload_result.get("summary", {}))
    summary["missing"] = len(missing_ids)
    return {
        "results": upload_result.get("results", []),
        "summary": summary,
        "batches": upload_result.get("batches", 0),
        "api_errors": upload_result.get("api_errors", []),
        "price": upload_result.get("price"),
        "missing_ids": missing_ids,
    }


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
