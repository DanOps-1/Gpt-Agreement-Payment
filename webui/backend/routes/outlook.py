"""Outlook account pool HTTP routing: bulk import / list / status / delete / OAuth refresh_token renewal."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from .. import outlook_pool
from .. import outlook_oauth_refresh

router = APIRouter(prefix="/api/outlook", tags=["outlook"])


class ImportRequest(BaseModel):
    text: str = Field(min_length=4, max_length=5_000_000,
                      description="多行 'email----password----client_id----refresh_token'")


class RefreshRtRequest(BaseModel):
    email: str = Field(min_length=3, max_length=200,
                       description="目标邮箱 (必须已在池子里, 用 DB 里的密码 + client_id 走 OAuth)")


class RevalidateRequest(BaseModel):
    include_used: bool = Field(default=False,
                               description="是否也验证 status='used' 的号 (默认排除, 已注册的不必再验 RT)")
    concurrency: int = Field(default=8, ge=1, le=32)


@router.post("/import")
def import_pool(req: ImportRequest, user: str = CurrentUser):
    """Bulk data insertion. Empty lines / lines starting with # / malformed lines are automatically skipped."""
    return outlook_pool.import_lines(req.text)


@router.get("/list")
def list_pool(limit: int = 200, status: str = "", user: str = CurrentUser):
    return {
        "items": outlook_pool.list_accounts(limit=min(int(limit), 1000), status=status),
        "stats": outlook_pool.stats(),
    }


@router.get("/stats")
def get_stats(user: str = CurrentUser):
    return outlook_pool.stats()


@router.delete("/{email}")
def delete_one(email: str, user: str = CurrentUser):
    if not outlook_pool.delete(email):
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.post("/revalidate-all")
async def revalidate_all_pool(req: RevalidateRequest, user: str = CurrentUser):
    """Run RT + IMAP verification concurrently on non-used accounts in the pool, directly update status + fail_reason.

    Blocking ~N*0.3s (concurrency 8); ~4-8s for 100 accounts; ~15s for 300 accounts.
    Return transitions list to allow frontend to highlight changes like "X transitioned from dead → available"."""
    try:
        result = await asyncio.to_thread(
            outlook_pool.revalidate_all,
            concurrency=req.concurrency,
            include_used=req.include_used,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"revalidate-all 内部异常: {e}")
    return result


class DeviceCodePollReq(BaseModel):
    device_code: str = Field(min_length=10, max_length=2000)
    email: str = Field(default="", max_length=200,
                       description="目标邮箱 (可选; 用于跟 token JWT 里的 email 校验 + 更新对应 DB row). 空则自动从 JWT 取")


@router.post("/device-code/start")
def device_code_start(user: str = CurrentUser):
    """Step 1: Get user_code + verification_uri for user to authorize in their own browser.

    Return {user_code, device_code, verification_uri, expires_in, interval, message}.
    Frontend shows dialog with user_code + URL; save device_code (pass to /poll)."""
    try:
        return outlook_oauth_refresh.device_code_start()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/device-code/poll")
def device_code_poll(req: DeviceCodePollReq, user: str = CurrentUser):
    """Step 2: Frontend polls periodically until status != 'pending'.

    Success: status=ok, RT already written to DB (if email is in pool), IMAP verified.
    Failure: status=error, error field explains root cause."""
    return outlook_oauth_refresh.device_code_poll(req.device_code, req.email)


@router.post("/refresh-rt")
async def refresh_rt(req: RefreshRtRequest, user: str = CurrentUser):
    """Use OAuth Code Flow + Playwright Firefox to obtain new refresh_token, immediately verify IMAP.

    Blocking ~20-40s (open browser + navigate proofs/Add + Consent + capture code + IMAP verification).
    Playwright sync API → asyncio.to_thread wrapper to avoid blocking uvicorn worker.

    Success: status → available, RT updated, IMAP login works
    Failure:
      - Cannot obtain OAuth code: do not modify DB
      - Obtained RT but IMAP still rejected (supplier client_id restriction): write RT to DB, status=dead with reason"""
    try:
        result = await asyncio.to_thread(outlook_oauth_refresh.refresh_and_update_db, req.email)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"refresh-rt 内部异常: {e}")
    return result
