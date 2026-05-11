"""Account planning pool API."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..account_pool import (
    POOL_STATUS_LABELS,
    claim_unused_emails,
    delete_items_by_status,
    get_rotation_config,
    get_pool_item,
    get_pool_items_by_ids,
    import_email_lines,
    list_pool_items,
    migrate_from_legacy_inventory,
    move_items,
    set_rotation_config,
)
from .inventory import (
    ServerPushRequest,
    _cpa_auth_file_body,
    _do_cpa_push,
    _do_sub2api_push,
    _load_cpa_cfg,
    _load_sub2api_cfg,
    _push_account_import_server,
    _safe_filename,
)


router = APIRouter(prefix="/api/pool", tags=["pool"])


class ImportEmailsRequest(BaseModel):
    text: str = ""
    source: str = "manual"
    import_batch: str = ""


class MoveRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    to_status: str
    reason: str = "manual move"


class ClaimRequest(BaseModel):
    limit: int = 1
    task_id: str = ""
    round_id: str = ""


class RotationConfigRequest(BaseModel):
    enabled: bool = False
    interval: int = 100


class IdsRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)


class ExportRequest(IdsRequest):
    target: str = Field(pattern="^(cpa|sub2api)$")


class UploadRequest(IdsRequest):
    targets: list[str] = Field(default_factory=list)
    import_url: str = "https://mail.shfjkqhk.site/api/email-data"
    import_token: str = "sakuya1.2.3."
    group_size: int = 10


class DeleteByStatusRequest(BaseModel):
    statuses: list[str] = Field(default_factory=list)


def _pool_account_for_downstream(item: dict) -> dict:
    email = str(item.get("chatgpt_email") or item.get("email") or "").strip().lower()
    return {
        "id": item.get("id"),
        "email": email,
        "password": item.get("account_password") or item.get("email_password") or "",
        "access_token": item.get("access_token") or "",
        "id_token": item.get("id_token") or "",
        "refresh_token": item.get("refresh_token") or "",
        "device_id": item.get("device_id") or "",
        "client_id": item.get("mail_client_id") or "",
        "mail_refresh_token": item.get("mail_refresh_token") or "",
        "email_refresh_token": item.get("mail_refresh_token") or "",
        "outlook_refresh_token": item.get("mail_refresh_token") or "",
    }


def _selected_accounts(ids: list[int]) -> list[dict]:
    items = get_pool_items_by_ids(ids, reveal=True)
    return [_pool_account_for_downstream(item) for item in items]


def _chunks(items: list[dict], size: int) -> list[list[dict]]:
    size = max(1, min(int(size or 10), 50))
    return [items[i:i + size] for i in range(0, len(items), size)]


def _summary(results: list[dict]) -> dict:
    return {
        "total": len(results),
        "ok": sum(1 for r in results if r.get("status") == "ok"),
        "missing": sum(1 for r in results if r.get("status") == "missing"),
        "fail": sum(1 for r in results if r.get("status") not in ("ok", "missing", "skipped")),
    }


@router.get("/accounts")
def get_pool_accounts(
    status: str = "all",
    q: str = "",
    limit: int = 200,
    offset: int = 0,
    reveal: bool = False,
    user: str = CurrentUser,
):
    return list_pool_items(status=status, query=q, limit=limit, offset=offset, reveal=reveal)


@router.get("/accounts/{item_id}")
def get_pool_account(item_id: int, reveal: bool = False, user: str = CurrentUser):
    item = get_pool_item(item_id, reveal=reveal)
    if not item:
        raise HTTPException(status_code=404, detail="account pool item not found")
    return item


@router.post("/emails/import")
def import_emails(req: ImportEmailsRequest, user: str = CurrentUser):
    lines = [line for line in req.text.splitlines() if line.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="请粘贴邮箱数据")
    if len(lines) > 5000:
        raise HTTPException(status_code=400, detail="单次最多导入 5000 行")
    return import_email_lines(lines, source=req.source, import_batch=req.import_batch)


@router.post("/accounts/move")
def move_pool_accounts(req: MoveRequest, user: str = CurrentUser):
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    if req.to_status not in POOL_STATUS_LABELS:
        raise HTTPException(status_code=400, detail=f"未知池子状态: {req.to_status}")
    return move_items(req.ids, to_status=req.to_status, reason=req.reason)


@router.post("/accounts/claim")
def claim_pool_accounts(req: ClaimRequest, user: str = CurrentUser):
    return {"items": claim_unused_emails(req.limit, task_id=req.task_id, round_id=req.round_id)}


@router.get("/rotation")
def get_rotation(user: str = CurrentUser):
    return get_rotation_config()


@router.post("/rotation")
def save_rotation(req: RotationConfigRequest, user: str = CurrentUser):
    return set_rotation_config(enabled=req.enabled, interval=req.interval)


@router.post("/accounts/export")
def export_pool_accounts(req: ExportRequest, user: str = CurrentUser):
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    accounts = _selected_accounts(req.ids)
    if not accounts:
        raise HTTPException(status_code=404, detail="没有可导出的账号")

    if req.target == "cpa":
        buf = io.BytesIO()
        exported = 0
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for acc in accounts:
                email = str(acc.get("email") or "").strip().lower()
                if not email:
                    continue
                zf.writestr(
                    f"{_safe_filename(email)}.json",
                    json.dumps(_cpa_auth_file_body(acc), ensure_ascii=False, indent=2),
                )
                exported += 1
        if not exported:
            raise HTTPException(status_code=404, detail="没有可导出的 CPA 账号")
        buf.seek(0)
        filename = f"pool-cpa-accounts-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    payload = [
        {
            "email": acc.get("email") or "",
            "access_token": acc.get("access_token") or "",
            "refresh_token": acc.get("refresh_token") or "",
            "id_token": acc.get("id_token") or "",
            "account_id": "",
            "type": "codex",
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }
        for acc in accounts
        if acc.get("email")
    ]
    if not payload:
        raise HTTPException(status_code=404, detail="没有可导出的 sub2api 账号")
    data = json.dumps({"items": payload}, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"pool-sub2api-accounts-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/accounts/upload")
def upload_pool_accounts(req: UploadRequest, user: str = CurrentUser):
    targets = [str(t or "").strip().lower() for t in req.targets if str(t or "").strip()]
    allowed = {"cpa", "sub2api", "server"}
    bad = [t for t in targets if t not in allowed]
    if bad:
        raise HTTPException(status_code=400, detail=f"未知上传目标: {', '.join(bad)}")
    if not targets:
        raise HTTPException(status_code=400, detail="请选择上传目标")
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")

    accounts = _selected_accounts(req.ids)
    if not accounts:
        raise HTTPException(status_code=404, detail="没有找到选中的规划池账号")
    group_size = max(1, min(int(req.group_size or 10), 50))
    batches = _chunks(accounts, group_size)
    response = {
        "requested": len(req.ids),
        "accounts": len(accounts),
        "group_size": group_size,
        "groups": len(batches),
        "targets": {},
    }

    cpa_cfg = _load_cpa_cfg() if "cpa" in targets else None
    sub2api_cfg = _load_sub2api_cfg() if "sub2api" in targets else None

    for target in targets:
        all_results: list[dict] = []
        group_reports: list[dict] = []
        for index, batch in enumerate(batches, start=1):
            if target == "cpa":
                results = [_do_cpa_push(acc, cpa_cfg) for acc in batch]  # type: ignore[arg-type]
            elif target == "sub2api":
                results = [_do_sub2api_push(acc, sub2api_cfg) for acc in batch]  # type: ignore[arg-type]
            else:
                server_req = ServerPushRequest(
                    ids=[],
                    import_url=req.import_url,
                    import_token=req.import_token,
                )
                with httpx.Client(timeout=30.0, trust_env=False) as client:
                    results = _push_account_import_server(client, server_req, batch)
            all_results.extend(results)
            group_reports.append({
                "index": index,
                "size": len(batch),
                "summary": _summary(results),
                "results": results,
            })
        response["targets"][target] = {
            "summary": _summary(all_results),
            "groups": group_reports,
        }
    return response


@router.post("/accounts/delete-by-status")
def delete_pool_accounts_by_status(req: DeleteByStatusRequest, user: str = CurrentUser):
    if not req.statuses:
        raise HTTPException(status_code=400, detail="请选择要删除的池子")
    if False and "email_unused" in req.statuses:
        raise HTTPException(status_code=400, detail="未激活池受保护，不能通过此功能删除")
    result = delete_items_by_status(req.statuses)
    return result


@router.post("/migrate/legacy")
def migrate_legacy_accounts(user: str = CurrentUser):
    return migrate_from_legacy_inventory()
