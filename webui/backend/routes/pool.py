"""Account planning pool API."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..account_pool import (
    POOL_STATUS_LABELS,
    claim_unused_emails,
    get_rotation_config,
    get_pool_item,
    import_email_lines,
    list_pool_items,
    migrate_from_legacy_inventory,
    move_items,
    set_rotation_config,
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


@router.post("/migrate/legacy")
def migrate_legacy_accounts(user: str = CurrentUser):
    return migrate_from_legacy_inventory()
