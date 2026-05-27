"""Promo 长链接池 HTTP 路由: 列表 / 状态 / 标记 used / 删除 / 区域转换."""
from __future__ import annotations

import json
import socket
import time
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from .. import settings as s
from ..db import get_db


router = APIRouter(prefix="/api/promo-links", tags=["promo-links"])

COUNTRY_CURRENCY: dict[str, str] = {
    "ID": "IDR",
    "US": "USD",
    "JP": "JPY",
    "GB": "GBP",
    "IE": "EUR",
    "FR": "EUR",
    "DE": "EUR",
    "ES": "EUR",
    "IT": "EUR",
    "NL": "EUR",
    "CA": "CAD",
    "AU": "AUD",
    "NZ": "NZD",
    "SG": "SGD",
    "HK": "HKD",
    "TW": "TWD",
    "KR": "KRW",
    "BR": "BRL",
    "MX": "MXN",
    "IN": "INR",
    "TH": "THB",
    "MY": "MYR",
    "PH": "PHP",
    "VN": "VND",
}


def _norm_country(value: str) -> str:
    country = (value or "").strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise HTTPException(status_code=400, detail="country 必须是 2 位 ISO 国家代码，例如 ID / US / JP")
    return country


def _norm_currency(value: str, country: str) -> str:
    currency = (value or COUNTRY_CURRENCY.get(country) or "USD").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise HTTPException(status_code=400, detail="currency 必须是 3 位币种代码，例如 IDR / USD / JPY")
    return currency


def _read_proxy_url() -> str:
    try:
        cfg = json.loads(s.PAY_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(cfg, dict):
        return ""
    return str(cfg.get("proxy") or "").strip()


def _ensure_checkout_proxy(proxy_url: str) -> str:
    """确保转换 checkout 时使用的本地 gost socks 中继已经启动。

    pipeline 主流程在进入 promo_link_loop 前会调用 _ensure_gost_alive；
    但 /api/promo-links/*/convert 是 WebUI 后端直接调用 fetch_promo_link，
    如果不做同样保活，就会踩到 socks5://127.0.0.1:18898 connect refused。
    """
    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return ""

    parsed = urlparse(proxy_url)
    host = (parsed.hostname or "").lower()
    port = int(parsed.port or 0)
    is_local = host in {"127.0.0.1", "localhost", "::1"}
    if not is_local or port <= 0:
        return proxy_url

    # 已有外部/手动 gost 在听就直接用。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.8)
        try:
            sock.connect(("127.0.0.1", port))
            return proxy_url
        except Exception:
            pass

    try:
        cfg = json.loads(s.PAY_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"代理 {proxy_url} 未监听，且读取 pay config 失败: {e}")

    ws_cfg = cfg.get("webshare") if isinstance(cfg.get("webshare"), dict) else {}
    if not ws_cfg.get("enabled"):
        raise HTTPException(
            status_code=502,
            detail=f"代理 {proxy_url} 未监听；webshare 未启用，无法自动拉起 gost",
        )

    try:
        from pipeline import _ensure_gost_alive  # type: ignore
        ok = bool(_ensure_gost_alive(cfg))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"代理 {proxy_url} 未监听，自动拉起 gost 异常: {e}")
    if not ok:
        raise HTTPException(
            status_code=502,
            detail=f"代理 {proxy_url} 未监听，自动拉起 gost 失败；请检查 Webshare API key/额度/上游代理",
        )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock2:
        sock2.settimeout(1.5)
        try:
            sock2.connect(("127.0.0.1", port))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"gost 保活返回成功但 {proxy_url} 仍不可连接: {e}")
    return proxy_url


def _get_link_row(link_id: int) -> dict:
    with get_db()._conn() as c:
        row = c.execute(
            """
            SELECT id, email, checkout_url, cs_id, processor_entity,
                   plan_name, promo_campaign_id, billing_country, billing_currency,
                   amount_due_cents, status, created_at, used_at, raw_response
            FROM promo_links WHERE id=?
            """,
            (int(link_id),),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="promo link not found")
    return dict(row)


def _plan_from_row(row: dict, requested: str = "") -> str:
    plan = (requested or "").strip().lower()
    if plan in {"plus", "team"}:
        return plan
    plan_name = (row.get("plan_name") or "").lower()
    return "team" if "team" in plan_name else "plus"


def _promo_threshold(currency: str, configured: int) -> int:
    # ChatGPT API 返回字段名叫 amount_due_cents，但 Stripe 对 JPY/KRW/IDR
    # 这类 zero-decimal/特殊币种也常用 minor amount。这里继续沿用项目既有
    # “≤100 minor units ~= ≤1 currency unit” 的保守判断，并允许前端/接口覆盖。
    return max(0, int(configured or 100))


def _replace_link_row(link_id: int, email: str, info: dict) -> None:
    with get_db()._conn() as c:
        cur = c.execute(
            """
            UPDATE promo_links
            SET email=?, checkout_url=?, cs_id=?, processor_entity=?,
                plan_name=?, promo_campaign_id=?, billing_country=?, billing_currency=?,
                amount_due_cents=?, status='fresh', created_at=?, used_at=0,
                raw_response=?
            WHERE id=?
            """,
            (
                email,
                str(info.get("checkout_url") or ""),
                str(info.get("cs_id") or ""),
                str(info.get("processor_entity") or ""),
                str(info.get("plan_name") or ""),
                str(info.get("promo_campaign_id") or ""),
                str(info.get("billing_country") or ""),
                str(info.get("billing_currency") or ""),
                int(info.get("amount_due_cents") or 0),
                time.time(),
                json.dumps(info.get("raw") or {}, ensure_ascii=False),
                int(link_id),
            ),
        )
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="promo link not found")


def _convert_link(link_id: int, req: "ConvertReq") -> dict:
    from pipeline.promo_link import fetch_promo_link

    row = _get_link_row(link_id)
    country = _norm_country(req.country)
    currency = _norm_currency(req.currency, country)
    plan = _plan_from_row(row, req.plan)
    # 默认沿用原库存行的优惠活动；只有用户显式填写时才覆盖。
    # 如果原行为空，fetch_promo_link 会按 plan 自动补 plus/team 默认 campaign。
    campaign = (req.promo_campaign_id or "").strip() or (row.get("promo_campaign_id") or "").strip()
    account = get_db().find_latest_registered_account(row["email"])
    if not account:
        raise HTTPException(
            status_code=400,
            detail=f"{row['email']} 不在 registered_accounts 库存里，无法用同账号重建 checkout",
        )
    access_token = (account.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(
            status_code=400,
            detail=f"{row['email']} 库存账号缺 access_token，无法转换区域；需要重新跑 promo_link/login 刷新凭证",
        )

    info = fetch_promo_link(
        access_token=access_token,
        cookie_header=account.get("cookie_header") or "",
        device_id=account.get("device_id") or "",
        plan=plan,
        country=country,
        currency=currency,
        promo_campaign_id=campaign,
        proxy_url=_ensure_checkout_proxy(_read_proxy_url()),
        timeout=req.timeout_s,
    )
    if not info.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=f"checkout 转换失败：{str(info.get('error') or '?')[:300]}",
        )

    amount_due = int(info.get("amount_due_cents") or 0)
    effective_campaign = str(info.get("promo_campaign_id") or campaign or "").strip()
    promo_limit = _promo_threshold(currency, req.max_promo_amount_minor)
    if req.require_promo_hit and amount_due > promo_limit:
        raise HTTPException(
            status_code=409,
            detail=(
                "checkout 已带 promo_campaign_id="
                f"{effective_campaign or '(empty)'}，但目标区域返回全价/未命中："
                f"amount_due={amount_due} {currency} minor units > {promo_limit}。"
                "通常是该账号/IP/目标国家不满足此优惠；已阻止写入库存。"
                "如确实要保存全价链接，请关闭“只保存优惠命中”。"
            ),
        )

    if req.mode == "replace":
        _replace_link_row(row["id"], row["email"], info)
        new_id = int(row["id"])
    else:
        new_id = get_db().add_promo_link({
            "email": row["email"],
            "checkout_url": info["checkout_url"],
            "cs_id": info.get("cs_id") or "",
            "processor_entity": info.get("processor_entity") or "",
            "plan_name": info.get("plan_name") or "",
            "promo_campaign_id": info.get("promo_campaign_id") or "",
            "billing_country": info.get("billing_country") or country,
            "billing_currency": info.get("billing_currency") or currency,
            "amount_due_cents": info.get("amount_due_cents") or 0,
            "raw_response": info.get("raw") or {},
        })
    return {
        "ok": True,
        "source_id": int(row["id"]),
        "id": new_id,
        "mode": req.mode,
        "email": row["email"],
        "checkout_url": info.get("checkout_url") or "",
        "cs_id": info.get("cs_id") or "",
        "plan_name": info.get("plan_name") or "",
        "promo_campaign_id": info.get("promo_campaign_id") or "",
        "billing_country": info.get("billing_country") or country,
        "billing_currency": info.get("billing_currency") or currency,
        "amount_due_cents": int(info.get("amount_due_cents") or 0),
    }


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


class ConvertReq(BaseModel):
    country: str = Field(default="ID", min_length=2, max_length=2)
    currency: str = Field(default="")
    plan: str = Field(default="", pattern="^(|plus|team)$")
    promo_campaign_id: str = ""
    require_promo_hit: bool = True
    max_promo_amount_minor: int = Field(default=100, ge=0, le=1000000)
    mode: str = Field(default="clone", pattern="^(clone|replace)$")
    timeout_s: int = Field(default=30, ge=5, le=120)


class BulkConvertReq(ConvertReq):
    ids: list[int] = Field(default_factory=list)


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


@router.post("/{link_id}/convert")
def convert_link(link_id: int, req: ConvertReq, user: str = CurrentUser):
    """用同一个库存账号重新创建指定国家/币种的 hosted checkout。

    注意：Stripe/ChatGPT checkout session 的区域不是 URL 字符串字段，不能靠替换
    文本改区；这里会读取对应 email 最近一次注册库存里的 access_token，重新调用
    ChatGPT checkout API。mode=clone 保留旧链接并新增一条；mode=replace 覆盖原行。
    """
    return _convert_link(link_id, req)


@router.post("/convert-bulk")
def convert_bulk(req: BulkConvertReq, user: str = CurrentUser):
    ids = [int(x) for x in req.ids if int(x) > 0]
    if not ids:
        raise HTTPException(status_code=400, detail="ids 为空")
    if len(ids) > 50:
        raise HTTPException(status_code=400, detail="一次最多转换 50 条")
    ok: list[dict] = []
    errors: list[dict] = []
    single_req = ConvertReq(**req.model_dump(exclude={"ids"}))
    for link_id in ids:
        try:
            ok.append(_convert_link(link_id, single_req))
        except HTTPException as e:
            errors.append({"id": link_id, "error": e.detail})
        except Exception as e:
            errors.append({"id": link_id, "error": str(e)[:300]})
    return {"ok": not errors, "converted": ok, "errors": errors}


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
