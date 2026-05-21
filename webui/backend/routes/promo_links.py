"""Promo 长链接池 HTTP 路由: 列表 / 状态 / 标记 used / 删除."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from ..db import get_db


router = APIRouter(prefix="/api/promo-links", tags=["promo-links"])


@router.get("/list")
def list_links(limit: int = 200, status: str = "", user: str = CurrentUser):
    db = get_db()
    items = db.list_promo_links(status=status, limit=min(int(limit), 1000))
    return {"items": items, "stats": db.promo_links_stats()}


@router.get("/stats")
def stats(user: str = CurrentUser):
    return get_db().promo_links_stats()


@router.post("/{link_id}/mark-used")
def mark_used(link_id: int, user: str = CurrentUser):
    if not get_db().mark_promo_link_used(link_id):
        raise HTTPException(status_code=404, detail="not found or not fresh")
    return {"ok": True}


class MarkStatusReq(BaseModel):
    status: str = Field(pattern="^(fresh|used|expired)$")


@router.post("/{link_id}/status")
def set_status(link_id: int, req: MarkStatusReq, user: str = CurrentUser):
    db = get_db()
    import time
    used_at_update = ", used_at=?" if req.status == "used" else ""
    params = [req.status]
    if req.status == "used":
        params.append(time.time())
    params.append(int(link_id))
    sql = f"UPDATE promo_links SET status=?{used_at_update} WHERE id=?"
    with db._conn() as c:
        cur = c.execute(sql, tuple(params))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "status": req.status}


@router.delete("/{link_id}")
def delete_link(link_id: int, user: str = CurrentUser):
    with get_db()._conn() as c:
        cur = c.execute("DELETE FROM promo_links WHERE id=?", (int(link_id),))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.delete("")
def delete_bulk(status: str = "", user: str = CurrentUser):
    """删除指定状态全部 (空 = 不允许批量删, 防误操作)."""
    if status not in ("used", "expired"):
        raise HTTPException(
            status_code=400,
            detail="bulk delete 只允许 status=used 或 status=expired (防误删 fresh)",
        )
    with get_db()._conn() as c:
        cur = c.execute("DELETE FROM promo_links WHERE status=?", (status,))
        return {"ok": True, "deleted": cur.rowcount}
