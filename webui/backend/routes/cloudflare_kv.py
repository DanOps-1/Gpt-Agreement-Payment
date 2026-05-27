"""Auto-setup endpoint：用户只填 API token，后端把 KV + Worker + 3 zone 的
catch-all 路由全配好，返回写入 SQLite secrets 所需的字段。

复用 scripts/setup_cf_email_worker.py 的 CFClient（已重构成抛 CFError 而
非 SystemExit），不再要求用户跑 CLI 脚本。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_db

# 把 scripts/ 加到 sys.path 以便 import setup_cf_email_worker
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from setup_cf_email_worker import CFClient, CFError, WORKER_JS  # noqa: E402

router = APIRouter(prefix="/api/cloudflare_kv", tags=["cloudflare_kv"])


class AutoSetupInput(BaseModel):
    api_token: str
    account_id: Optional[str] = None  # 不传时从 /accounts 列表里挑第一个
    zones: list[str] = []
    worker_name: str = "otp-relay"
    kv_name: str = "OTP_KV"
    fallback_to: str = ""


class ZoneResult(BaseModel):
    zone: str
    ok: bool
    before: str = ""
    error: str = ""


class AutoSetupResult(BaseModel):
    account_id: str
    account_name: str
    kv_namespace_id: str
    worker_name: str
    zones_configured: list[ZoneResult]
    secrets_path: Optional[str] = None


def _short_actions(actions: list) -> str:
    return "; ".join(
        f"{a.get('type')}={','.join(a.get('value') or [])}" for a in actions
    ) or "<none>"


@router.post("/auto-setup", response_model=AutoSetupResult)
def auto_setup(body: AutoSetupInput, user: str = CurrentUser):
    """一键部署：建 KV → 上传 Worker → 给每 zone 切 catch-all → 落 SQLite secrets。"""
    if not WORKER_JS.exists():
        raise HTTPException(status_code=500, detail=f"找不到 Worker 脚本: {WORKER_JS}")

    client = CFClient(body.api_token)

    # ── account_id：缺省自动发现（取第一个能访问的）
    account_id = (body.account_id or "").strip()
    if not account_id:
        r = client._req("GET", "/accounts?per_page=10")
        if not r.get("success"):
            raise HTTPException(status_code=400, detail=f"列 accounts 失败: {r.get('errors')}")
        results = r.get("result") or []
        if not results:
            raise HTTPException(status_code=400, detail="token 看不到任何 account")
        if len(results) > 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "token 可见多个 account，请明确指定 account_id："
                    + ", ".join(f"{a['id']}={a.get('name','?')}" for a in results)
                ),
            )
        account_id = results[0]["id"]

    # ── 校验 token 实际能访问 account
    try:
        info = client.verify_token(account_id)
    except CFError as e:
        raise HTTPException(status_code=400, detail=f"token 校验失败: {e}")
    account_name = (info.get("result") or {}).get("name", "?")

    # ── KV
    try:
        kv_id = client.find_or_create_kv(account_id, body.kv_name)
    except CFError as e:
        raise HTTPException(status_code=400, detail=f"KV 失败: {e}")

    # ── Worker
    try:
        client.upload_worker(
            account_id=account_id,
            script_name=body.worker_name,
            script_body=WORKER_JS.read_text(encoding="utf-8"),
            kv_namespace_id=kv_id,
            fallback_to=body.fallback_to,
        )
    except CFError as e:
        raise HTTPException(status_code=400, detail=f"Worker 上传失败: {e}")

    # ── 每个 zone 切 catch-all
    zones_results: list[ZoneResult] = []
    for zone in body.zones:
        zone = zone.strip()
        if not zone:
            continue
        try:
            zid = client.get_zone_id(zone)
            cur = client.get_catch_all(zid)
            before = _short_actions(cur.get("actions") or [])
            client.ensure_email_routing_enabled(zid)
            client.set_catch_all_to_worker(zid, body.worker_name)
            zones_results.append(ZoneResult(zone=zone, ok=True, before=before))
        except CFError as e:
            zones_results.append(ZoneResult(zone=zone, ok=False, error=str(e)))

    # ── 写 SQLite secrets（增量合并）
    db = get_db()
    existing = db.get_runtime_json("secrets", {})
    if not isinstance(existing, dict):
        existing = {}
    cf_section = existing.setdefault("cloudflare", {})
    cf_section["api_token"] = body.api_token
    cf_section["account_id"] = account_id
    cf_section["otp_kv_namespace_id"] = kv_id
    cf_section["otp_worker_name"] = body.worker_name
    if body.zones:
        cf_section["zone_names"] = list(body.zones)
    if body.fallback_to:
        cf_section["forward_to"] = body.fallback_to
    db.set_runtime_json("secrets", existing)
    secrets_path = "sqlite:runtime_meta/secrets"

    return AutoSetupResult(
        account_id=account_id,
        account_name=account_name,
        kv_namespace_id=kv_id,
        worker_name=body.worker_name,
        zones_configured=zones_results,
        secrets_path=secrets_path,
    )
