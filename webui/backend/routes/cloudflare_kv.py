"""Auto-setup endpoint: user only provides API token, backend configures KV + Worker + catch-all
routes for all zones, returning the fields needed to write SQLite secrets.

Reuses CFClient from scripts/setup_cf_email_worker.py (refactored to raise CFError instead
of SystemExit), no longer requires users to run CLI scripts.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..auth import CurrentUser
from ..db import get_db

# Add scripts/ to sys.path to import setup_cf_email_worker
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from setup_cf_email_worker import CFClient, CFError, WORKER_JS  # noqa: E402

router = APIRouter(prefix="/api/cloudflare_kv", tags=["cloudflare_kv"])


class AutoSetupInput(BaseModel):
    api_token: str
    account_id: Optional[str] = None  # defaults to first from /accounts list
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
    """One-click deploy: create KV → upload Worker → set catch-all per zone → write SQLite secrets."""
    if not WORKER_JS.exists():
        raise HTTPException(status_code=500, detail=f"Worker script not found: {WORKER_JS}")

    client = CFClient(body.api_token)

    # ── account_id: auto-discover (pick first accessible)
    account_id = (body.account_id or "").strip()
    if not account_id:
        r = client._req("GET", "/accounts?per_page=10")
        if not r.get("success"):
            raise HTTPException(status_code=400, detail=f"List accounts failed: {r.get('errors')}")
        results = r.get("result") or []
        if not results:
            raise HTTPException(status_code=400, detail="Token can't see any account")
        if len(results) > 1:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Token sees multiple accounts, please specify account_id: "
                    + ", ".join(f"{a['id']}={a.get('name','?')}" for a in results)
                ),
            )
        account_id = results[0]["id"]

    # ── Verify token can actually access the account
    try:
        info = client.verify_token(account_id)
    except CFError as e:
        raise HTTPException(status_code=400, detail=f"Token verification failed: {e}")
    account_name = (info.get("result") or {}).get("name", "?")

    # ── KV
    try:
        kv_id = client.find_or_create_kv(account_id, body.kv_name)
    except CFError as e:
        raise HTTPException(status_code=400, detail=f"KV failed: {e}")

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
        raise HTTPException(status_code=400, detail=f"Worker upload failed: {e}")

    # ── Set catch-all per zone
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

    # ── Write SQLite secrets (incremental merge)
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
