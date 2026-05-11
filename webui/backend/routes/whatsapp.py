"""WhatsApp Web sidecar control + status."""
from __future__ import annotations

import secrets
import time

from fastapi import APIRouter, Header, HTTPException, Response
from pydantic import BaseModel, Field

from ..auth import CurrentUser
from .. import runner, wa_relay


router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


class StartRequest(BaseModel):
    mode: str = Field(pattern="^(qr|pairing)$", default="qr")
    phone: str = ""
    engine: str = ""


class SettingsRequest(BaseModel):
    engine: str = ""


class ExternalOTPRequest(BaseModel):
    otp: str = Field(min_length=4, max_length=32)
    phone: str = ""
    country_code: str = ""
    source: str = "external"
    ts: float | None = None


def _bearer_token(authorization: str = "") -> str:
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def _check_relay_token(
    token: str = "",
    x_wa_relay_token: str = "",
    authorization: str = "",
) -> None:
    got = token or x_wa_relay_token or _bearer_token(authorization) or ""
    expected = wa_relay.relay_token()
    if not got or not secrets.compare_digest(got, expected):
        raise HTTPException(status_code=403, detail="invalid relay token")


@router.get("/status")
def get_status(
    phone: str = "",
    country_code: str = "",
    since: float = 0.0,
    user: str = CurrentUser,
):
    st = wa_relay.status()
    if phone or country_code or since:
        filtered = wa_relay.latest_otp(
            since=since,
            phone=phone,
            country_code=country_code,
        )
        if filtered:
            st["latest"] = filtered
            st["updated_at"] = filtered.get("received_at") or filtered.get("ts") or st.get("updated_at")
        else:
            st.pop("latest", None)
    st["external_otp_token"] = wa_relay.relay_token()
    st["external_otp_path"] = "/api/whatsapp/external-otp"
    return st


@router.post("/start")
def start(req: StartRequest, user: str = CurrentUser):
    try:
        return wa_relay.start(mode=req.mode, pairing_phone=req.phone, engine=req.engine)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/settings")
def update_settings(req: SettingsRequest, user: str = CurrentUser):
    try:
        return wa_relay.set_preferred_engine(req.engine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/stop")
def stop(user: str = CurrentUser):
    return wa_relay.stop()


@router.post("/logout")
def logout(user: str = CurrentUser):
    return wa_relay.logout()


@router.post("/test-otp/start")
def test_otp_start(user: str = CurrentUser):
    return {"ok": True, "since": time.time(), "status": wa_relay.status()}


@router.post("/sidecar/state")
def sidecar_state(
    payload: dict,
    token: str = "",
    x_wa_relay_token: str = Header(default=""),
    authorization: str = Header(default=""),
):
    _check_relay_token(token=token, x_wa_relay_token=x_wa_relay_token, authorization=authorization)
    return {"ok": True, "state": wa_relay.apply_sidecar_state(payload)}


@router.post("/external-otp")
def external_otp(
    req: ExternalOTPRequest,
    token: str = "",
    x_wa_relay_token: str = Header(default=""),
    authorization: str = Header(default=""),
):
    _check_relay_token(token=token, x_wa_relay_token=x_wa_relay_token, authorization=authorization)
    try:
        item = wa_relay.submit_external_otp(
            req.otp,
            source=req.source or "external",
            ts=req.ts,
            sender="external_otp",
            phone=req.phone,
            country_code=req.country_code,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    runner.notify_external_otp(item)
    return {"ok": True, "latest": item}


@router.get("/latest-otp")
def latest_otp(
    response: Response,
    since: float = 0.0,
    phone: str = "",
    country_code: str = "",
    token: str = "",
    x_wa_relay_token: str = Header(default=""),
    authorization: str = Header(default=""),
):
    _check_relay_token(token=token, x_wa_relay_token=x_wa_relay_token, authorization=authorization)
    item = wa_relay.latest_otp(since=since, phone=phone, country_code=country_code)
    if not item:
        response.status_code = 204
        return None
    runner.notify_otp_consumed(item)
    return item
